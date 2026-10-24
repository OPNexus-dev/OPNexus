"""Audit export settings (admin only): where the audit trail is forwarded to."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.schemas.audit_export import AuditExportRead, AuditExportUpdate
from app.services import audit_export_service as svc
from app.services import notification_secret_store, url_guard

router = APIRouter(prefix="/settings/audit-export", tags=["audit-export"])


def _require_admin(user: AdminUser) -> None:
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)


@router.get("", response_model=AuditExportRead)
async def read(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    return await svc.get_settings(db)


@router.put("", response_model=AuditExportRead)
async def update(payload: AuditExportUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    if payload.webhook_url:
        try:
            payload.webhook_url = url_guard.validate_url(payload.webhook_url)
        except ValueError as exc:
            raise api_error(422, ErrorCode.NOTIFICATION_WEBHOOK_URL_INVALID, reason=str(exc)) from None
    row = await svc.get_settings(db)
    was_active = svc.active(row)
    for field in ("syslog_enabled", "syslog_host", "syslog_port", "syslog_protocol", "webhook_enabled", "webhook_url"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value.strip() if isinstance(value, str) else value)
    if payload.clear_webhook_signing_secret:
        notification_secret_store.delete_secret(svc.SECRET_NAME)
        row.webhook_signing_secret_set = False
    elif payload.webhook_signing_secret is not None:
        notification_secret_store.save_secret(svc.SECRET_NAME, payload.webhook_signing_secret)
        row.webhook_signing_secret_set = True
    if not svc.active(row):
        row.cursor_created_at = row.cursor_id = None  # next enabling starts from "now" again
    elif not was_active:
        row.cursor_created_at, row.cursor_id = datetime.now(timezone.utc), None
    await db.commit()
    return row


@router.post("/test")
async def test(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    try:
        await svc.send_test(await svc.get_settings(db))
    except svc.ExportError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc
    return {"success": True}
