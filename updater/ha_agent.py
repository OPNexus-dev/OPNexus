"""Kern des HA-Agenten (docs/DESIGN-HA-UI.md, Abschnitte 3.2 bis 3.4): Kopplungscode, Einmal-Code- und Clustergeheimnis-Speicher,
Prüfung der Partner-Anfragen und die feste Verbliste.

Der Agent ist der Updater-Container; sein Netzzugang für den Partner ist allein der `ha-gate` (Caddy, kein Socket, keine
Geheimnisse). Dieses Modul kennt kein HTTP und startet nichts: es entscheidet nur, wer etwas darf und was genau ausgeführt
würde (ein fester CLI-Aufruf mit geprüften Argumenten, nie ein freier Befehl). Nur Standardbibliothek.
"""
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

CODE_PREFIX = "OPNX1-"
CODE_VERSION = 1
CODE_TTL = 30 * 60        # Sekunden, die ein Kopplungscode gilt
MAX_ATTEMPTS = 5          # Fehlversuche, danach ist der Code verbraucht
SECRET_BYTES = 16         # Geheimnis im Kopplungscode
CLUSTER_SECRET_BYTES = 32  # Geheimnis nach der Kopplung
PIN_BYTES = 32            # SHA-256 des Gate-Zertifikats (DER)

VERSION_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,5}\.[0-9]{1,6}$")  # nur feste Release-Versionen: kein "latest", keine Kandidaten, keine fremden Tags
# Wie lange der Agent kurze Verben höchstens laufen lässt (Sekunden); der Client wartet etwas länger (ha_cli.py)
SYNC_TIMEOUTS = {"status": 90, "fence": 240, "receive/stamp": 30, "receive/ca-sum": 30}
SYNC_TIMEOUT_DEFAULT = 120
SUM_RE = re.compile(r"^[0-9a-f]{64}$")
STAMP_RE = re.compile(r"^[0-9]{1,12}$")


class CodeError(ValueError):
    """Kopplungscode nicht lesbar (Tippfehler, falsche Version, abgeschnitten)."""


# ── Kopplungscode ────────────────────────────────────────────────────────────

def encode_code(ip: str, port: int, secret: bytes, pin: bytes) -> str:
    """OPNX1-<Base32 in Fünfergruppen>: Version, IPv4, Port, Geheimnis, Zertifikats-Prüfsumme und CRC16 gegen Tippfehler."""
    address = ipaddress.IPv4Address(ip)
    if not (0 < port < 65536) or len(secret) != SECRET_BYTES or len(pin) != PIN_BYTES:
        raise ValueError("ungültige Bestandteile")
    body = bytes([CODE_VERSION]) + address.packed + port.to_bytes(2, "big") + secret + pin
    raw = body + binascii.crc_hqx(body, 0xFFFF).to_bytes(2, "big")
    text = base64.b32encode(raw).decode("ascii").rstrip("=")
    return CODE_PREFIX + "-".join(text[i:i + 5] for i in range(0, len(text), 5))


def decode_code(code: str) -> dict:
    """Gegenstück zu encode_code. Groß/Klein, Leerraum und Bindestriche sind egal. Wirft CodeError."""
    text = (code or "").strip()
    if text[: len(CODE_PREFIX) - 1].upper() != CODE_PREFIX[:-1]:
        raise CodeError("Das ist kein OPNexus-Kopplungscode (er beginnt mit OPNX1-).")
    payload = re.sub(r"[\s-]", "", text[len(CODE_PREFIX) - 1:]).upper()
    try:
        raw = base64.b32decode(payload + "=" * (-len(payload) % 8))
    except (binascii.Error, ValueError):
        raise CodeError("Der Code enthält ungültige Zeichen.") from None
    expected = 1 + 4 + 2 + SECRET_BYTES + PIN_BYTES + 2
    if len(raw) != expected:
        raise CodeError("Der Code hat die falsche Länge (vollständig kopiert?).")
    body, crc = raw[:-2], raw[-2:]
    if binascii.crc_hqx(body, 0xFFFF).to_bytes(2, "big") != crc:
        raise CodeError("Die Prüfsumme des Codes stimmt nicht (Tippfehler?).")
    if body[0] != CODE_VERSION:
        raise CodeError("Dieser Code stammt von einer anderen Version von OPNexus.")
    return {
        "ip": str(ipaddress.IPv4Address(body[1:5])),
        "port": int.from_bytes(body[5:7], "big"),
        "secret": body[7:7 + SECRET_BYTES],
        "pin": body[7 + SECRET_BYTES:],
    }


# ── Speicher (Dateien im Projektverzeichnis, 0600) ────────────────────────────

def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Store:
    """Zustand des Agenten: der offene Kopplungscode (nur sein Hash) und die Kopplung selbst (Partner, Clustergeheimnis, Pin)."""

    def __init__(self, directory: Path):
        self.pairing_file = Path(directory) / "pairing.json"
        self.cluster_file = Path(directory) / "cluster.json"

    # Einmal-Code ----------------------------------------------------------
    def open_pairing(self, now: float | None = None) -> bytes:
        """Neues Geheimnis für einen Kopplungscode; ersetzt einen noch offenen. Gespeichert wird nur der Hash."""
        secret = secrets.token_bytes(SECRET_BYTES)
        _write_private(self.pairing_file, {
            "hash": hashlib.sha256(secret).hexdigest(),
            "expires": (time.time() if now is None else now) + CODE_TTL,
            "attempts": 0,
        })
        return secret

    def pairing_open(self, now: float | None = None) -> bool:
        record = _read(self.pairing_file)
        return bool(record and record.get("expires", 0) > (time.time() if now is None else now)
                    and record.get("attempts", 0) < MAX_ATTEMPTS)

    def check_pairing(self, secret: bytes, now: float | None = None) -> bool:
        """Stimmt das Geheimnis mit dem offenen Code überein? Zählt Fehlversuche; nach MAX_ATTEMPTS oder Ablauf ist der Code weg."""
        record = _read(self.pairing_file)
        if not record:
            return False
        if record.get("expires", 0) <= (time.time() if now is None else now) or record.get("attempts", 0) >= MAX_ATTEMPTS:
            self.close_pairing()
            return False
        if hmac.compare_digest(hashlib.sha256(secret).hexdigest(), str(record.get("hash", ""))):
            return True
        record["attempts"] = record.get("attempts", 0) + 1
        if record["attempts"] >= MAX_ATTEMPTS:
            self.close_pairing()
        else:
            _write_private(self.pairing_file, record)
        return False

    def close_pairing(self) -> None:
        try:
            self.pairing_file.unlink()
        except OSError:
            pass

    # Kopplung ---------------------------------------------------------------
    def set_cluster(self, secret: str, peer_ip: str, peer_port: int, peer_pin: str) -> None:
        ipaddress.IPv4Address(peer_ip)
        if not SUM_RE.fullmatch(peer_pin) or not re.fullmatch(r"[0-9a-f]{64}", secret) or not (0 < peer_port < 65536):
            raise ValueError("ungültige Kopplungsdaten")
        _write_private(self.cluster_file, {"secret": secret, "peer_ip": peer_ip, "peer_port": peer_port, "peer_pin": peer_pin})

    def cluster(self) -> dict | None:
        record = _read(self.cluster_file)
        if record and all(k in record for k in ("secret", "peer_ip", "peer_port", "peer_pin")):
            return record
        return None

    def clear_cluster(self) -> None:
        try:
            self.cluster_file.unlink()
        except OSError:
            pass


def new_cluster_secret() -> str:
    return secrets.token_hex(CLUSTER_SECRET_BYTES)


# ── Prüfung der Anfragen ──────────────────────────────────────────────────────

def bearer(headers) -> str:
    header = headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def authorize_peer(store: Store, headers, remote: str) -> bool:
    """Anfrage vom gekoppelten Partner: Clustergeheimnis (konstante Zeit) und Absender gleich Partneradresse.
    `remote` kommt vom Gate (X-HA-Remote); ohne Kopplung ist jede Anfrage abgelehnt."""
    cluster = store.cluster()
    if not cluster:
        return False
    supplied = bearer(headers)
    secret_ok = bool(supplied) and hmac.compare_digest(supplied.encode(), str(cluster["secret"]).encode())
    return secret_ok and remote == cluster["peer_ip"]


def authorize_pairing(store: Store, headers, now: float | None = None) -> bool:
    """Anfrage mit dem Geheimnis aus dem Kopplungscode (Hex im Bearer-Kopf)."""
    supplied = bearer(headers)
    try:
        secret = bytes.fromhex(supplied)
    except ValueError:
        return False
    if len(secret) != SECRET_BYTES:
        return False
    return store.check_pairing(secret, now)


# ── Feste Verben des Partner-Wegs ─────────────────────────────────────────────

@dataclass(frozen=True)
class Verb:
    method: str          # GET | POST
    kind: str            # sync (kurz, Antwort sofort) | job (Auftrag, Abfrage) | stream (Datenstrom in die CLI) | local (beantwortet der Agent selbst)
    argv: tuple          # feste Argumente der CLI nach `opnexus`
    param: str | None = None   # Name des einen erlaubten Parameters (JSON-Körper bzw. Pfadteil)
    check: re.Pattern | None = None


VERBS: dict[str, Verb] = {
    "ping": Verb("GET", "local", ()),     # Gegenprobe der Kopplung: antwortet der Agent selbst, kein Befehl
    "job": Verb("GET", "local", ()),      # Zustand des letzten Auftrags
    "status": Verb("GET", "sync", ("ha", "status", "--porcelain")),
    "fence": Verb("POST", "sync", ("ha", "fence")),
    "promote": Verb("POST", "job", ("ha", "promote", "--yes")),
    "resync": Verb("POST", "job", ("ha", "resync", "--yes", "--from-peer")),
    "update-standby": Verb("POST", "job", ("ha", "update-standby"), "version", VERSION_RE),
    "receive/data": Verb("POST", "stream", ("ha", "receive", "data")),
    "receive/ca": Verb("POST", "stream", ("ha", "receive", "ca"), "sum", SUM_RE),
    "receive/env": Verb("POST", "stream", ("ha", "receive", "env")),
    "receive/env-init": Verb("POST", "stream", ("ha", "receive", "env-init")),
    "receive/ha-ca": Verb("POST", "stream", ("ha", "receive", "ha-ca")),
    "dissolve": Verb("POST", "job", ("ha", "dissolve", "--partner-side")),
    "receive/stamp": Verb("POST", "sync", ("ha", "receive", "stamp"), "value", STAMP_RE),
    "receive/ca-sum": Verb("GET", "sync", ("ha", "receive", "ca-sum")),
}


class VerbError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def build_argv(path: str, method: str, params: dict | None) -> tuple[Verb, list[str]]:
    """Pfad `/ha/peer/<verb>` → (Verb, CLI-Argumente). Nur Einträge aus VERBS, höchstens der eine geprüfte Parameter."""
    prefix = "/ha/peer/"
    if not path.startswith(prefix):
        raise VerbError("not_found", 404)
    verb = VERBS.get(path[len(prefix):])
    if verb is None:
        raise VerbError("not_found", 404)
    if verb.method != method:
        raise VerbError("method_not_allowed", 405)
    params = params or {}
    extra = [k for k in params if k != verb.param]
    if extra:
        raise VerbError("bad_request")
    argv = list(verb.argv)
    if verb.param:
        value = params.get(verb.param)
        if not isinstance(value, str) or verb.check is None or not verb.check.fullmatch(value):
            raise VerbError("bad_request")
        argv.append(value)
    return verb, argv
