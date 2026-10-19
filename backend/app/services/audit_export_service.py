"""Ships new audit events to syslog (RFC 5424, UDP / TCP / TLS) and/or a webhook.

A poll loop (main.py) calls ``run_once``: events after the stored cursor, oldest
first, in batches. A channel failing leaves the cursor where it is, so the same
events are tried again next round (at-least-once; every event carries its id so a
receiver can de-duplicate). If both channels are on, both must succeed to advance.

What is sent: who, what kind/action/status, which entity and host, the message and
a correlation id. NOT ``details`` — those hold before/after payloads of firewall
objects, which have no business in a log aggregator.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import socket
import ssl
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audit import AuditEvent
from app.models.audit_export import AuditExportSettings
from app.services import notification_secret_store
from app.version import APP_VERSION

logger = logging.getLogger(__name__)

SINGLETON_ID = 1
BATCH = 200
SECRET_NAME = "audit_webhook_signing_secret"
_FACILITY_LOCAL0 = 16
_BAD_STATUSES = {"denied", "rejected", "error", "revert_failed", "failed"}


class ExportError(Exception):
    pass


async def get_settings(db: AsyncSession) -> AuditExportSettings:
    row = await db.get(AuditExportSettings, SINGLETON_ID)
    if row is None:
        row = AuditExportSettings(
            id=SINGLETON_ID, syslog_enabled=False, syslog_port=514, syslog_protocol="udp",
            webhook_enabled=False, webhook_signing_secret_set=False,
        )
        db.add(row)
        await db.flush()
    return row


def event_dict(event: AuditEvent) -> dict:
    return {
        "id": str(event.id), "time": event.created_at.isoformat(), "correlation_id": event.correlation_id,
        "actor": event.actor, "kind": event.kind, "action": event.action, "status": event.status,
        "entity_id": event.entity_id, "entity_name": event.entity_name,
        "host_id": event.host_id, "host_name": event.host_name, "message": event.message,
    }


def syslog_line(event: dict, hostname: str = "opnexus") -> bytes:
    """RFC 5424: <PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID - MSG (JSON)."""
    severity = 4 if event["status"] in _BAD_STATUSES else 6
    pri = _FACILITY_LOCAL0 * 8 + severity
    return f"<{pri}>1 {event['time']} {hostname} opnexus - AUDIT - {json.dumps(event, ensure_ascii=False)}".encode()


def _frame_tcp(lines: list[bytes]) -> bytes:
    """RFC 6587 octet counting: '<len> <message>' per message."""
    return b"".join(str(len(line)).encode() + b" " + line for line in lines)


def _send_syslog_sync(row: AuditExportSettings, lines: list[bytes]) -> None:
    host, port = row.syslog_host, row.syslog_port
    if row.syslog_protocol == "udp":
        with socket.socket(socket.AF_INET6 if ":" in (host or "") else socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for line in lines:
                sock.sendto(line[:8000], (host, port))
        return
    with socket.create_connection((host, port), timeout=settings.opnsense_timeout) as raw:
        if row.syslog_protocol == "tls":
            context = ssl.create_default_context(cafile=settings.smtp_ca_file or None)
            with context.wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(_frame_tcp(lines))
        else:
            raw.sendall(_frame_tcp(lines))


async def send_syslog(row: AuditExportSettings, events: list[dict]) -> None:
    if not row.syslog_host:
        raise ExportError("Kein Syslog-Ziel konfiguriert")
    try:
        await asyncio.to_thread(_send_syslog_sync, row, [syslog_line(e) for e in events])
    except (OSError, ssl.SSLError) as exc:
        raise ExportError(f"Syslog-Zustellung fehlgeschlagen ({type(exc).__name__})") from None


async def send_webhook(row: AuditExportSettings, events: list[dict]) -> None:
    if not row.webhook_url:
        raise ExportError("Keine Webhook-URL konfiguriert")
    raw = json.dumps({"source": "opnexus", "version": APP_VERSION, "events": events}, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if row.webhook_signing_secret_set:
        secret = notification_secret_store.read_secret(SECRET_NAME)
        if not secret:
            raise ExportError("Signaturschlüssel ist konfiguriert, aber nicht lesbar – bitte neu setzen")
        headers["X-OPNexus-Signature"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=settings.opnsense_timeout) as client:
            (await client.post(row.webhook_url, content=raw, headers=headers)).raise_for_status()
    except httpx.HTTPError as exc:
        # The URL often is the credential: never put it in a message.
        reason = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
        raise ExportError(f"Webhook-Zustellung fehlgeschlagen ({reason})") from None


def active(row: AuditExportSettings) -> bool:
    return bool((row.syslog_enabled and row.syslog_host) or (row.webhook_enabled and row.webhook_url))


async def _ship(row: AuditExportSettings, events: list[dict]) -> None:
    if row.syslog_enabled and row.syslog_host:
        await send_syslog(row, events)
    if row.webhook_enabled and row.webhook_url:
        await send_webhook(row, events)


async def run_once(db: AsyncSession, lag_seconds: float = 3.0) -> int:
    """Ship one batch; returns how many events went out (0 when idle or failing).
    Only events at least ``lag_seconds`` old are shipped: an event is stamped when it
    is created, a moment before its transaction commits, and the cursor must not
    jump past one that becomes visible a little later."""
    row = await get_settings(db)
    if not active(row):
        return 0
    if row.cursor_created_at is None:
        # First round after enabling: start from now, do not replay history.
        row.cursor_created_at = datetime.now(timezone.utc)
        row.cursor_id = None
        await db.commit()
        return 0
    after = AuditEvent.created_at > row.cursor_created_at
    if row.cursor_id:
        after = or_(after, and_(AuditEvent.created_at == row.cursor_created_at, AuditEvent.id > uuid.UUID(row.cursor_id)))
    ripe = AuditEvent.created_at <= datetime.now(timezone.utc) - timedelta(seconds=lag_seconds)
    # Ordering by (created_at, id) must match the cursor comparison above.
    events = (await db.scalars(select(AuditEvent).where(after, ripe).order_by(AuditEvent.created_at, AuditEvent.id).limit(BATCH))).all()
    if not events:
        return 0
    try:
        await _ship(row, [event_dict(e) for e in events])
    except ExportError as exc:
        row.last_error, row.last_error_at = str(exc)[:300], datetime.now(timezone.utc)
        await db.commit()
        logger.warning("Audit-Export: %s", exc)
        return 0
    row.cursor_created_at, row.cursor_id = events[-1].created_at, str(events[-1].id)
    row.last_success_at, row.last_error = datetime.now(timezone.utc), None
    await db.commit()
    return len(events)


async def send_test(row: AuditExportSettings) -> None:
    event = {
        "id": "00000000-0000-0000-0000-000000000000", "time": datetime.now(timezone.utc).isoformat(),
        "correlation_id": "test", "actor": "opnexus", "kind": "test", "action": "test", "status": "accepted",
        "entity_id": None, "entity_name": None, "host_id": None, "host_name": None,
        "message": "Dies ist ein Test des Audit-Exports von OPNexus.",
    }
    if not (row.syslog_enabled or row.webhook_enabled):
        raise ExportError("Kein Kanal eingeschaltet")
    await _ship(row, [event])
