from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.database import Base

wireguard_tunnel_targets = Table(
    "wireguard_tunnel_targets",
    Base.metadata,
    Column("tunnel_id", PGUUID(as_uuid=True), ForeignKey("wireguard_tunnels.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
