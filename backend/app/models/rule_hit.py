"""When did a firewall rule last match something? (per host and rule UUID)

pf's own counters restart whenever the ruleset is reloaded (every apply, every reboot), so
"packets = 0" alone proves nothing. OPNexus samples them periodically and keeps the time of the
last observed hit, which survives those resets. See services/rule_hit_service.py.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuleHit(Base):
    __tablename__ = "rule_hits"
    __table_args__ = (UniqueConstraint("host_id", "rule_uuid", name="uq_rule_hit"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True)
    rule_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # None = never seen a hit since OPNexus started watching.
    last_hit_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_packets: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
