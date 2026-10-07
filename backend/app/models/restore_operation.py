import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime, String, JSON
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class RestoreOperation(Base):
    """Stage 1 restore: preview a locally-stored config.xml backup and
    record a human-performed handoff to the OPNsense GUI. No row here ever
    triggers an OPNsense write — see restore_service.py's module docstring.
    Shaped like HostOperation (no FK to opnsense_hosts, denormalized name).
    """

    __tablename__ = "restore_operations"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    host_name: Mapped[str] = mapped_column(String(120))
    actor: Mapped[str] = mapped_column(String(150))
    backup_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="preview")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    confirm_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    handed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
