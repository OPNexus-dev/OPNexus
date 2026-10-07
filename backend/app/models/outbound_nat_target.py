from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.database import Base

outbound_nat_targets = Table(
    "outbound_nat_targets",
    Base.metadata,
    Column("outbound_nat_id", PGUUID(as_uuid=True), ForeignKey("outbound_nat_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
