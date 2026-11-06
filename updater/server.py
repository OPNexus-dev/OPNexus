#!/usr/bin/env python3
"""OPNexus-Updater: kleine interne HTTP-API vor dem `opnexus`-Skript.

Läuft als eigener, optionaler Container (Compose-Profil "updater") und ist der
EINZIGE Teil von OPNexus, der den Docker-Socket sieht — das Backend bekommt ihn
nie. Er kann bewusst nur drei Dinge:

    GET  /status    Zustand der Installation und des letzten/laufenden Auftrags
    POST /update    {"version": "X.Y.Z"}  auf eine neuere stabile Version wechseln
    POST /rollback  das letzte Update rückgängig machen

und es gibt keinen Weg, einen eigenen Befehl auszuführen. Erreichbar ist er nur
aus dem Compose-Netz (kein veröffentlichter Port) und nur mit dem Token, den er
beim ersten Start selbst erzeugt und in sein Zustands-Volume schreibt (das
Backend hängt dasselbe Volume schreibgeschützt ein).

Die eigentliche Arbeit — pullen, wechseln, auf "gesund" warten, bei Fehler
zurückrollen, Datenbank sichern/zurückspielen — macht dasselbe Skript wie auf
der Kommandozeile (`updater/opnexus`), damit es nur eine Update-Logik gibt.
"""
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ha_agent
import ha_net

STATE_DIR = Path(os.environ.get("UPDATER_STATE_DIR", "/state"))
PROJECT_DIR = Path(os.environ.get("OPNEXUS_DIR", "/project"))
SCRIPT = os.environ.get("OPNEXUS_SCRIPT", "/usr/local/bin/opnexus")
PORT = int(os.environ.get("UPDATER_PORT", "8080"))
# Wo der Docker-Socket im Container liegt (Ziel des Mounts aus der Compose-Datei).
# Der Pfad AUF DEM HOST kann ein anderer sein (Rootless Docker:
# /run/user/<UID>/docker.sock, via DOCKER_HOST_SOCKET in der .env) — self_update()
# liest ihn deshalb aus den eigenen Mounts statt ihn fest einzugeben.
DOCKER_SOCKET = "/var/run/docker.sock"
# Wie in einem Docker-Tag, aber ohne Vorabversionen: nur X.Y.Z wird akzeptiert.
VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,5})\.(\d{1,6})$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MAX_BODY = 1024
LOG_TAIL_LINES = 150
LOG_TAIL_BYTES = 16_000
STATUS_CACHE_SECONDS = 3.0

TOKEN_FILE = STATE_DIR / "token"
JOB_FILE = STATE_DIR / "job.json"
LOG_FILE = STATE_DIR / "job.log"
PHASE_FILE = STATE_DIR / "job.phases"
HA_JOB_FILE = STATE_DIR / "ha-job.json"
HA_LOG_FILE = STATE_DIR / "ha-job.log"
HA_API = 1                       # Version der Partner-Schnittstelle (/ha/pair, /ha/peer), Teil der Vorprüfung
HA_MAX_STREAM = 4 << 30          # größter Datenstrom eines `receive` (Konfigurationssicherungen der Firewalls können groß werden)
_commit_lock = threading.Lock()  # zwei Kopplungen gleichzeitig würden sich überschreiben
HA_SYNC_INTERVAL = 300           # Abgleich der Geheimnisse (früher Cron), nur am Primär
HA_VIP_INTERVAL = 60             # VIP nachführen (früher Cron @reboot), an jedem Knoten
HA_TIMER_LOG = STATE_DIR / "ha-timer.log"
HA_TIMER_LOG_MAX = 200_000

_lock = threading.Lock()  # genau ein Auftrag zur Zeit
_status_cache: dict = {"at": 0.0, "value": None}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_version(value: str | None):
    match = VERSION_RE.match((value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in (PROJECT_DIR / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def ha_role(env_file: dict[str, str]) -> str | None:
    """Role of this node in an HA pair: the state file of `opnexus ha` (.opnexus-ha/state), else the line an older version
    wrote to the .env. Read as text only."""
    try:
        for line in (PROJECT_DIR / ".opnexus-ha" / "state").read_text(encoding="utf-8").splitlines():
            if line.startswith("HA_ROLE=") and line[8:].strip():
                return line[8:].strip()
    except OSError:
        pass
    return env_file.get("HA_ROLE") or None


def load_token() -> str:
    """Token einmalig erzeugen; danach immer aus der Datei lesen."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)  # Zugriff regelt, welche Container das Volume sehen
    tmp.replace(TOKEN_FILE)
    return token


def read_job() -> dict:
    try:
        return json.loads(JOB_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_job(job: dict) -> None:
    tmp = JOB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(job), encoding="utf-8")
    tmp.replace(JOB_FILE)


def log_tail() -> list[str]:
    try:
        raw = LOG_FILE.read_bytes()[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ANSI_RE.sub("", line).rstrip() for line in raw.splitlines()]
    return [line for line in lines if line][-LOG_TAIL_LINES:]


def phases() -> list[str]:
    try:
        return [line.strip() for line in PHASE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def script_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"OPNEXUS_DIR": str(PROJECT_DIR), "OPNEXUS_UPDATER": "1"})
    env.update(extra or {})
    return env


def live_status() -> dict:
    """`opnexus status --porcelain`, kurz zwischengespeichert (docker exec ist nicht gratis)."""
    cached = _status_cache["value"]
    if cached is not None and time.monotonic() - _status_cache["at"] < STATUS_CACHE_SECONDS:
        return cached
    env_file = read_env_file()
    result: dict = {
        "mode": "release" if env_file.get("OPNEXUS_IMAGE_REPO") else "local",
        "version_setting": env_file.get("OPNEXUS_VERSION") or None,
        "running_version": None,
        "db_revision": None,
        "host_count": None,
        "ha_role": ha_role(env_file),  # set by `opnexus ha init`: this node is part of an HA pair
        "services": {},
        "last_update": None,
    }
    if result["mode"] == "release":
        try:
            proc = subprocess.run(
                [SCRIPT, "status", "--porcelain"], cwd=PROJECT_DIR, env=script_env(),
                capture_output=True, text=True, timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0:
            last: dict[str, str] = {}
            for line in proc.stdout.splitlines():
                key, _, value = line.partition("=")
                if key == "running_version":
                    result["running_version"] = value or None
                elif key == "db_revision":
                    result["db_revision"] = value or None
                elif key == "host_count":
                    result["host_count"] = int(value) if value.isdigit() else None
                elif key.startswith("service."):
                    result["services"][key.split(".", 1)[1]] = value
                elif key.startswith("last_update."):
                    last[key.split(".", 1)[1]] = value
            if last:
                result["last_update"] = {
                    "prev_version": last.get("prev_setting") if parse_version(last.get("prev_setting")) else last.get("prev_version"),
                    "prev_db_revision": last.get("prev_db_revision") or None,
                    "target": last.get("target"),
                    "started": last.get("started"),
                }
    _status_cache.update(at=time.monotonic(), value=result)
    return result


def status_payload() -> dict:
    job = read_job()
    running = job.get("state") == "running"
    payload = {"job": job or None, "phases": phases() if job else [], "log": log_tail() if job else []}
    if running:
        # Während eines Auftrags kann das Backend gerade fehlen — nicht anfragen.
        payload.update(mode="release", running_version=None, db_revision=None, services={}, last_update=None,
                       rollback_available=False, rollback_loses_data=False)
        return payload
    live = live_status()
    last = live["last_update"]
    payload.update(live)
    payload["rollback_available"] = bool(last and last.get("prev_version"))
    payload["rollback_loses_data"] = bool(last and last.get("prev_db_revision") != live["db_revision"])
    return payload


def ha_store() -> ha_agent.Store:
    return ha_agent.Store(PROJECT_DIR / ".opnexus-ha" / "agent")


def read_ha_job() -> dict:
    try:
        return json.loads(HA_JOB_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_ha_job(job: dict) -> None:
    tmp = HA_JOB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(job), encoding="utf-8")
    tmp.replace(HA_JOB_FILE)


def ha_log_tail() -> list[str]:
    try:
        raw = HA_LOG_FILE.read_bytes()[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in (ANSI_RE.sub("", ln).rstrip() for ln in raw.splitlines()) if line][-LOG_TAIL_LINES:]


def ha_facts() -> dict:
    """Was der Partner vor dem Einrichten über diesen Knoten wissen muss (Vorprüfung): Version, Datenbank, Rolle, Platz."""
    _status_cache["value"] = None
    live = live_status()
    try:
        free = shutil.disk_usage(PROJECT_DIR).free
    except OSError:
        free = None
    return {"api": HA_API, "mode": live["mode"], "version": live["running_version"], "db_revision": live["db_revision"],
            "ha_role": live["ha_role"], "hosts": live.get("host_count"), "disk_free": free, "proxy_port": read_env_file().get("PROXY_HTTPS_PORT") or "443"}


ACTOR_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")   # wer das Übernehmen angestoßen hat: die Konto-ID (der frei wählbare Name geht nie durch den Updater; das Audit schlägt ihn nach)
HA_GATE_DIR = Path(os.environ.get("HA_GATE_DIR", "/ha-gate"))
IPV4_RE = re.compile(r"^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$")
CIDR_RE = re.compile(r"^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])/(3[0-2]|[12]?[0-9])$")
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEV_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
CODE_RE = re.compile(r"^[A-Za-z0-9 -]{10,200}$")   # nur die Zeichen eines Kopplungscodes (Base32, Bindestriche, Leerraum)
# Aktionen des Primärs aus der Oberfläche: Name -> feste CLI-Argumente. Keine Parameter, keine freien Befehle.
HA_ACTIONS = {
    "sync": ["ha", "sync"],
    "switch": ["ha", "switch"],
    "rebuild": ["ha", "rebuild-standby"],
    "dissolve": ["ha", "dissolve", "--yes"],
    "dissolve_local": ["ha", "dissolve", "--yes", "--local-only"],
}


def start_ha_job(verb: str, argv: list[str], extra_env: dict[str, str] | None = None, stdin_text: str | None = None) -> dict:
    if not _lock.acquire(blocking=False):
        raise Reject("busy")
    try:
        if read_job().get("state") == "running" or read_ha_job().get("state") == "running":
            raise Reject("busy")
        job = {"id": secrets.token_hex(8), "verb": verb, "state": "running", "started_at": now_iso(), "finished_at": None, "exit_code": None}
        HA_LOG_FILE.write_text("", encoding="utf-8")
        write_ha_job(job)
    except BaseException:
        _lock.release()
        raise
    threading.Thread(target=_run_ha_job, args=(job, argv, extra_env or {}, stdin_text), daemon=True).start()
    return job


def _run_ha_job(job: dict, argv: list[str], extra_env: dict[str, str], stdin_text: str | None = None) -> None:
    try:
        with HA_LOG_FILE.open("ab") as log:
            proc = subprocess.Popen([SCRIPT, *argv], cwd=PROJECT_DIR, env=script_env({"TERM": "dumb", **extra_env}), stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL, start_new_session=True)
            if stdin_text is not None:   # z. B. der Kopplungscode: über stdin, nie als Argument (Prozessliste)
                try:
                    proc.stdin.write(stdin_text.encode())
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            code = proc.wait()
    except OSError as exc:
        code = 127
        with HA_LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"Agent: Skript konnte nicht gestartet werden: {exc}\n")
    job.update(state="succeeded" if code == 0 else "failed", exit_code=code, finished_at=now_iso())
    write_ha_job(job)
    _status_cache["value"] = None
    try:
        if code == 0 and job.get("verb") == "update-standby":
            self_update()   # der Agent des Standbys läuft sonst weiter auf dem alten Image (und nach einem Wechsel als Primär)
    finally:
        _lock.release()


def run_ha_sync(argv: list[str], timeout: int) -> dict:
    try:
        proc = subprocess.run([SCRIPT, *argv], cwd=PROJECT_DIR, env=script_env({"TERM": "dumb"}), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Reject("timeout", 504)
    except OSError:
        raise Reject("script_failed", 500)
    return {"rc": proc.returncode, "output": ANSI_RE.sub("", proc.stdout)[-16000:], "error": ANSI_RE.sub("", proc.stderr)[-4000:]}


def run_ha_stream(argv: list[str], rfile, length: int) -> dict:
    """Körper (genau `length` Bytes) in die CLI leiten, ohne ihn ganz im Speicher zu halten."""
    try:
        proc = subprocess.Popen([SCRIPT, *argv], cwd=PROJECT_DIR, env=script_env({"TERM": "dumb"}), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        raise Reject("script_failed", 500)
    remaining = length
    try:
        while remaining > 0:
            chunk = rfile.read(min(65536, remaining))
            if not chunk:
                break
            proc.stdin.write(chunk)
            remaining -= len(chunk)
    except (BrokenPipeError, OSError):
        pass   # die CLI hat aufgehört zu lesen: ihr Exit-Code und ihre Meldung zählen
    if remaining:
        # Die Verbindung ist vorzeitig abgerissen: die CLI darf den Rest NICHT als vollständig sehen (EOF würde eine abgeschnittene
        # letzte Zeile, etwa ein gekürztes Passwort, als gültig anwenden lassen), also beenden statt stdin zu schließen.
        proc.kill()
        proc.communicate()
        return {"rc": 1, "output": "", "error": "body_truncated"}
    try:
        out, err = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise Reject("timeout", 504)
    return {"rc": proc.returncode, "output": ANSI_RE.sub("", out.decode("utf-8", "replace"))[-16000:],
            "error": ANSI_RE.sub("", err.decode("utf-8", "replace"))[-4000:]}


_ha_timer = {"sync": None, "vip": None}   # Zeitpunkt (monotonic) des letzten Laufs; None = noch nie


def _timer_log(text: str) -> None:
    try:
        if HA_TIMER_LOG.exists() and HA_TIMER_LOG.stat().st_size > HA_TIMER_LOG_MAX:
            HA_TIMER_LOG.write_bytes(HA_TIMER_LOG.read_bytes()[-HA_TIMER_LOG_MAX // 2:])
        with HA_TIMER_LOG.open("a", encoding="utf-8") as log:
            log.write(f"{now_iso()} {text}\n")
    except OSError:
        pass


def sync_pairing_marker() -> None:
    """`ha-paired` in the state volume exists exactly while this node is paired with an agent. The backend reads it (the volume is mounted
    read-only there) to know whether a silent agent is a fault worth an alarm (ha_agent_down) or just a pair that never used one."""
    marker = STATE_DIR / "ha-paired"
    try:
        paired = bool(ha_store().cluster())
        if paired and not marker.exists():
            marker.write_text("1\n", encoding="utf-8")
        elif not paired and marker.exists():
            marker.unlink()
    except OSError:
        pass


def ha_timers_step(now: float) -> list[str]:
    """Ein Durchlauf der Zeitgeber (Aufrufer: ha_timer_loop, in Tests direkt). Gibt zurück, was lief.
    Nur an einem gekoppelten Knoten; nichts, solange ein Auftrag läuft (Promote setzt die VIP selbst, ein Update bringt Dienste um)."""
    ran: list[str] = []
    sync_pairing_marker()
    role = ha_role(read_env_file())
    if not role or not ha_store().cluster() or _lock.locked():
        return ran
    for name, interval, argv, timeout, only_role in (
            ("vip", HA_VIP_INTERVAL, ["ha", "vip", "reconcile"], 180, None),
            ("sync", HA_SYNC_INTERVAL, ["ha", "sync", "--quiet"], 900, "primary")):
        if only_role and role != only_role:
            continue
        last = _ha_timer[name]
        if last is not None and now - last < interval:
            continue
        _ha_timer[name] = now
        try:
            result = run_ha_sync(argv, timeout)
        except Reject as exc:
            _timer_log(f"{name}: {exc.code}")
        else:
            ran.append(name)
            if result["rc"] != 0:
                _timer_log(f"{name}: Exit {result['rc']}: {(result['error'] or result['output']).strip()[-300:]}")
    return ran


def ha_timer_loop() -> None:
    while True:
        try:
            ha_timers_step(time.monotonic())
        except Exception as exc:   # der Zeitgeber darf nie sterben
            _timer_log(f"Zeitgeber: {exc.__class__.__name__}: {exc}")
        time.sleep(10)


def ha_local_status() -> dict:
    """Für das Backend dieses Knotens (Token): Rolle, Kopplung, ob der Partner-Agent antwortet, und der letzte HA-Auftrag mit Protokoll."""
    store = ha_store()
    cluster = store.cluster()
    reachable = None
    if cluster:
        try:
            status, _ = ha_net.PeerClient(cluster["peer_ip"], cluster["peer_port"], cluster["peer_pin"], token=cluster["secret"], timeout=4).request("GET", "/ha/peer/ping")
            reachable = status == 200
        except (ha_net.PeerError, ha_net.PinMismatch):
            reachable = False
    job = read_ha_job()
    return {"ha_role": ha_role(read_env_file()), "paired": bool(cluster), "peer": cluster["peer_ip"] if cluster else None,
            "peer_reachable": reachable, "job": job or None, "log": ha_log_tail() if job else []}


def ha_local_promote(body: dict) -> dict:
    """Übernehmen am Standby, vom Backend dieses Knotens angestoßen (dort hat sich eine Person angemeldet). `force` nur, wenn der Partner
    nicht antwortet; dass das Backend das geprüft hat, ersetzt nicht die Prüfungen der CLI (`ha promote` verlangt Standby, Replica, Geheimnisse)."""
    if ha_role(read_env_file()) != "standby":
        raise Reject("not_standby")
    force, actor = body.get("force", False), body.get("actor")
    if not isinstance(force, bool) or not isinstance(actor, str) or not ACTOR_RE.fullmatch(actor):
        raise Reject("bad_request", 400)
    argv = ["ha", "promote", "--yes"] + (["--force"] if force else [])
    return start_ha_job("promote", argv, {"OPNEXUS_HA_ACTOR": actor})


def _text(body: dict, key: str, pattern: re.Pattern, optional: bool = False) -> str | None:
    value = body.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):   # fullmatch: `$` alone would let a trailing newline through
        raise Reject("bad_request", 400)
    return value


def ha_local_suggest(body: dict) -> dict:
    """Eigene Adresse und Schnittstelle in Richtung eines Ziels (Vorschlag für den Assistenten)."""
    target = _text(body, "target", IPV4_RE, optional=True) or "1.1.1.1"
    result = run_ha_sync(["ha", "suggest", target], 60)
    values = dict(line.split("=", 1) for line in result["output"].splitlines() if "=" in line)
    if result["rc"] != 0 or not values.get("node_ip"):
        raise Reject("suggest_failed", 502)
    return {"node_ip": values["node_ip"], "dev": values.get("dev", "")}


def ha_local_prepare(body: dict) -> dict:
    """Diesen Knoten zum Standby-Partner vorbereiten: Gate einschalten und einen Kopplungscode erzeugen (30 Minuten, einmal gültig).
    Der Code wird nur hier erzeugt und zurückgegeben, in kein Protokoll geschrieben."""
    ip = _text(body, "ip", IPV4_RE)
    if ha_role(read_env_file()):
        raise Reject("already_in_pair", 409)
    result = run_ha_sync(["ha", "prepare", "--gate-only", "--ip", ip], 240)
    if result["rc"] != 0:
        raise Reject("prepare_failed", 502)
    try:
        pin = ha_net.make_gate_cert(HA_GATE_DIR, ip)
    except (ha_net.PeerError, ValueError):
        raise Reject("prepare_failed", 502)
    secret = ha_store().open_pairing()
    return {"code": ha_agent.encode_code(ip, 8445, secret, bytes.fromhex(pin)), "expires_in": ha_agent.CODE_TTL, "ip": ip}


def ha_local_init(body: dict) -> dict:
    """Paar einrichten (Primär) bzw. nur vorprüfen (`check`): ein Auftrag mit Protokoll; der Code geht über stdin an die CLI."""
    if ha_role(read_env_file()):
        raise Reject("already_in_pair", 409)
    code, vip = _text(body, "code", CODE_RE), _text(body, "vip", CIDR_RE)
    node, peer_node = _text(body, "node", NODE_RE, optional=True), _text(body, "peer_node", NODE_RE, optional=True)
    node_ip, dev = _text(body, "node_ip", IPV4_RE, optional=True), _text(body, "vip_dev", DEV_RE, optional=True)
    check = body.get("check", False)
    if not isinstance(check, bool):
        raise Reject("bad_request", 400)
    argv = ["ha", "init", "--code", "-", "--vip", vip, "--yes"]
    for flag, value in (("--node", node), ("--peer-node", peer_node), ("--node-ip", node_ip), ("--vip-dev", dev)):
        if value:
            argv += [flag, value]
    if check:
        argv.append("--check")
    return start_ha_job("init-check" if check else "init", argv, stdin_text=code + "\n")


def ha_local_action(body: dict) -> dict:
    """Aktionen des Primärs: Abgleich jetzt, Wechsel zum Partner, Standby neu aufbauen, Paar auflösen."""
    action = body.get("action")
    if not isinstance(action, str) or action not in HA_ACTIONS:   # (a list or a dict is unhashable: it must not even reach the lookup)
        raise Reject("bad_request", 400)
    if ha_role(read_env_file()) != "primary":
        raise Reject("not_primary", 409)
    return start_ha_job(action, HA_ACTIONS[action])


class Reject(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def start_job(action: str, target: str | None) -> dict:
    if not _lock.acquire(blocking=False):
        raise Reject("busy")
    try:
        if read_job().get("state") == "running":
            raise Reject("busy")
        _status_cache["value"] = None
        live = live_status()
        if live["mode"] != "release":
            raise Reject("not_release_mode")
        if live["ha_role"]:
            # Updating one node of a pair by click would leave the standby behind the database revision, and a promote
            # would then fail. `opnexus ha update` does it in the right order (standby first).
            raise Reject("ha_active")
        if action == "update":
            current = parse_version(live["running_version"])
            wanted = parse_version(target)
            if wanted is None:
                raise Reject("invalid_version", 400)
            if current is None:
                raise Reject("backend_unreachable")
            if wanted <= current:
                raise Reject("not_newer")
            if wanted[0] != current[0]:
                raise Reject("major_change")
        elif action == "rollback":
            last = live["last_update"]
            if not (last and last.get("prev_version")):
                raise Reject("no_rollback")
        job = {
            "id": secrets.token_hex(8), "action": action, "target": target, "state": "running",
            "started_at": now_iso(), "finished_at": None, "exit_code": None,
            "from_version": live["running_version"],
        }
        for path in (LOG_FILE, PHASE_FILE):
            path.write_text("", encoding="utf-8")
        write_job(job)
    except BaseException:
        _lock.release()
        raise
    # Ab hier gehört das Lock dem Worker-Thread.
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job


def _run_job(job: dict) -> None:
    args = [SCRIPT, "update", job["target"]] if job["action"] == "update" else [SCRIPT, "rollback"]
    env = script_env({"OPNEXUS_PHASE_FILE": str(PHASE_FILE), "TERM": "dumb"})
    try:
        with LOG_FILE.open("ab") as log:
            proc = subprocess.Popen(args, cwd=PROJECT_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            code = proc.wait()
    except OSError as exc:
        code = 127
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"Updater: Skript konnte nicht gestartet werden: {exc}\n")
    if code == 0:
        state = "succeeded"
    elif code == 3 and job["action"] == "update":
        state = "rolled_back"
    else:
        state = "failed"
    job.update(state=state, exit_code=code, finished_at=now_iso())
    write_job(job)
    _status_cache["value"] = None
    try:
        if state == "succeeded" and job["action"] == "update":
            self_update()
    finally:
        _lock.release()


def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          cwd=PROJECT_DIR, env=script_env())


def self_update() -> None:
    """Den Updater-Container selbst auf das neue Image bringen.

    Läuft in einem eigenen, kurzlebigen Container: Würde der Updater sich aus
    sich selbst heraus neu erstellen, würde `docker compose` mitten im Vorgang
    mit ihm beendet. Das Ergebnis des Auftrags ist zu diesem Zeitpunkt längst
    gespeichert; schlägt dieser Schritt fehl, läuft der Updater einfach in der
    alten Version weiter und wird beim nächsten `docker compose up -d` erneuert.
    """
    def note(text: str) -> None:
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(text + "\n")

    try:
        me = os.environ.get("HOSTNAME", "")
        inspect = _docker("inspect", me, "--format",
                          '{{.Config.Image}}|{{range .Mounts}}{{if eq .Destination "' + str(PROJECT_DIR) + '"}}{{.Source}}{{end}}{{end}}')
        current_image, _, host_dir = inspect.stdout.strip().partition("|")
        if inspect.returncode != 0 or not host_dir.startswith("/"):
            note("Updater: eigenes Verzeichnis auf dem Host nicht ermittelbar — Updater-Container nicht erneuert.")
            return
        # Der Docker-Socket kann auf dem Host unter einem anderen Pfad liegen
        # als im Container (Rootless Docker: /run/user/<UID>/docker.sock).
        # Der Hilfscontainer braucht die QUELLE des Mounts — dieselbe Technik
        # wie beim Projektverzeichnis oben; nur lesend, kein Rückschluss auf
        # unbekannte Mounts. Ohne Fund (z. B. TCP statt Socket) bleibt der
        # klassische Pfad, wie bisher.
        socket_inspect = _docker("inspect", me, "--format",
                                 '{{range .Mounts}}{{if eq .Destination "' + DOCKER_SOCKET + '"}}{{.Source}}{{end}}{{end}}')
        host_socket = socket_inspect.stdout.strip() if socket_inspect.returncode == 0 else ""
        host_socket = host_socket if host_socket.startswith("/") else DOCKER_SOCKET
        images = _docker("compose", "config", "--images").stdout.split()
        new_image = next((i for i in images if "/opnexus-updater:" in i or i.startswith("opnexus-updater:")), "")
        if not new_image or new_image == current_image:
            return
        _docker("rm", "-f", "opnexus-updater-self-update")
        started = _docker(
            "run", "-d", "--rm", "--name", "opnexus-updater-self-update",
            "-v", f"{host_socket}:{DOCKER_SOCKET}", "-v", f"{host_dir}:{host_dir}", "-w", host_dir,
            "--entrypoint", "docker", new_image, "compose", "up", "-d", "--no-build", "--no-deps", "updater",
        )
        note("Updater: erneuert sich selbst auf " + new_image if started.returncode == 0
             else "Updater: Selbst-Update fehlgeschlagen: " + started.stderr.strip()[:300])
    except (OSError, subprocess.TimeoutExpired) as exc:
        note(f"Updater: Selbst-Update fehlgeschlagen: {exc}")


class Handler(BaseHTTPRequestHandler):
    server_version = "opnexus-updater"
    token = ""

    def log_message(self, fmt, *args):  # Zugriffsprotokoll auf stderr, ohne Query/Header
        sys.stderr.write("updater: %s %s\n" % (self.command, self.path.split("?", 1)[0]))

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        # Ein leeres Token darf nie "passen" (leerer Header == leeres Token).
        return bool(self.token) and hmac.compare_digest(supplied.encode(), self.token.encode())

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Reject("bad_request", 400)
        if length > MAX_BODY:
            raise Reject("bad_request", 413)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise Reject("bad_request", 400)
        if not isinstance(data, dict):
            raise Reject("bad_request", 400)
        return data

    def _ha(self, method: str, path: str) -> None:
        """Kopplung und Partner-Verben (nur über den ha-gate erreichbar; er setzt X-HA-Remote auf die Adresse des Gegenübers).
        Eigene Anmeldung: Geheimnis aus dem Kopplungscode bzw. Clustergeheimnis, nie das Token des Backends."""
        remote = self.headers.get("X-HA-Remote", "").strip()
        store = ha_store()
        try:
            if path in ("/ha/pair/hello", "/ha/pair/commit"):
                if method != "POST":
                    raise Reject("method_not_allowed", 405)
                if not ha_agent.authorize_pairing(store, self.headers):
                    raise Reject("unauthorized", 401)
                if path == "/ha/pair/hello":
                    self._body()
                    return self._send(200, ha_facts())
                return self._send(*self._ha_commit(store, remote))
            if not path.startswith("/ha/peer/"):
                raise Reject("not_found", 404)
            if not ha_agent.authorize_peer(store, self.headers, remote):
                raise Reject("unauthorized", 401)
            name = path[len("/ha/peer/"):]
            spec = ha_agent.VERBS.get(name)
            try:
                if spec is None:
                    raise ha_agent.VerbError("not_found", 404)
                if method != "POST":
                    params = {}
                elif spec.kind == "stream":
                    # Der Körper ist der Datenstrom; der eine Parameter (z. B. die Prüfsumme) kommt im Kopf X-HA-Param.
                    params = {spec.param: self.headers.get("X-HA-Param", "")} if spec.param else {}
                else:
                    params = self._body()
                verb, argv = ha_agent.build_argv(path, method, params)
            except ha_agent.VerbError as bad:
                raise Reject(bad.code, bad.status)
            if verb.kind == "local":
                if name == "ping":
                    return self._send(200, {"ok": True, "api": HA_API})
                job = read_ha_job()
                return self._send(200, {"job": job or None, "log": ha_log_tail() if job else []})
            if verb.kind == "job":
                return self._send(202, {"job": start_ha_job(name, argv)})
            if verb.kind == "sync":
                return self._send(200, run_ha_sync(argv, ha_agent.SYNC_TIMEOUTS.get(name, ha_agent.SYNC_TIMEOUT_DEFAULT)))
            # stream
            length = self._stream_length()
            if _lock.locked():
                raise Reject("busy")
            return self._send(200, run_ha_stream(argv, self.rfile, length))
        except Reject as rejected:
            return self._send(rejected.status, {"error": rejected.code})

    def _stream_length(self) -> int:
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            raise Reject("length_required", 411)
        if length > HA_MAX_STREAM:
            raise Reject("too_large", 413)
        return length

    def _ha_commit(self, store: ha_agent.Store, remote: str) -> tuple[int, dict]:
        """Zweiter Schritt der Kopplung (am künftigen Standby): Clustergeheimnis und Pin des Primärs übernehmen, dann beim
        Primär zurückfragen. Antwortet der nicht über sein Gate, ist die Kopplung wertlos und wird zurückgenommen."""
        with _commit_lock:
            return self._ha_commit_locked(store, remote)

    def _ha_commit_locked(self, store: ha_agent.Store, remote: str) -> tuple[int, dict]:
        body = self._body()
        try:
            ipaddress.IPv4Address(remote)
            secret, pin, port = str(body.get("cluster_secret", "")), str(body.get("peer_pin", "")), int(body.get("peer_port", 0))
        except (ValueError, TypeError):
            raise Reject("bad_request", 400)
        if store.cluster() or ha_role({}) or read_env_file().get("HA_ROLE"):
            raise Reject("already_paired", 409)
        try:
            store.set_cluster(secret, remote, port, pin)
        except ValueError:
            raise Reject("bad_request", 400)
        try:
            status, _ = ha_net.PeerClient(remote, port, pin, token=secret, timeout=10).request("GET", "/ha/peer/ping")
        except (ha_net.PeerError, ha_net.PinMismatch):
            status = 0
        if status != 200:
            store.clear_cluster()
            raise Reject("callback_failed", 502)
        store.close_pairing()
        return 200, {"ok": True, "api": HA_API}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, {"ok": True})
        if path.startswith("/ha/") and not path.startswith("/ha/local/"):
            return self._ha("GET", path)
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if path == "/status":
            return self._send(200, status_payload())
        if path == "/ha/local/status":
            return self._send(200, ha_local_status())
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/ha/") and not path.startswith("/ha/local/"):
            return self._ha("POST", path)
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            body = self._body()
            if path == "/ha/local/promote":
                return self._send(202, {"job": ha_local_promote(body)})
            if path == "/ha/local/suggest":
                return self._send(200, ha_local_suggest(body))
            if path == "/ha/local/prepare":
                return self._send(200, ha_local_prepare(body))
            if path == "/ha/local/init":
                return self._send(202, {"job": ha_local_init(body)})
            if path == "/ha/local/action":
                return self._send(202, {"job": ha_local_action(body)})
            if path == "/update":
                version = body.get("version")
                job = start_job("update", version if isinstance(version, str) else None)
            elif path == "/rollback":
                job = start_job("rollback", None)
            else:
                return self._send(404, {"error": "not_found"})
        except Reject as rejected:
            return self._send(rejected.status, {"error": rejected.code})
        self._send(202, {"job": job})


def mark_interrupted() -> None:
    """Ein Auftrag, der beim Start noch als "running" steht, hat nie geendet (Update- wie HA-Auftrag: sonst bliebe er für immer „busy“)."""
    job = read_job()
    if job.get("state") == "running":
        job.update(state="failed", finished_at=now_iso())
        write_job(job)
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write("Updater: wurde während des Auftrags neu gestartet — Zustand bitte prüfen (opnexus status).\n")
    ha_job = read_ha_job()
    if ha_job.get("state") == "running":
        ha_job.update(state="failed", finished_at=now_iso())
        write_ha_job(ha_job)
        with HA_LOG_FILE.open("a", encoding="utf-8") as log:
            log.write("Agent: wurde während des Auftrags neu gestartet — Zustand bitte prüfen (opnexus ha status).\n")


def probe_docker() -> None:
    """Startprüfung des Docker-Sockets: Der Healthcheck testet nur das HTTP-Ende,
    ein nicht nutzbarer Socket bliebe sonst bis zum ersten Auftrag unentdeckt —
    unter Rootless Docker ist ein falscher Mount-Pfad dann schwer einzuordnen."""
    try:
        proc = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0:
        print(f"updater: Docker-Socket erreichbar (Engine {proc.stdout.strip()}).", flush=True)
    else:
        print("updater: Docker-Socket nicht erreichbar — bei Rootless Docker "
              "DOCKER_HOST_SOCKET in der .env setzen ('opnexus updater enable' erkennt ihn).",
              file=sys.stderr, flush=True)


def main() -> None:
    Handler.token = load_token()
    mark_interrupted()
    probe_docker()
    threading.Thread(target=ha_timer_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"updater: listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
