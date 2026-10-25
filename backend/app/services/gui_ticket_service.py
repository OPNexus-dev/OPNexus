"""Tickets and gateway sessions for the firewall GUI access (1.27, gui-gateway container).

Flow (see docs in routers/gui_access.py):
* An admin asks for a ticket (POST /api/hosts/{id}/gui-ticket). 32 random bytes, valid 30 s, single use;
  only its SHA-256 is stored, bound to the OPNexus session (same hash as sessions.token_hash), the user,
  the host and a slot (gateway port = its own browser origin).
* The gateway redeems it through /internal/gui/redeem and gets what it needs to reach the firewall —
  base URL, pinned CA, overlay proxy, timeouts. Never the API key or secret.
* Every 20 s the gateway re-checks the gateway session (/internal/gui/revalidate): OPNexus session still
  valid, user still admin, switch still on, host still eligible, absolute limit not reached.

A slot stays assigned to one firewall for as long as that browser session has the firewall open; another
firewall gets another slot (another origin), so two firewall GUIs never share an origin at the same time.
After the last gateway session of a slot ended, the slot goes to a DIFFERENT firewall only after SLOT_COOLDOWN
(1.29, S1): a popup of a possibly compromised firewall GUI may still be open and its cookies belong to that
origin. The same firewall may take the slot again at once.

The target the gateway talks to (origin, pinned CA, overlay proxy) is fingerprinted at redeem time; every
re-check compares it with the host's current configuration and ends the session when it changed (S4).
"""
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.singleton import get_or_create
from app.config import settings
from app.errors import ErrorCode
from app.models.gui_access import GuiAccessSettings, GuiSession, GuiTicket
from app.models.host import OpnsenseHost
from app.models.user import AdminUser, Session
from app.services import audit_service, firewall_url, overlay_service

TICKET_TTL = timedelta(seconds=30)
# 20 s: a switch-off, logout or role change must end a gateway session within 30 s of the change, and
# the last re-check can lie up to one interval before it (live test 2026-10-21: 30 s gave 31 s).
REVALIDATE_SECONDS = 20
# A gateway session nobody re-checked for this long is treated as ended (gateway crashed without telling us).
SESSION_STALE_AFTER = timedelta(seconds=REVALIDATE_SECONDS * 3)
MAX_SLOTS = 4
# A slot freed by firewall A goes to firewall B no earlier than this long after A's session ended.
SLOT_COOLDOWN = timedelta(minutes=5)
# Open gateway sessions at once (the gateway container enforces the same number; test_gui_access checks they match).
MAX_GATEWAY_SESSIONS = 64
MIN_GATEWAY_TOKEN_LENGTH = 32
AUDIT_KIND = "gui_access"


@dataclass
class GuiAccessError(Exception):
    status: int
    code: ErrorCode
    params: dict


def hash_value(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── configuration ─────────────────────────────────────────────────────────

def slot_sites() -> dict[int, list[str]]:
    """Slot port -> configured https origins with that port (OPNEXUS_GUI_SITES)."""
    sites: dict[int, list[str]] = {}
    for origin in settings.gui_site_origins:
        try:
            parts = urlsplit(origin)
            port = parts.port
        except ValueError:
            continue
        if parts.scheme != "https" or not parts.hostname or not port or parts.path not in ("", "/"):
            continue
        sites.setdefault(port, []).append(f"https://{parts.netloc}")
    return sites


def gateway_configured() -> bool:
    return len(settings.gui_gateway_token) >= MIN_GATEWAY_TOKEN_LENGTH and bool(slot_sites())


def unavailable_reasons() -> list[str]:
    reasons = []
    if not settings.cookie_secure:
        reasons.append("cookie_secure")
    if len(settings.gui_gateway_token) < MIN_GATEWAY_TOKEN_LENGTH:
        reasons.append("gateway_token")
    if not slot_sites():
        reasons.append("gui_sites")
    return reasons


def active_slots(row: GuiAccessSettings) -> list[int]:
    return sorted(slot_sites())[: max(1, min(row.slot_count, MAX_SLOTS))]


async def get_settings(db: AsyncSession) -> GuiAccessSettings:
    return await get_or_create(
        db, GuiAccessSettings, enabled=False, idle_timeout_minutes=15, absolute_timeout_minutes=60, slot_count=MAX_SLOTS)


def host_refusal(host: OpnsenseHost) -> str | None:
    """Why this firewall must not be opened through the gateway (None = eligible)."""
    if not host.enabled:
        return "disabled"
    if urlsplit(host.base_url.strip()).scheme != "https" or firewall_url.check_base_url(host.base_url):
        return "https"
    if not host.verify_tls:
        return "tls"
    return None


def firewall_origin(base_url: str) -> str:
    parts = urlsplit(base_url.strip())
    return f"https://{parts.netloc.rsplit('@', 1)[-1]}"


def target_fingerprint(host: OpnsenseHost) -> str:
    """What a gateway session talks to: origin, pinned CA and overlay route. Changes -> the session must end."""
    material = "\n".join([firewall_origin(host.base_url), host.tls_ca_pem or "", overlay_service.proxy_for(host.base_url) or "",
                          "verify" if host.verify_tls else "noverify"])
    return hash_value(material)


# ── audit ─────────────────────────────────────────────────────────────────

async def _audit(actor: str, *, action: str, status: str, host: OpnsenseHost | None = None,
                 host_id=None, entity_id=None, message: str | None = None, details: dict | None = None) -> None:
    token = audit_service.context.set({"actor": actor, "id": str(uuid.uuid4()), "action": action})
    try:
        await audit_service.persist(
            kind=AUDIT_KIND, action=action, status=status, entity_id=str(entity_id) if entity_id else None,
            host_id=str(host.id) if host is not None else (str(host_id) if host_id else None),
            host_name=host.name if host is not None else None, message=message, details=details or {},
        )
    finally:
        audit_service.context.reset(token)


# ── tickets ───────────────────────────────────────────────────────────────

def _redeem_origin(slot: int, request_host: str) -> str:
    candidates = slot_sites().get(slot, [])
    wanted = (urlsplit(f"//{request_host}").hostname or "").lower()
    for origin in candidates:
        if (urlsplit(origin).hostname or "").lower() == wanted:
            return origin
    return candidates[0]


async def _pick_slot(db: AsyncSession, row: GuiAccessSettings, session_hash: str, user_id, host_id) -> int:
    now = _now()
    slots = active_slots(row)
    absolute = timedelta(minutes=row.absolute_timeout_minutes)
    in_use: dict[int, uuid.UUID] = {}
    open_sessions = await db.scalars(select(GuiSession).where(
        GuiSession.session_hash == session_hash, GuiSession.ended_at.is_(None), GuiSession.started_at > now - absolute))
    for gs in open_sessions:
        in_use[gs.slot] = gs.host_id
    pending = await db.scalars(select(GuiTicket).where(
        GuiTicket.session_hash == session_hash, GuiTicket.redeemed_at.is_(None), GuiTicket.expires_at > now))
    for ticket in pending:
        in_use.setdefault(ticket.slot, ticket.host_id)
    for slot in slots:
        if in_use.get(slot) == host_id:
            return slot  # this firewall is already open in this browser session: same origin again
    free = [s for s in slots if s not in in_use]
    if not free:
        raise GuiAccessError(409, ErrorCode.GUI_NO_FREE_SLOT, {"slots": str(len(slots))})
    # S1: a slot this user's browser may still have a window of ANOTHER firewall on stays closed to this
    # firewall — while a gateway session of the user is open there (any browser session) and for SLOT_COOLDOWN
    # after the last one ended. The same firewall may take it again at once.
    held = (await db.execute(
        select(GuiSession.slot, GuiSession.host_id, GuiSession.ended_at, GuiSession.last_checked_at).where(
            GuiSession.user_id == user_id,
            ((GuiSession.ended_at.is_(None)) & ((GuiSession.started_at > now - absolute) | (GuiSession.last_checked_at > now - SLOT_COOLDOWN)))
            | (GuiSession.ended_at > now - SLOT_COOLDOWN)))).all()
    cooling: dict[int, datetime] = {}  # slot -> when it may go to another firewall
    for slot, h_id, ended_at, checked_at in held:
        if h_id == host_id or slot not in free:
            continue
        if ended_at is None and checked_at is not None and now - checked_at > SESSION_STALE_AFTER:
            ended_at = checked_at  # the gateway stopped re-checking it (crash, kill): it ended when it was last seen
        release = (ended_at + SLOT_COOLDOWN) if ended_at is not None else now + SLOT_COOLDOWN
        if release <= now:
            continue  # cooled down long ago (a session that stopped answering a while back)
        cooling[slot] = max(cooling.get(slot, release), release)
    usable = [s for s in free if s not in cooling]
    if not usable:
        wait = min(cooling.values()) - now
        raise GuiAccessError(409, ErrorCode.GUI_SLOT_COOLING_DOWN,
                             {"minutes": str(max(1, -(-int(wait.total_seconds()) // 60)))})
    free = usable
    # Prefer the slot this user had this firewall on before, then the slot the user used least recently
    # (an old tab of another firewall on a reused origin is the documented residual risk).
    history = (await db.execute(
        select(GuiSession.slot, GuiSession.host_id, func.max(GuiSession.started_at))
        .where(GuiSession.user_id == user_id).group_by(GuiSession.slot, GuiSession.host_id)
    )).all()
    for slot, h_id, _ in sorted(history, key=lambda r: r[2], reverse=True):
        if h_id == host_id and slot in free:
            return slot
    last_used = {}
    for slot, _, started in history:
        last_used[slot] = max(last_used.get(slot, started), started)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return min(free, key=lambda s: (last_used.get(s, epoch), s))


async def issue_ticket(db: AsyncSession, *, user: AdminUser, session_token: str, host: OpnsenseHost,
                       request_host: str) -> dict:
    row = await get_settings(db)
    now = _now()
    await db.execute(delete(GuiTicket).where(GuiTicket.expires_at < now - timedelta(hours=1)))
    session_hash = hash_value(session_token)
    slot = await _pick_slot(db, row, session_hash, user.id, host.id)
    # Reopening a slot in the same browser session replaces its earlier gateway session, so that one does not count.
    open_count = await db.scalar(select(func.count()).select_from(GuiSession).where(
        GuiSession.ended_at.is_(None),
        GuiSession.started_at > now - timedelta(minutes=row.absolute_timeout_minutes),
        ~((GuiSession.session_hash == session_hash) & (GuiSession.slot == slot))))
    if (open_count or 0) >= MAX_GATEWAY_SESSIONS:
        raise GuiAccessError(409, ErrorCode.GUI_TOO_MANY_SESSIONS, {"max": str(MAX_GATEWAY_SESSIONS)})
    raw = secrets.token_urlsafe(32)
    db.add(GuiTicket(ticket_hash=hash_value(raw), session_hash=session_hash, user_id=user.id, host_id=host.id,
                     slot=slot, created_at=now, expires_at=now + TICKET_TTL))
    await db.commit()
    await _audit(user.username, action="ticket", status="accepted", host=host, details={"slot": slot})
    return {"url": f"{_redeem_origin(slot, request_host)}/__onx/redeem?t={raw}", "slot": slot,
            "expires_in": int(TICKET_TTL.total_seconds())}


# ── gateway side (internal endpoints) ─────────────────────────────────────

async def _live_check(db: AsyncSession, row: GuiAccessSettings, session_hash: str, user_id, host_id,
                      slot: int) -> tuple[str | None, AdminUser | None, OpnsenseHost | None]:
    session = await db.scalar(select(Session).where(Session.token_hash == session_hash))
    if session is None or session.expires_at <= _now() or session.user_id != user_id:
        return "session", None, None
    user = await db.get(AdminUser, user_id)
    if user is None or user.role != "admin":
        return "role", user, None
    if not row.enabled:
        return "disabled", user, None
    if not settings.cookie_secure or not gateway_configured():
        return "config", user, None
    host = await db.get(OpnsenseHost, host_id)
    if host is None or host_refusal(host):
        return "host", user, host
    if slot not in active_slots(row):
        return "slot", user, host
    return None, user, host


def _timing(row: GuiAccessSettings, started_at: datetime) -> dict:
    deadline = started_at + timedelta(minutes=row.absolute_timeout_minutes)
    return {"idle_timeout_seconds": row.idle_timeout_minutes * 60,
            "absolute_deadline": int(deadline.timestamp()),
            "revalidate_seconds": REVALIDATE_SECONDS}


async def redeem(db: AsyncSession, *, ticket_hash: str, session_hash: str, slot: int, client_ip: str | None) -> dict:
    """{'ok': True, ...upstream data...} or {'ok': False, 'reason': ...}. Any attempt burns the ticket."""
    now = _now()
    ticket = await db.scalar(select(GuiTicket).where(GuiTicket.ticket_hash == ticket_hash).with_for_update())
    if ticket is None:
        await db.rollback()
        return {"ok": False, "reason": "unknown"}
    replayed = ticket.redeemed_at is not None
    ticket.redeemed_at = ticket.redeemed_at or now
    row = await get_settings(db)
    reason = None
    if replayed:
        reason = "replayed"
    elif ticket.expires_at <= now:
        reason = "expired"
    elif not secrets.compare_digest(ticket.session_hash, session_hash):
        reason = "session"
    elif ticket.slot != slot:
        reason = "slot"
    user = host = None
    if reason is None:
        reason, user, host = await _live_check(db, row, ticket.session_hash, ticket.user_id, ticket.host_id, slot)
    if user is None:
        user = await db.get(AdminUser, ticket.user_id)
    if host is None:
        host = await db.get(OpnsenseHost, ticket.host_id)
    actor = user.username if user is not None else "unknown"
    if reason is not None:
        await db.commit()
        await _audit(actor, action="redeem", status="denied", host=host, host_id=ticket.host_id,
                     message=reason, details={"slot": slot, "client_ip": client_ip, "reason": reason})
        return {"ok": False, "reason": reason}
    # The same browser session reopening this slot replaces the earlier gateway session there.
    superseded = (await db.scalars(select(GuiSession).where(
        GuiSession.session_hash == session_hash, GuiSession.slot == slot, GuiSession.ended_at.is_(None)))).all()
    if any(old.host_id != host.id for old in superseded):
        # Two quick tickets both saw the slot free: the second must not take over another firewall's origin
        # without the cooling-down time (S1).
        await db.commit()
        await _audit(actor, action="redeem", status="denied", host=host, host_id=ticket.host_id,
                     message="slot", details={"slot": slot, "client_ip": client_ip, "reason": "slot"})
        return {"ok": False, "reason": "slot"}
    for old in superseded:
        old.ended_at, old.end_reason = now, "superseded"
    gs = GuiSession(session_hash=session_hash, user_id=user.id, host_id=host.id, slot=slot,
                    client_ip=(client_ip or None) and client_ip[:45], started_at=now, last_checked_at=now)
    db.add(gs)
    await db.commit()
    await _audit(actor, action="redeem", status="accepted", host=host, entity_id=gs.id,
                 details={"slot": slot, "client_ip": client_ip})
    return {
        "ok": True, "gui_session_id": str(gs.id), "host_id": str(host.id), "host_name": host.name,
        "upstream_origin": firewall_origin(host.base_url),
        "ca_pem": host.tls_ca_pem or None,  # public certificate (pin); None = system trust store
        "proxy_url": overlay_service.proxy_for(host.base_url),
        "target_fp": target_fingerprint(host),
        **_timing(row, now),
    }


async def _finish(db: AsyncSession, gs: GuiSession, reason: str) -> None:
    now = _now()
    if gs.ended_at is None:
        gs.ended_at, gs.end_reason = now, reason[:32]
    await db.commit()
    user = await db.get(AdminUser, gs.user_id)
    host = await db.get(OpnsenseHost, gs.host_id)
    await _audit(user.username if user else "unknown", action="end", status="accepted", host=host,
                 host_id=gs.host_id, entity_id=gs.id, message=reason,
                 details={"slot": gs.slot, "client_ip": gs.client_ip, "reason": reason,
                          "duration_seconds": int((gs.ended_at - gs.started_at).total_seconds())})


async def revalidate(db: AsyncSession, *, gui_session_id: uuid.UUID, session_hash: str,
                     target_fp: str | None = None) -> dict:
    gs = await db.get(GuiSession, gui_session_id)
    if gs is None or not secrets.compare_digest(gs.session_hash, session_hash):
        return {"ok": False, "reason": "unknown"}
    if gs.ended_at is not None:
        return {"ok": False, "reason": gs.end_reason or "ended"}
    row = await get_settings(db)
    if _now() >= gs.started_at + timedelta(minutes=row.absolute_timeout_minutes):
        await _finish(db, gs, "absolute")
        return {"ok": False, "reason": "absolute"}
    reason, _, host = await _live_check(db, row, gs.session_hash, gs.user_id, gs.host_id, gs.slot)
    # S4: the gateway presents the fingerprint it got at redeem time. Firewall address, pinned certificate or
    # overlay route changed since -> the session (and the upstream connection it holds) ends now.
    if reason is None and target_fp is not None and host is not None \
            and not secrets.compare_digest(target_fp, target_fingerprint(host)):
        reason = "target"
    if reason is not None:
        await _finish(db, gs, reason)
        return {"ok": False, "reason": reason}
    gs.last_checked_at = _now()
    await db.commit()
    return {"ok": True, **_timing(row, gs.started_at)}


async def end(db: AsyncSession, *, gui_session_id: uuid.UUID, session_hash: str, reason: str) -> bool:
    gs = await db.get(GuiSession, gui_session_id)
    if gs is None or not secrets.compare_digest(gs.session_hash, session_hash):
        return False
    if gs.ended_at is None:
        await _finish(db, gs, reason)
    return True


async def end_all(db: AsyncSession, reason: str) -> int:
    """Switch turned off: mark every open gateway session ended (the gateway drops them on its next check)."""
    result = await db.execute(update(GuiSession).where(GuiSession.ended_at.is_(None))
                              .values(ended_at=_now(), end_reason=reason[:32]))
    return result.rowcount or 0
