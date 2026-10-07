"""One-shot scheduled host-maintenance actions (reboot/update/major_update
at a future timestamp instead of immediately). A brand-new table, same
"no manual ALTER needed" situation as host_resource_samples.

Deliberately v1-minimal: one fixed `run_at`, no recurrence/cron
expression — see resource_monitor_service.py's sibling note in
config.py for the same "keep v1 small" rationale. Firing reuses the
existing HostOperation/ensure_no_maintenance lock machinery
(routers/scheduled_maintenance.py, main.py's
_scheduled_maintenance_poll_loop) rather than duplicating it — this
table only tracks the *schedule* itself, not the resulting operation.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScheduledMaintenance(Base):
    __tablename__ = "scheduled_maintenance"
    __table_args__ = (
        Index("ix_scheduled_maintenance_status_run_at", "status", "run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True
    )
    host_name: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(32))  # reboot | update | major_update
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|succeeded|failed|cancelled
    actor: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    # Set to the resulting HostOperation's id once fired, so the UI can
    # link from the schedule to the operation it produced.
    host_operation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
