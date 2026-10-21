from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationSettings
from app.schemas.notification import NotificationSettingsUpdate
from app.services import notification_secret_store

_SINGLETON_ID = 1


async def get_settings(db: AsyncSession) -> NotificationSettings:
    row = await db.get(NotificationSettings, _SINGLETON_ID)
    if row is None:
        row = NotificationSettings(id=_SINGLETON_ID)
        db.add(row)
        await db.flush()
    return row


async def update_settings(db: AsyncSession, payload: NotificationSettingsUpdate) -> NotificationSettings:
    row = await get_settings(db)

    simple_fields = [
        "webhook_enabled", "webhook_url",
        "email_enabled", "smtp_host", "smtp_port", "smtp_security", "smtp_username",
        "smtp_from_address", "smtp_to_addresses",
        "host_offline_enabled", "cert_expiring_enabled", "rule_drift_enabled",
        "revert_failed_enabled", "maintenance_unknown_enabled", "carp_health_enabled", "nat_drift_enabled",
        "network_drift_enabled", "carp_auto_revert_failed_enabled", "scheduled_maintenance_failed_enabled", "backup_failed_enabled", "compliance_alerts_enabled",
        "gateway_down_enabled", "resource_threshold_enabled", "service_down_enabled", "rollout_problems_enabled",
        "gateway_loss_threshold_pct", "gateway_delay_threshold_ms", "disk_threshold_pct", "memory_threshold_pct",
        "swap_threshold_pct", "load_per_cpu_threshold", "alert_confirm_samples", "resource_confirm_samples",
        "service_watchlist",
        "poll_interval_seconds",
    ]
    for field in simple_fields:
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)

    if payload.clear_webhook_signing_secret:
        notification_secret_store.delete_secret("webhook_signing_secret")
        row.webhook_signing_secret_set = False
    elif payload.webhook_signing_secret is not None:
        notification_secret_store.save_secret("webhook_signing_secret", payload.webhook_signing_secret)
        row.webhook_signing_secret_set = True

    if payload.clear_smtp_password:
        notification_secret_store.delete_secret("smtp_password")
        row.smtp_password_set = False
    elif payload.smtp_password is not None:
        notification_secret_store.save_secret("smtp_password", payload.smtp_password)
        row.smtp_password_set = True

    await db.commit()
    return row
