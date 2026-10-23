import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.config_key import config_key_column, register_config_key


class PolicyHierarchyRule(Base):
    """A rule authored on a PolicyHierarchyGroup rather than a specific
    host/DeviceGroup — see services/policy_hierarchy_service.py for how it
    gets pushed to every host in the group's subtree (effective_hosts) in
    the correct cross-group order (ordered_hierarchy_rules).

    Deliberately a separate table/entity from FirewallRule (not an
    extension of it) — the existing rule-deploy path is security-critical
    and this keeps zero regression risk on it. Same field set as
    FirewallRule plus hierarchy_group_id/rulebase/sequence.

    rulebase: "pre" or "post" — splits the rules into a pre- and a
    post-rulebase. Pre-rules are evaluated root-most-group-first (the top of the
    org hierarchy wins first, like a pre-rule should); post-rules
    leaf-most-group-first (the most specific group gets the last, most
    specific word before any host-local NAT/default-deny). See
    ordered_hierarchy_rules() for the actual implementation of this.

    sequence: manual ordering within the same (hierarchy_group_id,
    rulebase) pair — persisted atomically by the drag-and-drop endpoint;
    up/down buttons remain as an accessible keyboard fallback."""

    __tablename__ = "policy_hierarchy_rules"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_key: Mapped[str] = config_key_column()
    hierarchy_group_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policy_hierarchy_groups.id", ondelete="CASCADE")
    )
    rulebase: Mapped[str] = mapped_column(String(8))
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(String(255))
    interface: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    ipprotocol: Mapped[str] = mapped_column(String(8), default="inet")
    protocol: Mapped[str] = mapped_column(String(16), default="any")
    action: Mapped[str] = mapped_column(String(16), default="pass")
    source_net: Mapped[str] = mapped_column(String(128), default="any")
    source_port: Mapped[str | None] = mapped_column(String(32), nullable=True)
    destination_net: Mapped[str] = mapped_column(String(128), default="any")
    destination_port: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    group: Mapped["PolicyHierarchyGroup"] = relationship("PolicyHierarchyGroup")
    deployments: Mapped[list["PolicyHierarchyRuleDeployment"]] = relationship(
        "PolicyHierarchyRuleDeployment", back_populates="rule", cascade="all, delete-orphan"
    )


register_config_key(PolicyHierarchyRule, "ph")
