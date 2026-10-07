from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.database import Base

port_forward_targets = Table(
    "port_forward_targets",
    Base.metadata,
    Column("port_forward_id", PGUUID(as_uuid=True), ForeignKey("port_forward_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
