"""Additive tables: existing rule/host schemas and data remain valid."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Table, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


group_members = Table(
    "device_group_members", Base.metadata,
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
rule_groups = Table(
    "rule_groups", Base.metadata,
    Column("rule_id", PGUUID(as_uuid=True), ForeignKey("firewall_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)
alias_groups = Table(
    "alias_groups", Base.metadata,
    Column("alias_id", PGUUID(as_uuid=True), ForeignKey("firewall_aliases.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)
port_forward_groups = Table(
    "port_forward_groups", Base.metadata,
    Column("port_forward_id", PGUUID(as_uuid=True), ForeignKey("port_forward_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)


class DeviceGroup(Base):
    __tablename__ = "device_groups"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    hosts: Mapped[list["OpnsenseHost"]] = relationship("OpnsenseHost", secondary=group_members)


class RuleRevision(Base):
    __tablename__ = "rule_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("firewall_rules.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    snapshot: Mapped[dict] = mapped_column(JSON)


class AliasRevision(Base):
    __tablename__ = "alias_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alias_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("firewall_aliases.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    snapshot: Mapped[dict] = mapped_column(JSON)


class PortForwardRevision(Base):
    __tablename__ = "port_forward_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    port_forward_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("port_forward_rules.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    snapshot: Mapped[dict] = mapped_column(JSON)


one_to_one_groups = Table(
    "one_to_one_groups", Base.metadata,
    Column("one_to_one_id", PGUUID(as_uuid=True), ForeignKey("one_to_one_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)

class OneToOneRevision(Base):
    __tablename__ = "one_to_one_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    one_to_one_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("one_to_one_rules.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    snapshot: Mapped[dict] = mapped_column(JSON)



outbound_nat_groups = Table(
    "outbound_nat_groups", Base.metadata,
    Column("outbound_nat_id", PGUUID(as_uuid=True), ForeignKey("outbound_nat_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)

class OutboundNatRevision(Base):
    __tablename__ = "outbound_nat_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    outbound_nat_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("outbound_nat_rules.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    snapshot: Mapped[dict] = mapped_column(JSON)


ipsec_tunnel_groups = Table(
    "ipsec_tunnel_groups", Base.metadata,
    Column("tunnel_id", PGUUID(as_uuid=True), ForeignKey("ipsec_tunnels.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)

class IpsecTunnelRevision(Base):
    __tablename__ = "ipsec_tunnel_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tunnel_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("ipsec_tunnels.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    # Snapshot never includes the PSK secret itself (not stored in the DB
    # at all, see models/ipsec_tunnel.py) — only pre_shared_key_set, so a
    # restored revision reflects whether a key was configured, not what it
    # was.
    snapshot: Mapped[dict] = mapped_column(JSON)


openvpn_instance_groups = Table(
    "openvpn_instance_groups", Base.metadata,
    Column("instance_id", PGUUID(as_uuid=True), ForeignKey("openvpn_instances.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)

class OpenVpnInstanceRevision(Base):
    __tablename__ = "openvpn_instance_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("openvpn_instances.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    # Snapshot never includes the static-key material itself (not stored in
    # the DB at all, see models/openvpn_instance.py) — only
    # tls_key_material_set, mirroring IpsecTunnelRevision's PSK handling.
    snapshot: Mapped[dict] = mapped_column(JSON)


wireguard_tunnel_groups = Table(
    "wireguard_tunnel_groups", Base.metadata,
    Column("tunnel_id", PGUUID(as_uuid=True), ForeignKey("wireguard_tunnels.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)

class WireguardTunnelRevision(Base):
    __tablename__ = "wireguard_tunnel_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tunnel_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("wireguard_tunnels.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    # Snapshot never includes the server private key or peer PSK material
    # itself (not stored in the DB at all, see models/wireguard_tunnel.py)
    # — only server_private_key_set/peer_psk_set, mirroring
    # IpsecTunnelRevision/OpenVpnInstanceRevision's secret handling.
    snapshot: Mapped[dict] = mapped_column(JSON)


class PolicyHierarchyRuleRevision(Base):
    __tablename__ = "policy_hierarchy_rule_revisions"
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_hierarchy_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("policy_hierarchy_rules.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    note: Mapped[str] = mapped_column(String(255))
    # Snapshot includes hierarchy_group_id/rulebase/sequence (unlike the
    # other *Revision classes above) since, unlike a plain rule's
    # target_host_ids/target_group_ids, those three fields directly change
    # deploy ordering/targeting for this entity and must be restorable too.
    snapshot: Mapped[dict] = mapped_column(JSON)

