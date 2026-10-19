from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BackupStatusRead(BaseModel):
    """What every signed-in user may see: no endpoints, no keys."""

    model_config = ConfigDict(from_attributes=True)

    scheduled_enabled: bool
    interval_hours: int
    keep_count: int
    offsite_enabled: bool
    last_run_at: datetime | None
    last_offsite_ok_at: datetime | None
    last_offsite_error: str | None


class BackupSettingsRead(BackupStatusRead):
    s3_endpoint: str | None
    s3_region: str
    s3_bucket: str | None
    s3_prefix: str
    s3_access_key: str | None
    s3_path_style: bool
    s3_secret_set: bool
    passphrase_set: bool


class BackupSettingsUpdate(BaseModel):
    """Partial update; the S3 secret key and the passphrase are write-only."""

    scheduled_enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    keep_count: int | None = Field(default=None, ge=3, le=1000)
    offsite_enabled: bool | None = None
    s3_endpoint: str | None = Field(default=None, max_length=255)
    s3_region: str | None = Field(default=None, min_length=1, max_length=64)
    s3_bucket: str | None = Field(default=None, max_length=255)
    s3_prefix: str | None = Field(default=None, max_length=255)
    s3_access_key: str | None = Field(default=None, max_length=255)
    s3_path_style: bool | None = None
    s3_secret_key: str | None = Field(default=None, min_length=1, max_length=512)
    clear_s3_secret: bool = False
    passphrase: str | None = Field(default=None, min_length=12, max_length=512)
