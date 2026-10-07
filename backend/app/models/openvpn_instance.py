import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.openvpn_instance_target import openvpn_instance_targets
from app.models.policy import openvpn_instance_groups


class OpenVpnInstance(Base):
    """A centrally-authored OpenVPN instance (server OR client role — v1
    scope, see services/openvpn_deploy_service.py's module docstring for
    the full field verification and the scope decision this class embodies).

    Unlike IpsecTunnel (5 linked OPNsense objects), OPNsense's modern
    "Instance" object (OpenVPN.xml -> Instances.Instance, 24.1+) is a
    single flat model item — EXCEPT for the optional tls-auth/tls-crypt
    control-channel hardening key, which is its OWN separate OPNsense
    object (StaticKeys.StaticKey) referenced from the Instance via the
    `tls_key` ModelRelationField. So this entity is closer to a 2-object
    composite (StaticKey optionally created first, Instance referencing
    its UUID) than a perfectly flat single-row entity like FirewallRule —
    see OpenVpnInstanceDeployment's docstring for the resulting 2-UUID
    shape, and openvpn_deploy_service.py for exactly why.

    IMPORTANT SCOPE FINDING (verified directly against OpenVPN.xml, see
    openvpn_deploy_service.py's module docstring): the modern Instance
    model has NO "shared-key-only" role (unlike legacy pre-24.1
    openvpn-server/-client sections, which had a `mode` of
    "p2p_shared_key" needing no PKI at all) — `role` here is only
    "server"/"client", and BOTH still fundamentally require a working
    TLS handshake (a `cert` + `ca`, both ModelRelationFields into
    OPNsense's own certificate store) to actually pass traffic. The
    `tls_key`/StaticKey mechanism this class supports is only an
    OPTIONAL hardening layer on TOP of that TLS handshake (OpenVPN's
    --tls-auth/--tls-crypt), not a substitute for certificates — so,
    unlike IPsec's PSK, it can't be this entity's ONLY form of "auth
    material". cert_ref/ca_ref reference an already-existing certificate/CA
    on the TARGET host — but a cert's UUID is host-local, so a single
    shared string can't be correct for more than one target host at once
    (confirmed as a real, not hypothetical, gap: instances already support
    genuine multi-host targeting below). cert_refs/ca_refs are therefore
    **per-host maps** (`{host_id_str: uuid}`, JSON columns), populated via
    the frontend's per-target-host picker (backed by this project's
    existing read-only `GET /api/hosts/{id}/certificates` plus the sibling
    `GET /api/hosts/{id}/cas`) rather than a single hand-typed value. A
    target host missing from either map fails closed at deploy time — see
    openvpn_deploy_service.py's build_payloads().

    Targeting mirrors IpsecTunnel/FirewallRule exactly (direct host
    targets + DeviceGroup membership, resolved via
    policy_service.effective_hosts) — realistic v1 use cases: an
    identical server-role instance pushed to both HA members sharing a
    CARP VIP, or a client-role instance pointed at one specific remote
    peer (targeted at just that one host).

    The static key's raw material is never stored here — like
    IpsecTunnel's PSK, it lives only on disk, one file per instance id
    (see openvpn_key_store.py). tls_key_material_set just tracks whether
    one has been provided yet. tls_key_enabled/tls_key_mode/material are
    all optional — a valid instance can exist with tls hardening off.

    Deploy status model is deliberately as simple as IpsecTunnel's — see
    services/openvpn_deploy_service.py's module docstring for why (no
    pending/confirm/auto-revert window here either, same rationale: an
    OpenVPN `reconfigure` can equally sever the management path OPNexus
    would need to auto-revert it)."""

    __tablename__ = "openvpn_instances"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    description: Mapped[str] = mapped_column(String(255))
    # Instances.Instance.role — OptionValues are literally only
    # "client"/"server" (verified against OpenVPN.xml), no p2p/shared-key
    # role exists in the modern model.
    role: Mapped[str] = mapped_column(String(8), default="server")
    # Instances.Instance.proto — model OptionValues also include
    # udp4/udp6/tcp4/tcp6; v1 only exposes the dual-stack "udp"/"tcp"
    # defaults, same "don't over-engineer" simplification IPsec applied
    # to its own proposal/esp_proposal fields.
    proto: Mapped[str] = mapped_column(String(8), default="udp")
    # Instances.Instance.port (PortField) — kept as a plain string, sent
    # verbatim, same as every other OPNsense-validated field in this app.
    port: Mapped[str] = mapped_column(String(8), default="1194")
    # Instances.Instance.local (NetworkField, no netmask) — server-role
    # bind address; blank means "any" (OPNsense's own model default).
    local: Mapped[str] = mapped_column(String(255), default="")
    # Instances.Instance.remote (RemoteHostField) — client-role remote
    # peer ("host" or "host:port"); required for role=client, sent
    # verbatim with no comma-list building (v1 keeps to one peer).
    remote: Mapped[str] = mapped_column(String(255), default="")
    # Instances.Instance.server (NetworkField, NetMaskRequired=Y) —
    # server-role tunnel network in CIDR notation; required for role=server.
    server_network: Mapped[str] = mapped_column(String(255), default="")
    # Instances.Instance.cert / .ca (CertificateField -> ModelRelationField
    # into Trust\Cert) — per-host maps ({host_id_str: uuid}), see class
    # docstring for why a single shared value can't work across hosts.
    cert_refs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'::json"))
    ca_refs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'::json"))
    # Whether to also create/attach a StaticKeys.StaticKey for
    # tls-auth/tls-crypt hardening (Instances.Instance.tls_key). Optional —
    # see class docstring.
    tls_key_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # StaticKeys.StaticKey.mode — OptionValues are auth/crypt/crypt-v2;
    # v1 exposes auth/crypt only (crypt-v2 needs the "server" vs "client"
    # key-half distinction genKeyAction's tls-crypt-v2-server type hints
    # at, deliberately deferred as extra complexity not needed for the v1
    # HA-pair / single-peer use cases).
    tls_key_mode: Mapped[str] = mapped_column(String(16), default="crypt")
    tls_key_material_set: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    targets: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=openvpn_instance_targets)
    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=openvpn_instance_groups)
    deployments: Mapped[list["OpenVpnInstanceDeployment"]] = relationship(
        "OpenVpnInstanceDeployment", back_populates="instance", cascade="all, delete-orphan"
    )
