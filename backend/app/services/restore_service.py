"""Restore, Stage 1 only: parse a locally-stored OPNsense config.xml
backup, extract safe (non-secret) metadata for a human-readable preview,
and compute a fingerprint so a later handoff can detect the file changing
underneath it. This module makes NO network call and has NO code path that
writes to an OPNsense host — Stage 2 (calling the real `revertBackup` API)
is a deliberately separate, not-yet-built piece of work; see
docs/restore-plan.md.

Secret redaction follows the same pattern used for VPN secrets in
ipsec_deploy_service.py/openvpn_deploy_service.py/wireguard_deploy_service.py
after the 2026-09-09 plaintext-secret bug: replace the value with
`sha256:<hex>` rather than ever storing or returning it raw. Applied here to
every XML leaf whose tag name looks secret-like, not just one known field,
since an arbitrary uploaded backup can contain secrets anywhere in the tree.
"""
import hashlib
import re
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as safe_ET

MAX_BACKUP_BYTES = 32 * 1024 * 1024  # 32 MiB, per docs/restore-plan.md
_SECRET_TAG_RE = re.compile(r"password|psk|secret|key|passphrase", re.IGNORECASE)


class InvalidBackupError(ValueError):
    """The chosen file isn't a usable OPNsense config.xml backup."""
    def __init__(self, message: str, code: str, **params: str | int):
        super().__init__(message)
        self.code = code
        self.params = params


def _redact_tree(root: Element) -> int:
    """Walks the whole tree in place, hashing any leaf element whose tag
    looks secret-like. Returns how many fields were redacted."""
    count = 0
    for el in root.iter():
        if el.text and el.text.strip() and _SECRET_TAG_RE.search(el.tag) and len(el) == 0:
            el.text = "sha256:" + hashlib.sha256(el.text.strip().encode()).hexdigest()
            count += 1
    return count


def _text(root: Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None or not el.text.strip():
        return None
    return el.text.strip()


def parse_and_redact(xml_bytes: bytes) -> dict:
    """Returns a JSON-safe manifest dict. Raises InvalidBackupError for
    anything that isn't a plausible, safely-parseable OPNsense backup."""
    if len(xml_bytes) > MAX_BACKUP_BYTES:
        limit_mib = MAX_BACKUP_BYTES // (1024 * 1024)
        raise InvalidBackupError(f"Datei ist größer als das Limit von {limit_mib} MiB.", "BACKUP_TOO_LARGE", limit=limit_mib)
    if not xml_bytes.strip():
        raise InvalidBackupError("Datei ist leer.", "BACKUP_EMPTY")
    try:
        root = safe_ET.fromstring(xml_bytes)
    except Exception as exc:  # defusedxml raises various ParseError/EntitiesForbidden subclasses
        raise InvalidBackupError(f"Datei ist kein gültiges XML: {exc}", "BACKUP_INVALID_XML") from exc
    if root.tag != "opnsense":
        raise InvalidBackupError(
            f"Unerwartetes Wurzelelement „{root.tag}“ — keine OPNsense-Konfigurationssicherung.",
            "BACKUP_WRONG_ROOT",
            root=root.tag,
        )

    secret_fields_redacted = _redact_tree(root)

    interfaces = []
    for iface in root.findall("./interfaces/*"):
        if_name = _text(iface, "if")
        interfaces.append({"name": iface.tag, "if": if_name or "unbekannt"})

    return {
        "hostname": _text(root, "./system/hostname") or "unbekannt",
        "domain": _text(root, "./system/domain") or "unbekannt",
        "webgui_protocol": _text(root, "./system/webgui/protocol") or "unbekannt",
        "webgui_port": _text(root, "./system/webgui/port") or "unbekannt",
        "interfaces": interfaces,
        "hasync_enabled": root.find("./hasync") is not None,
        "synchronize_to_ip": _text(root, "./hasync/synchronizetoip"),
        "secret_fields_redacted": secret_fields_redacted,
    }


def diff_against_host(manifest: dict, host) -> list[str]:
    """Plain-language warnings comparing the backup's metadata against the
    host record already in Postgres. Self-computed — no OPNsense call."""
    warnings: list[str] = []
    hostname = manifest.get("hostname")
    if hostname and hostname != "unbekannt" and hostname.lower() not in host.name.lower() and host.name.lower() not in hostname.lower():
        warnings.append(
            f"Hostname in der Sicherung („{hostname}“) unterscheidet sich deutlich vom verwalteten Host „{host.name}“. "
            "Bitte prüfen, ob dies wirklich die richtige Sicherung für diesen Host ist."
        )
    if manifest.get("hasync_enabled") and host.cluster_name is None:
        warnings.append(
            "Die Sicherung enthält eine HA-Synchronisationskonfiguration, der Host ist aber aktuell keinem Cluster zugeordnet."
        )
    if manifest.get("secret_fields_redacted", 0) == 0:
        warnings.append(
            "Keine geheimen Felder (Passwörter/Schlüssel) in der Sicherung gefunden — bitte prüfen, ob die Datei vollständig ist."
        )
    return warnings


def fingerprint(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()
