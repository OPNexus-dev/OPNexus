"""Change history of the rules that are live on a firewall (all rules, also hand-made ones).

``live_rule_states`` is the last observed state per rule (a fingerprint plus the row itself), maintained by the
``rule_observe`` loop; ``rule_change_events`` is what changed between two observations. See
services/rule_history_service.py. Neither table holds anything the firewall itself would not show on its rule page.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LiveRuleState(Base):
    __tablename__ = "live_rule_states"

    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True)
    # The rule's UUID/label on the firewall; BASELINE_KEY marks "this host has been observed once".
    rule_uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    row: Mapped[dict] = mapped_column(JSON, nullable=False)
    # False = the rule is gone (kept as a marker so a returning UUID continues the numbering).
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Counts the observed changes of this rule; the event of a change gets the new number, which makes the
    # observation idempotent when two processes see the same change.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuleChangeEvent(Base):
    __tablename__ = "rule_change_events"
    __table_args__ = (
        UniqueConstraint("host_id", "rule_uuid", "seq", name="uq_rule_change_event"),
        Index("ix_rule_change_events_rule", "host_id", "rule_uuid", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), nullable=False)
    rule_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)       # added | changed | removed | enabled | disabled
    before: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)     # opnexus | external
    actor: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    # The audit entry that explains an OPNexus-made change; no foreign key, the audit trail outlives everything.
    audit_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Start of the time since the previous observation of the host: only audit entries from then on can explain
    # this change (a later reconcile must not reach back to an older deployment).
    window_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
