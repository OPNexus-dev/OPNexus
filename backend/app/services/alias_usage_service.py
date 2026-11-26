"""Which centrally managed objects use this alias? ("Used by", R5)

A pure database view over what OPNexus manages itself: central rules, port forwards, 1:1 NAT, outbound NAT, hierarchy
rules and the content of other aliases. Objects that only exist on a firewall (created by hand there) are not part of
it; the interface says so. The same answer protects the alias from being deleted while something still refers to it.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alias import FirewallAlias
from app.models.one_to_one import OneToOneRule
from app.models.outbound_nat import OutboundNatRule
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.port_forward import PortForwardRule
from app.models.rule import FirewallRule

FIELDS = ("source_net", "source_port", "destination_net", "destination_port", "target", "local_port", "target_port")
# kind -> (model, fields that can hold an alias name)
SOURCES = {
    "rule": FirewallRule,
    "port_forward": PortForwardRule,
    "one_to_one": OneToOneRule,
    "outbound_nat": OutboundNatRule,
    "hierarchy_rule": PolicyHierarchyRule,
}
MAX_LISTED = 25


def _refers(value, name: str) -> bool:
    """OPNsense writes an alias name as the whole field value; "!name" (inverted) and a comma list are tolerated."""
    if not value:
        return False
    return any(part.strip().lstrip("!") == name for part in str(value).split(","))


async def usage(db: AsyncSession, alias: FirewallAlias) -> dict:
    items: list[dict] = []
    for kind, model in SOURCES.items():
        for row in (await db.scalars(select(model))).all():
            fields = [f for f in FIELDS if hasattr(row, f) and _refers(getattr(row, f), alias.name)]
            if fields:
                items.append({"kind": kind, "id": str(row.id), "name": getattr(row, "description", "") or str(row.id), "fields": fields})
    for other in (await db.scalars(select(FirewallAlias).where(FirewallAlias.id != alias.id))).all():
        if any(line.strip() == alias.name for line in (other.content or "").splitlines()):
            items.append({"kind": "alias", "id": str(other.id), "name": other.name, "fields": ["content"]})
    items.sort(key=lambda i: (i["kind"], i["name"].lower()))
    return {"total": len(items), "items": items[:MAX_LISTED], "truncated": len(items) > MAX_LISTED}
