"""Scheduled config backups and their offsite copy (singleton row + upload ledger).

The S3 secret key and the encryption passphrase live on disk (notification_secret_store,
names ``backup_s3_secret`` / ``backup_passphrase``), never here.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BackupSettings(Base):
    __tablename__ = "backup_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheduled_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    keep_count: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    offsite_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    s3_endpoint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    s3_region: Mapped[str] = mapped_column(String(64), nullable=False, default="us-east-1")
    s3_bucket: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    s3_prefix: Mapped[str] = mapped_column(String(255), nullable=False, default="opnexus")
    s3_access_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    s3_path_style: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    s3_secret_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    passphrase_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_offsite_ok_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_offsite_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)


class BackupUpload(Base):
    """Which local snapshot already exists offsite (so nothing is uploaded twice)."""

    __tablename__ = "backup_uploads"
    __table_args__ = (UniqueConstraint("host_id", "snapshot_id", name="uq_backup_upload"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_key: Mapped[str] = mapped_column(String(400), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
