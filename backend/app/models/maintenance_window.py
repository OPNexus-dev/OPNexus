"""Recurring maintenance windows (1.25): "every Sunday 02:00-05:00 Europe/Berlin",
"first Tuesday of the month 22:00 for 3 h". A window on its own changes nothing on any
firewall; rollout plans (models/rollout.py) use it to decide WHEN they may start work on a host.

Times are wall-clock times in the window's own IANA time zone, so a window stays at 02:00
local time across daylight-saving changes (see services/window_service.py for the rules).
"""
import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Time, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    timezone: Mapped[str] = mapped_column(String(64))
    recurrence: Mapped[str] = mapped_column(String(16))  # weekly | monthly
    # weekly: weekdays 0 (Monday) .. 6 (Sunday). JSON columns get a Python default only (Alembic gotcha, see 0007).
    weekdays: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # monthly: "nth_weekday" (month_nth 1-4 or -1 = last, month_weekday 0-6) or "day" (month_day 1-28)
    month_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    month_nth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    month_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    month_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_time: Mapped[time] = mapped_column(Time)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    # After this many minutes into the window no NEW host action starts (running ones finish).
    start_cutoff_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_by: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    exceptions: Mapped[list["MaintenanceWindowException"]] = relationship(
        "MaintenanceWindowException", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
        order_by="MaintenanceWindowException.occurrence_date",
    )


class MaintenanceWindowException(Base):
    """One occurrence skipped or moved. Keyed by the occurrence's LOCAL date."""

    __tablename__ = "maintenance_window_exceptions"
    __table_args__ = (UniqueConstraint("window_id", "occurrence_date", name="uq_window_exception_date"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    window_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("maintenance_windows.id", ondelete="CASCADE"), index=True
    )
    occurrence_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(8))  # skip | move
    moved_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    actor: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
