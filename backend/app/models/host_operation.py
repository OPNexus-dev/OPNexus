import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime, String, JSON
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class HostOperation(Base):
    __tablename__ = "host_operations"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    host_name: Mapped[str] = mapped_column(String(120))
    actor: Mapped[str] = mapped_column(String(150))
    action: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="preview")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
