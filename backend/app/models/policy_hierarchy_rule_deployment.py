import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PolicyHierarchyRuleDeployment(Base):
    """Copy of models/rule_deployment.py for PolicyHierarchyRule — see that
    file's docstring for the status lifecycle and lockout mechanism, which
    this mirrors exactly (policy_hierarchy_deploy_service.py is a 1:1 port
    of rule_deploy_service.py). Columns are named policy_hierarchy_id /
    opnsense_policy_hierarchy_uuid (not rule_id/opnsense_rule_uuid) so
    audit_service.tracked_rollback("policy_hierarchy") — which derives
    attribute names from its `kind` argument — resolves them correctly.
    """

    __tablename__ = "policy_hierarchy_rule_deployments"
    __table_args__ = (UniqueConstraint("policy_hierarchy_id", "host_id", name="uq_policy_hierarchy_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_hierarchy_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policy_hierarchy_rules.id", ondelete="CASCADE")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opnsense_policy_hierarchy_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # Set while status == "pending"; sweep_expired_hierarchy_deployments
    # reverts once this passes without a confirm call.
    revert_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The OPNsense-shaped payload we last successfully applied on this host,
    # BEFORE the current pending change (None if this deploy created the
    # rule fresh on this host).
    previous_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The payload actually active on this host right now.
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    rule: Mapped["PolicyHierarchyRule"] = relationship("PolicyHierarchyRule", back_populates="deployments")
    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
