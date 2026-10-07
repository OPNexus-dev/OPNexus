import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PolicyHierarchyGroup(Base):
    """A node in the pre-/post-rulebase inheritance tree — deliberately
    separate from DeviceGroup (see models/policy.py), which allows a host
    to belong to more than one group at once. That multi-membership model
    is fine for plain rule-target grouping, but would make "which ancestor
    chain applies to this host" ambiguous for ordered inheritance, which is
    the entire point of this feature. A host belongs to at most one
    PolicyHierarchyGroup (see OpnsenseHost.policy_hierarchy_group_id).

    parent_id uses ondelete="RESTRICT": a group with children can't be
    deleted out from under them (see policy_hierarchy_group_service /
    routers/policy_hierarchy_groups.py, which also blocks deletion while
    rules are still attached, mirroring DeviceGroup's own delete guard)."""

    __tablename__ = "policy_hierarchy_groups"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(String(2000), default="")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policy_hierarchy_groups.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    parent: Mapped["PolicyHierarchyGroup | None"] = relationship(
        "PolicyHierarchyGroup", remote_side=[id], back_populates="children"
    )
    children: Mapped[list["PolicyHierarchyGroup"]] = relationship(
        "PolicyHierarchyGroup", back_populates="parent"
    )
    hosts: Mapped[list["OpnsenseHost"]] = relationship(
        "OpnsenseHost", back_populates="policy_hierarchy_group"
    )
