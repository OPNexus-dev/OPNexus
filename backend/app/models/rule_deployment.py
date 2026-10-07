import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RuleDeployment(Base):
    """Last known deploy result of one rule on one host. opnsense_rule_uuid
    is the UUID OPNsense assigned on THAT host (each host gets its own copy
    of the rule) — required to target setRule/delRule there later.

    status: "pending" (applied, awaiting confirmation — see
    rule_deploy_service.sweep_expired_deployments), "success" (confirmed,
    or a no-op re-deploy of an unchanged payload), "error" (the deploy
    call itself failed), "reverted" (auto-rolled-back after the
    confirmation window expired unconfirmed), "revert_failed" (the
    rollback call itself failed — needs manual attention).
    """

    __tablename__ = "rule_deployments"
    __table_args__ = (UniqueConstraint("rule_id", "host_id", name="uq_rule_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("firewall_rules.id", ondelete="CASCADE")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opnsense_rule_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # Set while status == "pending"; sweep_expired_deployments reverts once
    # this passes without a confirm call.
    revert_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The OPNsense-shaped payload we last successfully applied on this host,
    # BEFORE the current pending change (None if this deploy created the
    # rule fresh on this host — the revert path for that case is delRule,
    # not setRule). This is our own record of "what was there", not a value
    # fetched back from OPNsense — its getRule response shape is more
    # complex/uncertain than we want to depend on for a safety mechanism.
    previous_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The payload actually active on this host right now (mirrors
    # previous_payload's shape) — compared against a new deploy's payload
    # to detect a true no-op re-deploy, which skips the pending window
    # entirely since nothing risky happened.
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    rule: Mapped["FirewallRule"] = relationship("FirewallRule", back_populates="deployments")
    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
