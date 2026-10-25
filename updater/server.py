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
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE_DIR = Path(os.environ.get("UPDATER_STATE_DIR", "/state"))
PROJECT_DIR = Path(os.environ.get("OPNEXUS_DIR", "/project"))
SCRIPT = os.environ.get("OPNEXUS_SCRIPT", "/usr/local/bin/opnexus")
PORT = int(os.environ.get("UPDATER_PORT", "8080"))
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
        "ha_role": env_file.get("HA_ROLE") or None,  # set by `opnexus ha init`: this node is part of an HA pair
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
        images = _docker("compose", "config", "--images").stdout.split()
        new_image = next((i for i in images if "/opnexus-updater:" in i or i.startswith("opnexus-updater:")), "")
        if not new_image or new_image == current_image:
            return
        _docker("rm", "-f", "opnexus-updater-self-update")
        started = _docker(
            "run", "-d", "--rm", "--name", "opnexus-updater-self-update",
            "-v", "/var/run/docker.sock:/var/run/docker.sock", "-v", f"{host_dir}:{host_dir}", "-w", host_dir,
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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, {"ok": True})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if path == "/status":
            return self._send(200, status_payload())
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            body = self._body()
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
    """Ein Auftrag, der beim Start noch als "running" steht, hat nie geendet."""
    job = read_job()
    if job.get("state") == "running":
        job.update(state="failed", finished_at=now_iso())
        write_job(job)
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write("Updater: wurde während des Auftrags neu gestartet — Zustand bitte prüfen (opnexus status).\n")


def main() -> None:
    Handler.token = load_token()
    mark_interrupted()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"updater: listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
