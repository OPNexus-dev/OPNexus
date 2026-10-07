import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AliasDeployment(Base):
    """Last known deploy result of one alias on one host — mirrors
    RuleDeployment exactly (see that model for the status meanings);
    opnsense_alias_uuid is the UUID OPNsense assigned this alias on THAT
    host, needed to target setItem/delItem there later."""

    __tablename__ = "alias_deployments"
    __table_args__ = (UniqueConstraint("alias_id", "host_id", name="uq_alias_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alias_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("firewall_aliases.id", ondelete="CASCADE")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opnsense_alias_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    revert_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    alias: Mapped["FirewallAlias"] = relationship("FirewallAlias", back_populates="deployments")
    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
