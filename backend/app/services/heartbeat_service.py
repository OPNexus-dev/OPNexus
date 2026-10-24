"""Heartbeat to an external watcher: who watches the watcher (docs/HA-PLAN.md, 2.7).

If OPNexus stops, every alert it would send stops with it. With HEARTBEAT_URL set, the leader's notification loop
calls that URL after each successful check cycle; a watcher outside OPNexus (healthchecks.io style, ntfy, Uptime
Kuma push monitor) raises the alarm when the calls stop. Only the leader sends (the loop runs only there), and only
after a cycle that reached the database and finished its checks: a backend that is up but cut off from PostgreSQL
must not report "fine".

The URL usually carries a secret (the check id), so it is validated like a notification target, called through the
guarded client without redirects and never written to the log.
"""
import logging
from urllib.parse import urlsplit

import httpx

from app import runtime_settings
from app.config import settings
from app.services import url_guard
from app.version import APP_VERSION

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10.0
_warned = False


def configured_url() -> str | None:
    """The validated URL, or None (off or invalid; an invalid one is reported once)."""
    global _warned
    raw = runtime_settings.value("heartbeat_url").strip()
    if not raw:
        return None
    try:
        url = url_guard.validate_url(raw, max_length=2000)
        if urlsplit(url).scheme != "https":
            raise ValueError("Die Adresse muss mit https:// beginnen")
    except ValueError as exc:
        if not _warned:
            logger.warning("HEARTBEAT_URL wird ignoriert: %s", exc)
            _warned = True
        return None
    return url


async def ping() -> bool:
    url = configured_url()
    if url is None:
        return False
    with url_guard.muted():
        try:
            await url_guard.ensure_resolved_safe(url)
            async with url_guard.guarded_client(timeout=_TIMEOUT_SECONDS, follow_redirects=False) as client:
                (await client.get(url, headers={"User-Agent": f"OPNexus/{APP_VERSION}"})).raise_for_status()
        except ValueError as exc:
            logger.warning("Heartbeat nicht gesendet: %s", exc)
            return False
        except httpx.HTTPError as exc:
            logger.warning("Heartbeat fehlgeschlagen (%s)", url_guard.failure_reason(exc))
            return False
    return True
