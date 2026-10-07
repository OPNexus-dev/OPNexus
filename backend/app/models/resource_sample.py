"""Periodic host resource samples (CPU load, memory, disk, interface
counters) — OPNexus's own time series, polled independently of whatever
retention OPNsense's own on-device RRD graphs keep. A brand-new table,
same "no manual ALTER needed" situation as host_log_cursor/
firewall_log_entries (see those modules' docstrings) since nothing here
touches an existing table.

Like firewall_log_entries and unlike audit_events, this table needs
retention pruning (resource_monitor_service.prune_old_entries) to be able
to delete old rows, so no immutability trigger is added here either.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, JSON
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HostResourceSample(Base):
    __tablename__ = "host_resource_samples"
    __table_args__ = (
        Index("ix_host_resource_samples_host_sampled_at", "host_id", "sampled_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True
    )
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mem_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    load_avg_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Catch-all: full memory/disk-device/interface-statistics payloads, for
    # anything the three columns above don't cover (per-interface
    # throughput counters in particular — see resource_monitor_service.py).
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
