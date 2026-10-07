from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.database import Base

one_to_one_targets = Table(
    "one_to_one_targets",
    Base.metadata,
    Column("one_to_one_id", PGUUID(as_uuid=True), ForeignKey("one_to_one_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
