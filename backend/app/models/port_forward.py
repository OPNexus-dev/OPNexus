import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.config_key import config_key_column, register_config_key
from app.models.port_forward_target import port_forward_targets
from app.models.policy import port_forward_groups


class PortForwardRule(Base):
    """A centrally-authored Port Forward (destination NAT) rule. OPNsense's
    wire field for this is "disabled", not "enabled" (see
    port_forward_deploy_service._build_payload) — this model stores the
    same enabled=True/False polarity as FirewallRule/FirewallAlias for
    consistency within OPNexus; only the outgoing payload flips it."""

    __tablename__ = "port_forward_rules"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_key: Mapped[str] = config_key_column()
    description: Mapped[str] = mapped_column(String(255))
    interface: Mapped[str] = mapped_column(String(64))
    ipprotocol: Mapped[str] = mapped_column(String(8), default="inet")
    protocol: Mapped[str] = mapped_column(String(16), default="tcp")
    source_net: Mapped[str] = mapped_column(String(128), default="any")
    source_port: Mapped[str | None] = mapped_column(String(32), nullable=True)
    destination_net: Mapped[str] = mapped_column(String(128), default="any")
    destination_port: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target: Mapped[str] = mapped_column(String(128))
    local_port: Mapped[str | None] = mapped_column(String(32), nullable=True)
    natreflection: Mapped[str] = mapped_column(String(16), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    policy_hierarchy_group_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policy_hierarchy_groups.id", ondelete="SET NULL"), nullable=True
    )

    targets: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=port_forward_targets)
    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=port_forward_groups)
    deployments: Mapped[list["PortForwardDeployment"]] = relationship(
        "PortForwardDeployment", back_populates="port_forward", cascade="all, delete-orphan"
    )


register_config_key(PortForwardRule, "pf")
