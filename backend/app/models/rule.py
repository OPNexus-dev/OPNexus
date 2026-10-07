import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.rule_target import rule_targets
from app.models.policy import rule_groups


class FirewallRule(Base):
    """A centrally-authored rule. Pushing it to a host happens via
    services/rule_deploy_service.py, which records one RuleDeployment row
    per (rule, host)."""

    __tablename__ = "firewall_rules"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    targets: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=rule_targets)
    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=rule_groups)
    deployments: Mapped[list["RuleDeployment"]] = relationship(
        "RuleDeployment", back_populates="rule", cascade="all, delete-orphan"
    )
