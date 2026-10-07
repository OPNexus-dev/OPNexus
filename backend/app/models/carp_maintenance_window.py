import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CarpMaintenanceWindow(Base):
    """Tracks an optional auto-revert timer on top of the existing CARP
    maintenance-mode toggle (opnsense_client.toggle_carp_maintenance).

    One open ("active") row per host at most. The sweep in
    carp_maintenance_sweep_service checks for rows past auto_revert_at and
    calls the same toggle used by a manual "CARP-Normalbetrieb herstellen"
    click. status="confirmed_permanent" means the user chose no auto-revert
    (or explicitly cancelled the timer) — the row is kept for history rather
    than deleted.
    """

    __tablename__ = "carp_maintenance_windows"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    host_name: Mapped[str] = mapped_column(String(120))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    auto_revert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # active | confirmed_permanent | reverted | revert_failed
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
