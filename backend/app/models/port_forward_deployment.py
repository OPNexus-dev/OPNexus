import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PortForwardDeployment(Base):
    """Last known deploy result of one Port Forward rule on one host —
    mirrors RuleDeployment/AliasDeployment exactly; see rule_deployment.py
    for the status-value docstring, unchanged here."""

    __tablename__ = "port_forward_deployments"
    __table_args__ = (UniqueConstraint("port_forward_id", "host_id", name="uq_port_forward_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    port_forward_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("port_forward_rules.id", ondelete="CASCADE")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opnsense_port_forward_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    revert_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    port_forward: Mapped["PortForwardRule"] = relationship("PortForwardRule", back_populates="deployments")
    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
