"""Get-or-create for the LogSettings singleton row — same shape as
notification_settings_service.get_settings.

update_settings() added 2026-09-17: v1 shipped with no settings UI/router
for this (row only ever read, with defaults, by the ingest poll loop),
which meant an admin who wanted a different retention_days had to edit
the row directly in Postgres. Found to be a real gap the hard way: the
30-day default, combined with two busy real firewalls, grew
firewall_log_entries to ~8.7M rows / ~9.7GB in 9 days with no way for an
operator to see or change that from the app itself."""
from app.singleton import get_or_create
from app.config import settings
from app.models.log_settings import LogSettings
from app.schemas.logs import LogSettingsUpdate
from sqlalchemy.ext.asyncio import AsyncSession

_SINGLETON_ID = 1


async def get_settings(db: AsyncSession) -> LogSettings:
    return await get_or_create(db, LogSettings)


async def update_settings(db: AsyncSession, payload: LogSettingsUpdate) -> LogSettings:
    row = await get_settings(db)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.poll_interval_seconds is not None:
        row.poll_interval_seconds = max(payload.poll_interval_seconds, settings.log_poll_min_interval_seconds)
    if payload.retention_days is not None:
        row.retention_days = payload.retention_days
    if payload.max_size_gb is not None:
        row.max_size_gb = payload.max_size_gb
    await db.commit()
    await db.refresh(row)
    return row
