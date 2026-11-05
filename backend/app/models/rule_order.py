"""A change of the ORDER of rules on one firewall (moving a rule up or down), on probation like a rule deployment.

One row per move. While a row is ``pending`` the new order is live but unconfirmed: ``revert_deadline`` is armed, and if
nobody confirms in time the sweeper moves the rule back (rule_order_service.sweep_expired_order_changes). A row is
written BEFORE the firewall is touched, so a crash leaves a normal, sweepable pending change.

status: pending (live, awaiting confirmation), confirmed, reverted, failed (the move was refused or did not show on the
firewall and was undone at once), revert_failed (taking it back failed: needs attention, blocks deploys on the host).
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

OPEN_STATUSES = ("pending", "revert_failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RuleOrderChange(Base):
    __tablename__ = "rule_order_changes"
    __table_args__ = (Index("ix_rule_order_changes_host_status", "host_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), nullable=False)
    rule_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    prio_group: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # Where the rule was: the rule that followed it and the one before it (either can be missing at the ends of the group).
    anchor_next: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    anchor_prev: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Where it was put: directly before this rule, or NULL = to the end of its group.
    target_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    actor: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    revert_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
