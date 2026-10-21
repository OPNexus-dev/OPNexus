"""Firewall GUI access through the gui-gateway container (1.27).

Public API (session cookie, admin only, never API tokens):
* POST /api/hosts/{id}/gui-ticket — one-time ticket + the slot URL the browser opens (noopener).
* GET/PUT /api/settings/gui-access — switch, idle/absolute timeout, number of slots.

Internal API for the gateway only (NOT under /api, so the proxy never forwards it; shared secret
GUI_GATEWAY_TOKEN in the X-Onx-Gateway-Token header, compared in constant time):
* POST /internal/gui/redeem, /internal/gui/revalidate, /internal/gui/end.
The answers never contain firewall credentials: only the firewall origin, its pinned (public) CA
certificate, the overlay proxy address and the timeouts.
"""
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.models.user import AdminUser
from app.routers.auth import SECURE_COOKIE_NAME, current_user
from app.services import api_token_service, gui_ticket_service as svc

router = APIRouter(tags=["gui-access"])
internal_router = APIRouter(prefix="/internal/gui", include_in_schema=False)


def _require_admin_session(request: Request, user: AdminUser) -> None:
    if getattr(request.state, "api_token", None) is not None or api_token_service.bearer_from_header(
        request.headers.get("authorization", "")
    ) is not None:
        raise api_error(status.HTTP_403_FORBIDDEN, ErrorCode.GUI_SESSION_REQUIRED)
    if user.role != "admin":
        raise api_error(status.HTTP_403_FORBIDDEN, ErrorCode.ADMIN_REQUIRED)


class GuiTicketRead(BaseModel):
    url: str
    slot: int
    expires_in: int


@router.post("/hosts/{host_id}/gui-ticket", response_model=GuiTicketRead)
async def create_ticket(host_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
                        user: AdminUser = Depends(current_user)):
    _require_admin_session(request, user)
    if not svc.gateway_configured():
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.GUI_GATEWAY_NOT_CONFIGURED)
    session_cookie = request.cookies.get(SECURE_COOKIE_NAME, "")
    if not settings.cookie_secure or not session_cookie:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.GUI_REQUIRES_HTTPS)
    row = await svc.get_settings(db)
    if not row.enabled:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.GUI_ACCESS_DISABLED)
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(status.HTTP_404_NOT_FOUND, ErrorCode.HOST_NICHT_GEFUNDEN)
    refusal = svc.host_refusal(host)
    if refusal:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.GUI_HOST_NOT_ELIGIBLE, reason=refusal)
    try:
        return await svc.issue_ticket(db, user=user, session_token=session_cookie, host=host,
                                      request_host=request.headers.get("host", ""))
    except svc.GuiAccessError as exc:
        raise api_error(exc.status, exc.code, **exc.params)


class GuiAccessSettingsRead(BaseModel):
    enabled: bool
    idle_timeout_minutes: int
    absolute_timeout_minutes: int
    slot_count: int
    available: bool
    unavailable_reasons: list[str]
    slots: list[int]
    updated_by: str | None
    updated_at: datetime | None


class GuiAccessSettingsUpdate(BaseModel):
    enabled: bool
    idle_timeout_minutes: int = Field(ge=1, le=240)
    absolute_timeout_minutes: int = Field(ge=5, le=720)
    slot_count: int = Field(ge=1, le=svc.MAX_SLOTS)

    @model_validator(mode="after")
    def _idle_within_absolute(self):
        if self.idle_timeout_minutes > self.absolute_timeout_minutes:
            raise ValueError("idle_timeout_minutes must not exceed absolute_timeout_minutes")
        return self


def _settings_read(row) -> dict:
    reasons = svc.unavailable_reasons()
    return {"enabled": row.enabled, "idle_timeout_minutes": row.idle_timeout_minutes,
            "absolute_timeout_minutes": row.absolute_timeout_minutes, "slot_count": row.slot_count,
            "available": not reasons, "unavailable_reasons": reasons, "slots": svc.active_slots(row),
            "updated_by": row.updated_by, "updated_at": row.updated_at}


@router.get("/settings/gui-access", response_model=GuiAccessSettingsRead)
async def read_settings(request: Request, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin_session(request, user)
    row = await svc.get_settings(db)
    await db.commit()
    return _settings_read(row)


@router.put("/settings/gui-access", response_model=GuiAccessSettingsRead)
async def update_settings(payload: GuiAccessSettingsUpdate, request: Request, db: AsyncSession = Depends(get_db),
                          user: AdminUser = Depends(current_user)):
    _require_admin_session(request, user)
    if payload.enabled and svc.unavailable_reasons():
        # Never switchable on without HTTPS cookies and a configured gateway.
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.GUI_GATEWAY_NOT_CONFIGURED,
                        reasons=", ".join(svc.unavailable_reasons()))
    row = await svc.get_settings(db)
    row.enabled = payload.enabled
    row.idle_timeout_minutes = payload.idle_timeout_minutes
    row.absolute_timeout_minutes = payload.absolute_timeout_minutes
    row.slot_count = payload.slot_count
    row.updated_by = user.username
    row.updated_at = datetime.now(timezone.utc)
    if not payload.enabled:
        await svc.end_all(db, "disabled")
    await db.commit()
    return _settings_read(row)


# ── internal: gateway only ────────────────────────────────────────────────

def _gateway_auth(x_onx_gateway_token: str = Header(default="")) -> None:
    expected = settings.gui_gateway_token
    if len(expected) < svc.MIN_GATEWAY_TOKEN_LENGTH:
        raise HTTPException(status_code=404)
    # Fixed-length digests: the comparison time does not depend on the presented value.
    if not hmac.compare_digest(hashlib.sha256(x_onx_gateway_token.encode()).digest(),
                               hashlib.sha256(expected.encode()).digest()):
        raise HTTPException(status_code=401)


_HEX64 = r"^[0-9a-f]{64}$"


class RedeemRequest(BaseModel):
    ticket_hash: str = Field(pattern=_HEX64)
    session_hash: str = Field(pattern=_HEX64)
    slot: int = Field(ge=1, le=65535)
    client_ip: str | None = Field(default=None, max_length=45)


class RevalidateRequest(BaseModel):
    gui_session_id: uuid.UUID
    session_hash: str = Field(pattern=_HEX64)
    # Fingerprint of the target as handed out at redeem time (optional: an older gateway image omits it).
    target_fp: str | None = Field(default=None, pattern=_HEX64)


class EndRequest(BaseModel):
    gui_session_id: uuid.UUID
    session_hash: str = Field(pattern=_HEX64)
    reason: Literal["idle", "absolute", "logout", "revoked", "superseded", "shutdown"]


@internal_router.post("/redeem", dependencies=[Depends(_gateway_auth)])
async def internal_redeem(payload: RedeemRequest, db: AsyncSession = Depends(get_db)):
    result = await svc.redeem(db, ticket_hash=payload.ticket_hash, session_hash=payload.session_hash,
                              slot=payload.slot, client_ip=payload.client_ip)
    if not result["ok"]:
        raise HTTPException(status_code=403, detail={"ok": False, "reason": result["reason"]})
    return result


@internal_router.post("/revalidate", dependencies=[Depends(_gateway_auth)])
async def internal_revalidate(payload: RevalidateRequest, db: AsyncSession = Depends(get_db)):
    return await svc.revalidate(db, gui_session_id=payload.gui_session_id, session_hash=payload.session_hash,
                                 target_fp=payload.target_fp)


@internal_router.post("/end", dependencies=[Depends(_gateway_auth)])
async def internal_end(payload: EndRequest, db: AsyncSession = Depends(get_db)):
    return {"ok": await svc.end(db, gui_session_id=payload.gui_session_id, session_hash=payload.session_hash,
                                reason=payload.reason)}
