"""Prüft, ob eine neuere OPNexus-Version veröffentlicht wurde.

Fragt (höchstens alle `update_check_interval_hours`) den konfigurierten
Release-Feed ab (GitHub/Forgejo-`releases/latest`-JSON) und vergleicht
`tag_name` mit der laufenden Version. Das Ergebnis ist rein informativ — die
App aktualisiert sich nie selbst (das macht `scripts/opnexus` auf dem Host).

Bewusst robust gegen einen kaputten/unerreichbaren Feed: Fehler werden
gemerkt und frühestens nach `_RETRY_AFTER` erneut versucht, der letzte
bekannte Stand bleibt erhalten, und nichts davon darf je einen Request
scheitern lassen oder länger als `_TIMEOUT_SECONDS` blockieren.
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.version import APP_VERSION

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0
_MAX_BYTES = 1_000_000
_RETRY_AFTER = timedelta(minutes=15)
# Nur stabile Versionen "X.Y.Z" gelten als Update; Vorabversionen (1.10.0-rc1)
# werden nie vorgeschlagen.
_STABLE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass
class _State:
    attempted_at: datetime | None = None
    checked_at: datetime | None = None
    latest: str | None = None
    release_url: str | None = None
    error: str | None = None


_state = _State()
_lock = asyncio.Lock()


def reset_cache() -> None:
    """Nur für Tests."""
    global _state
    _state = _State()


def _parse(tag: str) -> tuple[int, int, int] | None:
    match = _STABLE_TAG.match(tag.strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _safe_url(value: object) -> str | None:
    """Nur http(s)-Links weitergeben — der Wert landet als href im Browser."""
    if isinstance(value, str) and re.match(r"^https?://[^\s<>\"']+$", value) and len(value) <= 500:
        return value
    return None


async def _fetch() -> tuple[str, str | None]:
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True, max_redirects=3) as http:
        response = await http.get(
            settings.update_check_url,
            headers={"Accept": "application/json", "User-Agent": f"OPNexus/{APP_VERSION}"},
        )
    response.raise_for_status()
    if len(response.content) > _MAX_BYTES:
        raise ValueError("response too large")
    data = response.json()
    tag = data["tag_name"]
    if not isinstance(tag, str):
        raise ValueError("tag_name is not a string")
    return tag, _safe_url(data.get("html_url"))


async def check() -> dict:
    """Aktueller Stand für GET /api/system/version-check."""
    enabled = bool(settings.update_check_url.strip())
    current = _parse(APP_VERSION)
    result = {
        "enabled": enabled,
        "current": APP_VERSION,
        "latest": None,
        "update_available": False,
        "release_url": None,
        "checked_at": None,
        "error": None,
    }
    if not enabled:
        return result

    async with _lock:
        now = datetime.now(timezone.utc)
        interval = timedelta(hours=max(1, settings.update_check_interval_hours))
        fresh = _state.checked_at is not None and now - _state.checked_at < interval
        backing_off = _state.error is not None and _state.attempted_at is not None and now - _state.attempted_at < _RETRY_AFTER
        if not fresh and not backing_off:
            _state.attempted_at = now
            try:
                tag, url = await _fetch()
                if _parse(tag) is None:
                    # z. B. eine Vorabversion — kein Fehler, nur kein Update.
                    _state.latest, _state.release_url = None, None
                else:
                    _state.latest, _state.release_url = tag.lstrip("v"), url
                _state.checked_at, _state.error = now, None
            except Exception as exc:  # Netz, HTTP, JSON, Format: alles nur "geht gerade nicht"
                logger.warning("Update-Check fehlgeschlagen: %s", exc.__class__.__name__)
                _state.error = "unavailable"

    latest = _parse(_state.latest) if _state.latest else None
    result.update(
        latest=_state.latest,
        update_available=bool(latest and current and latest > current),
        release_url=_state.release_url,
        checked_at=_state.checked_at.isoformat() if _state.checked_at else None,
        error=_state.error,
    )
    return result
