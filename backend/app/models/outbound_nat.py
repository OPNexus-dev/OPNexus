import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.outbound_nat_target import outbound_nat_targets
from app.models.policy import outbound_nat_groups


class OutboundNatRule(Base):
    __tablename__ = "outbound_nat_rules"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    description: Mapped[str] = mapped_column(String(255))
    interface: Mapped[str] = mapped_column(String(64))
    source_net: Mapped[str] = mapped_column(String(128))
    destination_net: Mapped[str] = mapped_column(String(128))
    source_not: Mapped[bool] = mapped_column(Boolean, default=False)
    destination_not: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ipprotocol: Mapped[str] = mapped_column(String(8), default="inet")
    protocol: Mapped[str] = mapped_column(String(16))
    source_port: Mapped[str | None] = mapped_column(String(32), nullable=True)
    destination_port: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target: Mapped[str] = mapped_column(String(128))
    target_port: Mapped[str | None] = mapped_column(String(32), nullable=True)
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

    targets: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=outbound_nat_targets)
    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=outbound_nat_groups)
    deployments: Mapped[list["OutboundNatDeployment"]] = relationship(
        "OutboundNatDeployment", back_populates="outbound_nat", cascade="all, delete-orphan"
    )
