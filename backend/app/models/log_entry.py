"""Persisted firewall log entries, ingested incrementally by
log_ingest_service.py via OPNsense's digest-based cursor (`diagnostics/
firewall/log`'s `digest` param — verified against core's
scripts/filter/read_log.py, see opnsense_client.get_log).

Counter-example to models/audit.py on purpose: audit_events is made
immutable (DB trigger blocks UPDATE/DELETE) because it's an audit trail.
This table needs the OPPOSITE property — retention pruning
(log_ingest_service.prune_old_entries) must be able to delete old rows,
so no such trigger is added here.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FirewallLogEntry(Base):
    __tablename__ = "firewall_log_entries"
    __table_args__ = (
        # "unique je Host" per the design — a digest is only guaranteed
        # unique within one host's own log stream, and this constraint is
        # also what the ingest service's ON CONFLICT DO NOTHING dedupe
        # keys off of.
        UniqueConstraint("host_id", "digest", name="uq_firewall_log_entry_host_digest"),
        Index("ix_firewall_log_entries_host_timestamp", "host_id", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    interface: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    protocol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_net: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_port: Mapped[str | None] = mapped_column(String(16), nullable=True)
    destination_net: Mapped[str | None] = mapped_column(String(255), nullable=True)
    destination_port: Mapped[str | None] = mapped_column(String(16), nullable=True)
    label: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Full original OPNsense record, for future-proofing / debugging —
    # the mapped columns above only cover today's known-useful fields.
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    # OPNsense's own `__digest__` for this exact line — the cursor value
    # log_ingest_service.py hands back as `digest` on the next poll.
    digest: Mapped[str] = mapped_column(String(64))
