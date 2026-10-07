from sqlalchemy import Column, ForeignKey, Table

from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.database import Base

alias_targets = Table(
    "alias_targets",
    Base.metadata,
    Column("alias_id", PGUUID(as_uuid=True), ForeignKey("firewall_aliases.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
