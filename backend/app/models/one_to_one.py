import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.one_to_one_target import one_to_one_targets
from app.models.policy import one_to_one_groups


class OneToOneRule(Base):
    __tablename__ = "one_to_one_rules"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    description: Mapped[str] = mapped_column(String(255))
    interface: Mapped[str] = mapped_column(String(64))
    source_net: Mapped[str] = mapped_column(String(128))
    destination_net: Mapped[str] = mapped_column(String(128))
    source_not: Mapped[bool] = mapped_column(Boolean, default=False)
    destination_not: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    type: Mapped[str] = mapped_column(String(8), default="binat")
    external: Mapped[str] = mapped_column(String(128))
    natreflection: Mapped[str] = mapped_column(String(16), default="")
    log: Mapped[bool] = mapped_column(Boolean, default=False)
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

    targets: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=one_to_one_targets)
    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=one_to_one_groups)
    deployments: Mapped[list["OneToOneDeployment"]] = relationship(
        "OneToOneDeployment", back_populates="one_to_one", cascade="all, delete-orphan"
    )
