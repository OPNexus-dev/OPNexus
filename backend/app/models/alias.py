import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.alias_target import alias_targets
from app.models.policy import alias_groups


class FirewallAlias(Base):
    """A centrally-authored alias (object), pushed to hosts the same way
    FirewallRule is — see services/alias_deploy_service.py. Editing an
    alias's content changes what every rule referencing it enforces on
    every host it reaches, so it goes through the same
    draft/preview/push/confirm/auto-rollback safety net as rules, not an
    immediate write.
    """

    __tablename__ = "firewall_aliases"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64))
    # OPNsense alias types this app manages centrally — the common,
    # user-editable ones. "internal" (auto-generated __if_network) and
    # "external" (built-ins like bogons/sshlockout) aren't created here.
    type: Mapped[str] = mapped_column(String(32))
    # Newline-separated entries, matching OPNsense's own wire format for
    # this field (verified live via GET .../alias/searchItem).
    content: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(String(255), default="")
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

    targets: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=alias_targets)
    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=alias_groups)
    deployments: Mapped[list["AliasDeployment"]] = relationship(
        "AliasDeployment", back_populates="alias", cascade="all, delete-orphan"
    )
