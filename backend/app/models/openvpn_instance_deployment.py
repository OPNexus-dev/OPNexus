import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OpenVpnInstanceDeployment(Base):
    """Last known deploy result of one OpenVPN instance on one host.

    Two opnsense_*_uuid columns, not one — see OpenVpnInstance's docstring
    for why this entity isn't perfectly flat: `opnsense_statickey_uuid` is
    only populated when tls_key_enabled (the StaticKeys.StaticKey object,
    written FIRST so its UUID can be injected into the Instance's tls_key
    field), `opnsense_instance_uuid` is the Instances.Instance object
    itself, required to target setAction/delAction there later — exactly
    like IpsecTunnelDeployment.opnsense_connection_uuid etc.

    status: "success" (the instance — and, if enabled, its StaticKey —
    were written AND the follow-up reconfigure call was confirmed) or
    "error". Deliberately NO "pending"/"reverted"/"revert_failed" here —
    see services/openvpn_deploy_service.py's module docstring for why this
    entity has no auto-revert safety net (same rationale as
    IpsecTunnelDeployment: an OpenVPN reconfigure can cut off the very
    management path OPNexus would need to auto-revert it).

    A host with no OpenVpnInstanceDeployment row at all is this instance's
    "draft" state on that host (never pushed there) — surfaced by
    openvpn_service.preview()'s action="create" for that row, not by a
    persisted status value.
    """

    __tablename__ = "openvpn_instance_deployments"
    __table_args__ = (UniqueConstraint("instance_id", "host_id", name="uq_openvpn_instance_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("openvpn_instances.id", ondelete="CASCADE")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opnsense_statickey_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opnsense_instance_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # The OPNsense-shaped payloads (keyed "statickey"/"instance") last
    # successfully written to this host — compared against a fresh
    # deploy's payloads to detect a true no-op re-deploy. Like
    # IpsecTunnelDeployment there is deliberately no previous_payload:
    # nothing here is ever restored automatically.
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    instance: Mapped["OpenVpnInstance"] = relationship("OpenVpnInstance", back_populates="deployments")
    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
