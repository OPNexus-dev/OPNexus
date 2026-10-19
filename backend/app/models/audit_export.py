"""Forwarding of the audit trail to a syslog server and/or a webhook (singleton row).

``cursor_*`` is the position of the last shipped event, so delivery resumes where
it stopped (at-least-once). Enabling a channel starts at "now": the existing history
is not replayed. The webhook signing secret lives on disk, like every other secret
(services/notification_secret_store.py).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditExportSettings(Base):
    __tablename__ = "audit_export_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    syslog_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    syslog_host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    syslog_port: Mapped[int] = mapped_column(Integer, nullable=False, default=514)
    # "udp" | "tcp" | "tls"
    syslog_protocol: Mapped[str] = mapped_column(String(3), nullable=False, default="udp")
    webhook_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    webhook_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    webhook_signing_secret_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cursor_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    last_error_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
