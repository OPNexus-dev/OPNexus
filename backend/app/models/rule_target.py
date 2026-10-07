from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.database import Base

rule_targets = Table(
    "rule_targets",
    Base.metadata,
    Column("rule_id", PGUUID(as_uuid=True), ForeignKey("firewall_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("host_id", PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True),
)
