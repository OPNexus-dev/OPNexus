"""Rule cleanup (1.31, B4 part 2): switching rules off or deleting them through preview -> deploy -> confirm.

``RuleCleanupOperation`` is one cleanup run on a host (or a cluster pair, anchored at one host); its id doubles as
the preview token. ``RuleCleanupItem`` is one rule on one node of that run. The columns only carry what the design
(DESIGN-1.31) needs to resume, revert and restore after a crash; the logic lives in services/rule_cleanup_service.py.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# kind: disable | enable (re-enable after a restore) | delete | restore
OPERATION_KINDS = ("disable", "enable", "delete", "restore")
# preview -> running -> awaiting_confirm -> confirmed; the rest are the side exits.
OPERATION_STATUSES = (
    "preview", "running", "awaiting_confirm", "confirmed", "partial", "apply_failed", "reverting", "reverted",
    "revert_failed", "done", "failed", "cancelled", "expired",
)
ITEM_STATES = (
    "queued", "disabling", "disabled", "confirmed", "reverting", "reverted", "deleting", "deleted", "restoring",
    "restored", "failed", "unknown", "revert_failed",
    # never attempted (the run stopped at an earlier error); not an open state, so the rule is free again
    "skipped",
)
# A rule in one of these states is being worked on or its outcome is unresolved: no second item may claim it.
OPEN_ITEM_STATES = ("queued", "disabling", "disabled", "reverting", "deleting", "restoring", "unknown", "revert_failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RuleCleanupOperation(Base):
    __tablename__ = "rule_cleanup_operations"
    __table_args__ = (
        Index("ix_rule_cleanup_operations_host", "host_id", "created_at"),
        Index("ix_rule_cleanup_operations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), nullable=False)
    cluster_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="preview")
    actor: Mapped[str] = mapped_column(String(150), nullable=False)
    # The stable account id next to the name (see migration 0026); no foreign key, a deleted account stays gone.
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Written at every checkpoint, so another process can tell an abandoned ``running`` operation from a live one.
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Auto-revert deadline (disable/enable only); NULL once confirmed or for kinds without a window.
    revert_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    # sha256 over everything the preview showed; the deploy recomputes it and refuses on any difference.
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # unused_days, acknowledged rules (with the text version), protected management sources.
    options: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class RuleCleanupItem(Base):
    __tablename__ = "rule_cleanup_items"
    __table_args__ = (
        Index("ix_rule_cleanup_items_operation", "operation_id"),
        # Independent of any lock: one rule on one node can be in only one unfinished item at a time.
        Index(
            "uq_cleanup_rule_open", "host_id", "rule_uuid", unique=True,
            postgresql_where=text("state IN (" + ", ".join(f"'{s}'" for s in OPEN_ITEM_STATES) + ")"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("rule_cleanup_operations.id", ondelete="CASCADE"), nullable=False)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), nullable=False)
    rule_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    carp_role: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # What the preview showed.
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    categories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    row_before: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    fp_before: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # shadowed/duplicate: the covering rule must stay unchanged until the deploy.
    cover_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cover_fp: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Flattened getRule, raw answer and firmware; written BEFORE a rule is deleted.
    snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Where the rule sat (neighbours in the same prio_group by sort_order), for the restore.
    anchor_next_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    anchor_prev_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prio_group: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # delete -> the confirmed disable item it builds on; restore -> the delete item it undoes. Plain id, no foreign key.
    source_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    restored_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
