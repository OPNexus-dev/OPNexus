"""Append-only history, deliberately no foreign keys: deletion keeps evidence."""
import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime, String, JSON, Text, event, DDL
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    actor: Mapped[str] = mapped_column(String(150), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    entity_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    host_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    host_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

event.listen(AuditEvent.__table__, "after_create", DDL("""
CREATE OR REPLACE FUNCTION opnexus_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Audit history is append-only'; END $$;
""").execute_if(dialect="postgresql"))
event.listen(AuditEvent.__table__, "after_create", DDL("""
CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION opnexus_audit_immutable();
""").execute_if(dialect="postgresql"))
