import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WireguardTunnelDeployment(Base):
    """Last known deploy result of one WireguardTunnel (one Server + one
    Peer, see WireguardTunnel's docstring) on one host.

    Two opnsense_*_uuid columns, mirroring OpenVpnInstanceDeployment's
    shape: `opnsense_server_uuid` (Wireguard\\Server) is written FIRST
    (the Peer's payload needs to reference it via the `servers` field —
    see wireguard_deploy_service.py for why getting this order wrong would
    silently produce an unlinked peer), `opnsense_peer_uuid`
    (Wireguard\\Client) required to target setClient/delClient there later.

    status: "success" (both objects written AND the follow-up reconfigure
    call was confirmed) or "error". Deliberately NO "pending"/"reverted"/
    "revert_failed" here — see services/wireguard_deploy_service.py's
    module docstring for why this entity has no auto-revert safety net
    (same rationale as IpsecTunnelDeployment/OpenVpnInstanceDeployment: a
    WireGuard reconfigure can cut off the very management path OPNexus
    would need to auto-revert it).

    A host with no WireguardTunnelDeployment row at all is this tunnel's
    "draft" state on that host (never pushed there) — surfaced by
    wireguard_service.preview()'s action="create" for that row, not by a
    persisted status value.
    """

    __tablename__ = "wireguard_tunnel_deployments"
    __table_args__ = (UniqueConstraint("tunnel_id", "host_id", name="uq_wireguard_tunnel_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tunnel_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("wireguard_tunnels.id", ondelete="CASCADE")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opnsense_server_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opnsense_peer_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # The OPNsense-shaped payloads (keyed "server"/"peer") last
    # successfully written to this host — compared against a fresh
    # deploy's payloads to detect a true no-op re-deploy. Like the other
    # two VPN features, deliberately no previous_payload: nothing here is
    # ever restored automatically.
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    tunnel: Mapped["WireguardTunnel"] = relationship("WireguardTunnel", back_populates="deployments")
    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
