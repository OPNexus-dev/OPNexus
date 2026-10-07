from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.notification import NotificationProblem
from app.schemas.notification import (
    NotificationProblemRead,
    NotificationSettingsRead,
    NotificationSettingsUpdate,
    TestNotificationRequest,
)
from app.services import notification_delivery, notification_settings_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/settings", response_model=NotificationSettingsRead)
async def read_settings(db: AsyncSession = Depends(get_db)):
    return await notification_settings_service.get_settings(db)


@router.put("/settings", response_model=NotificationSettingsRead)
async def update_settings(payload: NotificationSettingsUpdate, db: AsyncSession = Depends(get_db)):
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
