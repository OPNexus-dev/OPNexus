import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.ipsec_tunnel_target import ipsec_tunnel_targets
from app.models.policy import ipsec_tunnel_groups


class IpsecTunnel(Base):
    """A centrally-authored site-to-site IPsec tunnel (PSK auth only — v1
    scope, see README/plan). Hides 5 linked OPNsense objects (Swanctl
    Connection, Local, Remote, Child, and IPsec.preSharedKeys.preSharedKey)
    behind one composite form, mirroring how an admin actually thinks about
    "a VPN tunnel to site X" rather than five separate model array items —
    see services/ipsec_deploy_service.py for exactly how each field maps.

    Targeting mirrors FirewallRule exactly (direct host targets + DeviceGroup
    membership, resolved via policy_service.effective_hosts) — the classic
    use case is pushing the identical tunnel config to both HA cluster
    members (CARP VIP as local endpoint).

    The pre-shared-key SECRET itself is never stored here — like
    OpnsenseHost's OPNsense API secret (see opnsense_secret_store.py), it
    lives only on disk, one file per tunnel id (see ipsec_psk_store.py).
    pre_shared_key_set just tracks whether one has been provided yet.

    Deploy status model is deliberately simpler than FirewallRule's — see
    services/ipsec_deploy_service.py's module docstring for why (no
    pending/confirm/auto-revert window here, on purpose)."""

    __tablename__ = "ipsec_tunnels"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    description: Mapped[str] = mapped_column(String(255))
    # Connection.local_addrs / remote_addrs (IKEAddressField) — v1 keeps
    # this to a single IP/hostname per side (no comma-separated list in the
    # form), matching the "simple 1:1 network-to-network tunnel" v1 scope.
    local_address: Mapped[str] = mapped_column(String(255))
    remote_address: Mapped[str] = mapped_column(String(255))
    # Connection.version: "ike" (0, IKEv1+IKEv2), "ikev1" (1), "ikev2" (2).
    ike_version: Mapped[str] = mapped_column(String(8), default="ikev2")
    # Connection.proposals (IPsecProposalField, phase 1). "default" is
    # OPNsense's own model default (Swanctl.xml <Default>default</Default>)
    # — the conservative choice documented in ipsec_deploy_service.py.
    proposal: Mapped[str] = mapped_column(String(128), default="default")
    # locals.local.id (this side's IKE identity) — also becomes
    # preSharedKeys.preSharedKey.ident. Required: PSK lookup keys off it.
    local_identity: Mapped[str] = mapped_column(String(255))
    # remotes.remote.id — also becomes preSharedKeys.preSharedKey.remote_ident.
    # Optional in the OPNsense model (unlike local id); default "".
    remote_identity: Mapped[str] = mapped_column(String(255), default="")
    pre_shared_key_set: Mapped[bool] = mapped_column(Boolean, default=False)
    # children.child.local_ts / remote_ts (NetworkField, AsList=Y) —
    # comma-separated CIDRs; v1 form still just takes a single CIDR each,
    # matching the "no pools / wildcard traffic selectors" v1 scope.
    local_networks: Mapped[str] = mapped_column(String(255))
    remote_networks: Mapped[str] = mapped_column(String(255))
    # children.child.esp_proposals (IPsecProposalField, phase 2). "default"
    # for the same reason as `proposal` above.
    esp_proposal: Mapped[str] = mapped_column(String(128), default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    targets: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=ipsec_tunnel_targets)
    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=ipsec_tunnel_groups)
    deployments: Mapped[list["IpsecTunnelDeployment"]] = relationship(
        "IpsecTunnelDeployment", back_populates="tunnel", cascade="all, delete-orphan"
    )
