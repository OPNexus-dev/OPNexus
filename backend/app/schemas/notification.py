import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.models.notification import DEFAULT_SERVICE_WATCHLIST

SmtpSecurity = Literal["none", "starttls", "ssl"]
ProblemType = Literal[
    "host_offline", "cert_expiry", "rule_drift", "revert_failed", "maintenance_unknown",
    "carp_split_brain", "nat_drift", "carp_auto_revert_failed", "network_drift",
    "scheduled_maintenance_failed",
    "backup_stale",
    "backup_offsite_failed",
    "compliance_finding",
    "gateway_down", "resource_threshold", "service_down",
]
ChannelType = Literal["ntfy", "gotify", "telegram", "teams", "matrix"]


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
    network_drift_enabled: bool = True
    carp_auto_revert_failed_enabled: bool
    scheduled_maintenance_failed_enabled: bool = True
    backup_failed_enabled: bool = True
    compliance_alerts_enabled: bool = False
    gateway_down_enabled: bool = True
    resource_threshold_enabled: bool = False
    service_down_enabled: bool = False
    gateway_loss_threshold_pct: int = 20
    gateway_delay_threshold_ms: int = 500
    disk_threshold_pct: int = 90
    memory_threshold_pct: int = 95
    swap_threshold_pct: int = 50
    load_per_cpu_threshold: float = 2.0
    alert_confirm_samples: int = 3
    resource_confirm_samples: int = 5
    service_watchlist: list[str] = Field(default_factory=lambda: list(DEFAULT_SERVICE_WATCHLIST))

    poll_interval_seconds: int
    updated_at: datetime

    @field_validator("service_watchlist", mode="before")
    @classmethod
    def _default_watchlist(cls, value):
        return list(DEFAULT_SERVICE_WATCHLIST) if value is None else value


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
    network_drift_enabled: bool | None = None
    carp_auto_revert_failed_enabled: bool | None = None
    scheduled_maintenance_failed_enabled: bool | None = None
    backup_failed_enabled: bool | None = None
    compliance_alerts_enabled: bool | None = None
    gateway_down_enabled: bool | None = None
    resource_threshold_enabled: bool | None = None
    service_down_enabled: bool | None = None
    # 0 switches that single threshold off.
    gateway_loss_threshold_pct: int | None = Field(default=None, ge=0, le=100)
    gateway_delay_threshold_ms: int | None = Field(default=None, ge=0, le=60000)
    disk_threshold_pct: int | None = Field(default=None, ge=0, le=100)
    memory_threshold_pct: int | None = Field(default=None, ge=0, le=100)
    swap_threshold_pct: int | None = Field(default=None, ge=0, le=100)
    load_per_cpu_threshold: float | None = Field(default=None, ge=0, le=1000)
    alert_confirm_samples: int | None = Field(default=None, ge=1, le=60)
    resource_confirm_samples: int | None = Field(default=None, ge=1, le=60)
    service_watchlist: list[str] | None = Field(default=None, max_length=100)

    @field_validator("service_watchlist")
    @classmethod
    def _clean_watchlist(cls, value):
        if value is None:
            return None
        names = []
        for raw in value:
            name = str(raw).strip()
            if not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", name):
                raise ValueError("Dienstnamen bestehen aus Buchstaben, Ziffern und . _ + -")
            if name not in names:
                names.append(name)
        return names

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


class NotificationChannelRead(BaseModel):
    """Never carries the credential; ``config`` is withheld from non-admins (an ntfy topic is a password)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    channel_type: ChannelType
    enabled: bool
    config: dict
    secret_set: bool
    created_at: datetime
    updated_at: datetime


def _channel_name(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip():
        raise ValueError("Name darf nicht leer sein")
    return value.strip()


class NotificationChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    channel_type: ChannelType
    enabled: bool = True
    config: dict = Field(default_factory=dict)
    # Token, or for Teams the webhook address. Write-only.
    secret: str | None = Field(default=None, max_length=2100)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value):
        return _channel_name(value)


class NotificationChannelUpdate(BaseModel):
    """Partial: omitted fields stay. ``secret`` replaces the credential, ``clear_secret`` removes it."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    config: dict | None = None
    secret: str | None = Field(default=None, max_length=2100)
    clear_secret: bool = False

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value):
        return _channel_name(value)
