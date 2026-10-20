import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.models.notification import NotificationChannel, NotificationProblem
from app.schemas.notification import (
    NotificationChannelCreate,
    NotificationChannelRead,
    NotificationChannelUpdate,
    NotificationProblemRead,
    NotificationSettingsRead,
    NotificationSettingsUpdate,
    TestNotificationRequest,
)
from app.services import notification_channels, notification_delivery, notification_secret_store, notification_settings_service, url_guard

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _redact_url(url: str | None) -> str | None:
    """Webhook URLs very often carry the credential itself (a token in the path or
    query), so anyone who can read the URL can post to the receiver. Readers see
    only where it points."""
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.hostname:
        return "…"
    return f"{parts.scheme}://{parts.hostname}{f':{parts.port}' if parts.port else ''}/…"


@router.get("/settings", response_model=NotificationSettingsRead)
async def read_settings(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    row = await notification_settings_service.get_settings(db)
    result = NotificationSettingsRead.model_validate(row)
    if user.role != "admin":
        result = result.model_copy(update={"webhook_url": _redact_url(result.webhook_url)})
    return result


@router.put("/settings", response_model=NotificationSettingsRead)
async def update_settings(payload: NotificationSettingsUpdate, db: AsyncSession = Depends(get_db)):
    if payload.webhook_url:
        try:
            payload.webhook_url = url_guard.validate_url(payload.webhook_url)
        except ValueError as exc:
            raise api_error(422, ErrorCode.NOTIFICATION_WEBHOOK_URL_INVALID, reason=str(exc)) from None
    return await notification_settings_service.update_settings(db, payload)


@router.get("/problems", response_model=list[NotificationProblemRead])
async def list_problems(
    include_resolved: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    clauses = [] if include_resolved else [NotificationProblem.status == "open"]
    result = await db.execute(
        select(NotificationProblem)
        .where(*clauses)
        .order_by(NotificationProblem.last_seen_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.post("/test")
async def test_notification(payload: TestNotificationRequest, db: AsyncSession = Depends(get_db)):
    settings_row = await notification_settings_service.get_settings(db)
    fake_problem = NotificationProblem(
        problem_type="test",
        dedup_key="test",
        context={"message": "Dies ist eine Testbenachrichtigung von OPNexus."},
    )
    try:
        if payload.channel == "webhook":
            await notification_delivery.send_webhook(settings_row, fake_problem, "opened")
        else:
            subject, body = notification_delivery.render_message(fake_problem, "opened")
            await notification_delivery.send_email(settings_row, subject, body)
    except notification_delivery.NotificationDeliveryError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc
    return {"success": True}


# ── Additional channels (ntfy, Gotify, Telegram, Teams, Matrix) ────────────────

def _channel_read(row: NotificationChannel, user: AdminUser) -> NotificationChannelRead:
    read = NotificationChannelRead.model_validate(row)
    # An ntfy topic or a chat id is as good as a password for whoever knows it: only admins see the settings.
    return read if user.role == "admin" else read.model_copy(update={"config": {}})


async def _get_channel(db: AsyncSession, channel_id: uuid.UUID) -> NotificationChannel:
    row = await db.get(NotificationChannel, channel_id)
    if row is None:
        raise api_error(404, ErrorCode.NOTIFICATION_CHANNEL_NOT_FOUND)
    return row


def _invalid(exc: ValueError):
    return api_error(422, ErrorCode.NOTIFICATION_CHANNEL_INVALID, reason=str(exc))


@router.get("/channels", response_model=list[NotificationChannelRead])
async def list_channels(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    rows = await db.scalars(select(NotificationChannel).order_by(NotificationChannel.name))
    return [_channel_read(r, user) for r in rows.all()]


@router.post("/channels", response_model=NotificationChannelRead, status_code=201)
async def create_channel(payload: NotificationChannelCreate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    try:
        config = notification_channels.clean_config(payload.channel_type, payload.config)
        secret = notification_channels.clean_secret(payload.channel_type, payload.secret) if payload.secret else None
    except ValueError as exc:
        raise _invalid(exc) from None
    row = NotificationChannel(
        name=payload.name.strip(), channel_type=payload.channel_type, enabled=payload.enabled, config=config,
        secret_set=secret is not None,
    )
    row.id = uuid.uuid4()
    if secret is None and notification_channels.missing_requirements(row) == "Zugangsdaten fehlen":
        raise _invalid(ValueError("Zugangsdaten fehlen"))
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise api_error(409, ErrorCode.NOTIFICATION_CHANNEL_NAME_TAKEN) from None
    if secret is not None:
        notification_secret_store.save_secret(notification_channels.secret_name(row.id), secret)
    await db.commit()
    return _channel_read(row, user)


@router.put("/channels/{channel_id}", response_model=NotificationChannelRead)
async def update_channel(
    channel_id: uuid.UUID, payload: NotificationChannelUpdate,
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user),
):
    row = await _get_channel(db, channel_id)
    try:
        if payload.config is not None:
            row.config = notification_channels.clean_config(row.channel_type, payload.config)
        new_secret = notification_channels.clean_secret(row.channel_type, payload.secret) if payload.secret else None
    except ValueError as exc:
        raise _invalid(exc) from None
    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.clear_secret:
        notification_secret_store.delete_secret(notification_channels.secret_name(row.id))
        row.secret_set = False
    elif new_secret is not None:
        notification_secret_store.save_secret(notification_channels.secret_name(row.id), new_secret)
        row.secret_set = True
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise api_error(409, ErrorCode.NOTIFICATION_CHANNEL_NAME_TAKEN) from None
    return _channel_read(row, user)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(channel_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = await _get_channel(db, channel_id)
    notification_secret_store.delete_secret(notification_channels.secret_name(row.id))
    await db.delete(row)
    await db.commit()


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = await _get_channel(db, channel_id)
    try:
        await notification_channels.send_test(row)
    except notification_delivery.NotificationDeliveryError as exc:
        # The text names the channel and the HTTP status; it never contains a URL or a token.
        raise api_error(502, ErrorCode.NOTIFICATION_CHANNEL_TEST_FAILED, reason=str(exc)) from None
    return {"success": True}
