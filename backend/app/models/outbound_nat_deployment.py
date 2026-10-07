import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OutboundNatDeployment(Base):
    """Last known deploy result of one Outbound NAT rule on one host —
    mirrors RuleDeployment/AliasDeployment exactly; see rule_deployment.py
    for the status-value docstring, unchanged here."""

    __tablename__ = "outbound_nat_deployments"
    __table_args__ = (UniqueConstraint("outbound_nat_id", "host_id", name="uq_outbound_nat_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    outbound_nat_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("outbound_nat_rules.id", ondelete="CASCADE")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opnsense_outbound_nat_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    revert_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    outbound_nat: Mapped["OutboundNatRule"] = relationship("OutboundNatRule", back_populates="deployments")
    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
