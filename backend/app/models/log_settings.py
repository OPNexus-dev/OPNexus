"""Log-ingest configuration, singleton row (id fixed at 1) — exactly the
NotificationSettings pattern (see models/notification.py /
services/notification_settings_service.py)."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LogSettings(Base):
    __tablename__ = "log_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    # Lowered from 30 to 7 (2026-09-17): two real, moderately busy
    # firewalls filled firewall_log_entries to ~8.7M rows / ~9.7GB in just
    # 9 days at the old default, with no settings UI to notice or fix it
    # (see log_settings_service's update_settings() docstring) — a public
    # self-hoster with busier traffic could fill a small disk in weeks.
    # Still just a default; admin-editable per-install via the new
    # GET/PUT /api/logs/settings.
    retention_days: Mapped[int] = mapped_column(Integer, default=7)
    # Upper bound for the log table in GB (0 = unlimited). Retention by age
    # alone cannot protect the disk on a busy firewall — the oldest entries
    # are deleted once the estimated live data exceeds this.
    max_size_gb: Mapped[float] = mapped_column(Float, nullable=False, default=10.0, server_default="10")

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
