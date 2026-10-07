from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.database import Base

openvpn_instance_targets = Table(
    "openvpn_instance_targets",
    Base.metadata,
    Column("instance_id", PGUUID(as_uuid=True), ForeignKey("openvpn_instances.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
