from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.database import Base

ipsec_tunnel_targets = Table(
    "ipsec_tunnel_targets",
    Base.metadata,
    Column("tunnel_id", PGUUID(as_uuid=True), ForeignKey("ipsec_tunnels.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
