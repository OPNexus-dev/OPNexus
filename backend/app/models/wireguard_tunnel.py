import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.wireguard_tunnel_target import wireguard_tunnel_targets
from app.models.policy import wireguard_tunnel_groups


class WireguardTunnel(Base):
    """A centrally-authored WireGuard interconnect (v1 scope, see
    services/wireguard_deploy_service.py's module docstring for the full
    field verification and the scope decision this class embodies).

    ENTITY-SHAPE DECISION (see wireguard_deploy_service.py for the full
    reasoning): WireGuard's real OPNsense object model is one `Server`
    (the local wg-quick interface: keypair, listen port, own tunnel
    address) that owns a comma-list `peers` field pointing at zero or more
    `Client` (peer) objects — a genuine one-to-many, NOT a 1:1 pairing.
    Nonetheless, this class models exactly ONE Server + exactly ONE Client
    per WireguardTunnel row (a 2-object composite, closest in shape to
    OpenVpnInstance's Instance+optional-StaticKey composite, NOT IPsec's
    5-object one) rather than either (a) a single flat entity, or (b) two
    fully independent top-level CRUD entities (WireguardServer/
    WireguardPeer) that the operator would have to keep in sync by hand.
    Reasoning: the realistic v1 fleet use case named in the plan — one
    interconnect between two managed firewalls, or a managed firewall and
    one external peer — is exactly a 1:1 Server<->Peer pairing, the same
    way IpsecTunnel models "a site-to-site VPN connection" as one form an
    admin actually thinks in, instead of exposing Swanctl's Connection/
    Local/Remote/Child as four separate CRUD screens. A multi-peer WG hub
    (one Server, many Clients) is a real, deliberately deferred v1 gap —
    see the module docstring for why "always exactly one peer" was chosen
    over building that out tonight.

    The peer<->server linkage itself is NOT a plain ModelRelationField
    written by this app directly (unlike IPsec's Local/Remote/Child, which
    carry their own `connection` UUID field this app sets) — it is a
    denormalized, bidirectionally-maintained relationship where the
    SERVER's `peers` field is the only persisted list, and OPNsense's own
    `ClientController::setClientAction`/`delClientAction` (verified
    directly from source, see module docstring) update that list
    server-side as a side effect of writing/deleting the Client, whenever
    this app's Client payload includes a `servers` field naming the
    Server's UUID. This app therefore never reads or rewrites the
    Server's `peers` field itself — seebuild_payloads()/deploy service for
    exactly how the `servers` field is injected at deploy time.

    Targeting mirrors IpsecTunnel/OpenVpnInstance exactly (direct host
    targets + DeviceGroup membership, resolved via
    policy_service.effective_hosts).

    Two secrets live on disk only, never in Postgres as their own column
    (see wireguard_key_store.py, mirroring ipsec_psk_store.py/
    openvpn_key_store.py): the Server's WireGuard private key (required —
    OPNsense's own model requires `privkey`, and does NOT derive a public
    key from it anywhere in the model/field-type source, confirmed by
    reading ServerField.php — so both server_public_key AND the private
    key must be operator-supplied, see module docstring for why this app
    doesn't call `keyPairAction` to generate one), and the peer's optional
    WireGuard pre-shared key (an extra symmetric hardening layer WireGuard
    itself supports per-peer, unrelated to IPsec's PSK despite the shared
    name — OPNsense's `Client.psk` field). `server_private_key_set` /
    `peer_psk_set` just track whether material has been provided, same
    pattern as IpsecTunnel.pre_shared_key_set /
    OpenVpnInstance.tls_key_material_set.

    Deploy status model is deliberately as simple as IpsecTunnel's/
    OpenVpnInstance's — see services/wireguard_deploy_service.py's module
    docstring for why (no pending/confirm/auto-revert window here either,
    same rationale: a WireGuard `reconfigure` can equally sever the
    management path OPNexus would need to auto-revert it)."""

    __tablename__ = "wireguard_tunnels"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # ── Server (local wg-quick interface) — Wireguard\Server.xml
    # servers.server fields, see module docstring for the exact mapping.
    # `name` doubles as this entity's display label (WireGuard's model has
    # no separate free-text `description` field the way Swanctl/OpenVPN
    # do — masked identically to OPNsense's own Mask,
    # `/^([0-9a-zA-Z._\-]){1,64}$/u`, enforced again in schemas/wireguard.py).
    server_name: Mapped[str] = mapped_column(String(64))
    server_public_key: Mapped[str] = mapped_column(String(64))
    server_private_key_set: Mapped[bool] = mapped_column(Boolean, default=False)
    # Server.port (PortField) — kept as a plain string, sent verbatim,
    # same convention as every other OPNsense-validated field in this app.
    listen_port: Mapped[str] = mapped_column(String(8), default="51820")
    # Server.tunneladdress (NetworkField, NetMaskRequired=Y, AsList=Y) —
    # this interface's own tunnel address in CIDR notation. v1 keeps this
    # to a single CIDR (no comma-list UI), same simplification precedent
    # as IpsecTunnel.local_networks/OpenVpnInstance.server_network.
    server_tunnel_address: Mapped[str] = mapped_column(String(255))
    # Server.dns / .mtu — both optional in the OPNsense model.
    dns: Mapped[str] = mapped_column(String(255), default="")
    mtu: Mapped[str] = mapped_column(String(8), default="")

    # ── Peer (Wireguard\Client.xml clients.client fields) — v1 pins
    # exactly one peer per tunnel, see class docstring.
    peer_name: Mapped[str] = mapped_column(String(64))
    peer_public_key: Mapped[str] = mapped_column(String(64))
    peer_psk_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    peer_psk_set: Mapped[bool] = mapped_column(Boolean, default=False)
    # Client.tunneladdress (NetworkField, NetMaskRequired=Y, Required=Y,
    # AsList=Y) — this peer's Allowed-IPs entry in the server's config.
    # Single CIDR only in v1, same precedent as above.
    peer_allowed_ips: Mapped[str] = mapped_column(String(255))
    # Client.serveraddress/.serverport — the Endpoint (host/port) this
    # OPNsense box should dial to reach the peer. Optional: a peer that
    # only ever connects inward (e.g. a road-warrior-style client with no
    # fixed address) legitimately has neither set — see module docstring
    # for why these are believed (not independently confirmed beyond
    # field placement/naming in Client.xml) to be the real, persisted
    # Endpoint fields, as opposed to the sibling `endpoint` field, which
    # is explicitly marked `volatile="true"` in the model XML (i.e.
    # runtime/status-only, never written to config).
    peer_endpoint_address: Mapped[str] = mapped_column(String(255), default="")
    peer_endpoint_port: Mapped[str] = mapped_column(String(8), default="")
    # Client.keepalive (IntegerField, 1-65535) — optional persistent
    # keepalive interval in seconds.
    peer_keepalive: Mapped[str] = mapped_column(String(8), default="")

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    targets: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=wireguard_tunnel_targets)
    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=wireguard_tunnel_groups)
    deployments: Mapped[list["WireguardTunnelDeployment"]] = relationship(
        "WireguardTunnelDeployment", back_populates="tunnel", cascade="all, delete-orphan"
    )
