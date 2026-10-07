import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CarpEvent(Base):
    """One row per observed CARP-role transition on a host, e.g. BACKUP -> MASTER.

    Populated by carp_event_service.detect_and_record, called from main.py's
    existing _notification_poll_loop. Deliberately has no "current state"
    column on OpnsenseHost to diff against — the previous state is read back
    from this table's own latest row per host, so no manual ALTER is needed
    on an existing table for this feature.
    """

    __tablename__ = "carp_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    host_name: Mapped[str] = mapped_column(String(120))
    cluster_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
