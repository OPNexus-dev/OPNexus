import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings

SmtpSecurity = Literal["none", "starttls", "ssl"]
ProblemType = Literal[
    "host_offline", "cert_expiry", "rule_drift", "revert_failed", "maintenance_unknown",
    "carp_split_brain", "nat_drift", "carp_auto_revert_failed",
]


class NotificationSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    webhook_enabled: bool
    webhook_url: str | None
    webhook_signing_secret_set: bool

    email_enabled: bool
    smtp_host: str | None
    smtp_port: int
    smtp_security: SmtpSecurity
    smtp_username: str | None
    smtp_password_set: bool
    smtp_from_address: str | None
    smtp_to_addresses: list[str]

    host_offline_enabled: bool
    cert_expiring_enabled: bool
    rule_drift_enabled: bool
    revert_failed_enabled: bool
    maintenance_unknown_enabled: bool
    carp_health_enabled: bool
    nat_drift_enabled: bool
    carp_auto_revert_failed_enabled: bool

    poll_interval_seconds: int
    updated_at: datetime


class NotificationSettingsUpdate(BaseModel):
    """Partial update — everything optional, only provided fields change.
    Secret values (smtp_password/webhook_signing_secret) are write-only:
    provide a new value to set it, or the matching clear_* flag to remove
    it — omitting both leaves the currently stored secret untouched, same
    disambiguation pattern as HostUpdate.clear_cluster_name."""

    webhook_enabled: bool | None = None
    webhook_url: str | None = Field(default=None, max_length=500)
    webhook_signing_secret: str | None = Field(default=None, min_length=1)
    clear_webhook_signing_secret: bool = False

    email_enabled: bool | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: SmtpSecurity | None = None
    smtp_username: str | None = Field(default=None, max_length=255)
    smtp_password: str | None = Field(default=None, min_length=1)
    clear_smtp_password: bool = False
    smtp_from_address: str | None = Field(default=None, max_length=255)
    smtp_to_addresses: list[str] | None = None

    host_offline_enabled: bool | None = None
    cert_expiring_enabled: bool | None = None
    rule_drift_enabled: bool | None = None
    revert_failed_enabled: bool | None = None
    maintenance_unknown_enabled: bool | None = None
    carp_health_enabled: bool | None = None
    nat_drift_enabled: bool | None = None
    carp_auto_revert_failed_enabled: bool | None = None

    poll_interval_seconds: int | None = Field(
        default=None, ge=settings.notification_poll_min_interval_seconds
    )


class NotificationProblemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    problem_type: ProblemType
    status: Literal["open", "resolved"]
    context: dict
    first_detected_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    last_notified_at: datetime | None
    resolved_notified_at: datetime | None


class TestNotificationRequest(BaseModel):
    channel: Literal["webhook", "email"]
