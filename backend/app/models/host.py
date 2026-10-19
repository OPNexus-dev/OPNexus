import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OpnsenseHost(Base):
    """A managed OPNsense firewall. The API secret never lives here — see
    services/opnsense_secret_store.py, which keeps it on disk only."""

    __tablename__ = "opnsense_hosts"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(String(255))
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    # Pinned certificate (or CA) of the firewall, PEM. When set together with
    # verify_tls, only this certificate is trusted — see tls_pin_service.
    tls_ca_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Free-text grouping label — hosts sharing the same value are shown
    # together on the cluster view (e.g. an HA pair). Not a separate
    # entity/table on purpose: a host belongs to at most one such group,
    # and there's no cluster-level data to manage beyond the name itself.
    cluster_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # A host belongs to at most one policy-hierarchy group (see
    # models/policy_hierarchy_group.py for why this is nullable+singular
    # rather than the DeviceGroup multi-membership pattern). NOT created by
    # Base.metadata.create_all() on an existing table — see README for the
    # required manual ALTER TABLE step, following the cluster_name /
    # deploy-lockout precedent.
    policy_hierarchy_group_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("policy_hierarchy_groups.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    policy_hierarchy_group: Mapped["PolicyHierarchyGroup | None"] = relationship(
        "PolicyHierarchyGroup", back_populates="hosts"
    )
    # Values for {{NAME}} placeholders (services/variables_service.py); always loaded with the host.
    variables: Mapped[list["HostVariable"]] = relationship(
        "HostVariable", lazy="selectin", cascade="all, delete-orphan", order_by="HostVariable.name"
    )
