import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class IpsecTunnelDeployment(Base):
    """Last known deploy result of one tunnel on one host. The five
    opnsense_*_uuid columns are the per-host UUIDs OPNsense assigned to
    this tunnel's Connection/Local/Remote/Child/PreSharedKey objects —
    required to target setX/delX there later, exactly like
    RuleDeployment.opnsense_rule_uuid.

    status: "success" (all 5 objects written AND the follow-up reconfigure
    call was confirmed) or "error" (something failed — see `message`).
    Deliberately NO "pending"/"reverted"/"revert_failed" here — see
    services/ipsec_deploy_service.py's module docstring for why this
    entity has no auto-revert safety net (an IPsec reconfigure can cut off
    the very management path OPNexus would need to auto-revert it).

    A host with no IpsecTunnelDeployment row at all is this tunnel's
    "draft" state on that host (never pushed there) — surfaced by
    ipsec_service.preview()'s action="create" for that row, not by a
    persisted status value. This mirrors how RuleDeployment rows also
    don't exist until a rule's first deploy attempt.
    """

    __tablename__ = "ipsec_tunnel_deployments"
    __table_args__ = (UniqueConstraint("tunnel_id", "host_id", name="uq_ipsec_tunnel_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tunnel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("ipsec_tunnels.id", ondelete="CASCADE")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opnsense_connection_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opnsense_local_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opnsense_remote_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opnsense_child_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opnsense_psk_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # The OPNsense-shaped payloads (keyed "connection"/"local"/"remote"/
    # "child"/"psk") last successfully written to this host — compared
    # against a fresh deploy's payloads to detect a true no-op re-deploy.
    # Unlike RuleDeployment there is deliberately no previous_payload:
    # nothing here is ever restored automatically.
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    tunnel: Mapped["IpsecTunnel"] = relationship("IpsecTunnel", back_populates="deployments")
    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
