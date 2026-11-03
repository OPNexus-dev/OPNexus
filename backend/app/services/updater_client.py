"""Client für den optionalen OPNexus-Updater-Container (updater/server.py).

Das Backend kann selbst nichts am Host ändern und soll das auch nicht können:
Es fragt den Updater-Dienst nach dem Zustand und reicht eine bereits
geprüfte Update-/Rollback-Anfrage weiter. Ist der Dienst nicht aktiviert oder
nicht erreichbar, ist das ein normaler Zustand ("nicht verfügbar"), kein Fehler.
"""
import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_STATUS_TIMEOUT = 50.0  # der Updater fragt docker ab (bis zu 45 s)
_ACTION_TIMEOUT = 15.0


class UpdaterUnavailable(Exception):
    """Updater nicht aktiviert (kein Token) oder nicht erreichbar."""


class UpdaterRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _token() -> str:
    try:
        token = Path(settings.updater_token_file).read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        raise UpdaterUnavailable("not_enabled")
    return token


async def _call(method: str, path: str, payload: dict | None, timeout: float) -> dict:
    headers = {"Authorization": f"Bearer {_token()}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.request(method, settings.updater_url.rstrip("/") + path, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.info("Updater nicht erreichbar: %s", exc.__class__.__name__)
        raise UpdaterUnavailable("unreachable") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 401:
        # Token passt nicht (Volume neu angelegt?) — wie "nicht verfügbar" behandeln.
        raise UpdaterUnavailable("unauthorized")
    if response.status_code >= 400:
        raise UpdaterRejected(str(body.get("error") or "rejected"))
    return body


async def status() -> dict:
    return await _call("GET", "/status", None, _STATUS_TIMEOUT)


async def start_update(version: str) -> dict:
    return await _call("POST", "/update", {"version": version}, _ACTION_TIMEOUT)


async def start_rollback() -> dict:
    return await _call("POST", "/rollback", {}, _ACTION_TIMEOUT)


# HA (docs/DESIGN-HA-UI.md): the updater of this node is the agent of the pair. Only the standby's own door and (later) the
# pair card in Settings talk to these; the partner never reaches them (the gate forwards only /ha/pair and /ha/peer).
async def ha_local_status() -> dict:
    return await _call("GET", "/ha/local/status", None, _STATUS_TIMEOUT)


async def ha_local_suggest(target: str | None) -> dict:
    return await _call("POST", "/ha/local/suggest", {"target": target} if target else {}, _STATUS_TIMEOUT)


async def ha_local_prepare(ip: str) -> dict:
    return await _call("POST", "/ha/local/prepare", {"ip": ip}, 260.0)   # brings the gate up: image pull and start may take a while


async def ha_local_init(body: dict) -> dict:
    return await _call("POST", "/ha/local/init", body, _ACTION_TIMEOUT)


async def ha_local_action(action: str) -> dict:
    return await _call("POST", "/ha/local/action", {"action": action}, _ACTION_TIMEOUT)


async def ha_local_promote(force: bool, actor: str) -> dict:
    return await _call("POST", "/ha/local/promote", {"force": force, "actor": actor}, _ACTION_TIMEOUT)
