from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditExportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    syslog_enabled: bool
    syslog_host: str | None
    syslog_port: int
    syslog_protocol: Literal["udp", "tcp", "tls"]
    webhook_enabled: bool
    webhook_url: str | None
    webhook_signing_secret_set: bool
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None


class AuditExportUpdate(BaseModel):
    """Partial update; the signing secret is write-only (set it or clear it)."""

    syslog_enabled: bool | None = None
    syslog_host: str | None = Field(default=None, max_length=255)
    syslog_port: int | None = Field(default=None, ge=1, le=65535)
    syslog_protocol: Literal["udp", "tcp", "tls"] | None = None
    webhook_enabled: bool | None = None
    webhook_url: str | None = Field(default=None, max_length=500)
    webhook_signing_secret: str | None = Field(default=None, min_length=1)
    clear_webhook_signing_secret: bool = False
