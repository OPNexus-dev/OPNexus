"""When did a central rule last match something? (table view of the rules page)

The numbers come from ``rule_hit_service`` (pf counters sampled by the observe loop, stored per firewall and rule label).
A central rule has one deployment per target host - on a CARP pair that is one rule per node, and only the master sees
real traffic - so the usage of a rule is merged over its deployments exactly like the cleanup assistant does for the
nodes of a cluster: the newest hit of any node counts, the observation window starts at the latest start of any node
and "unused" needs every node to have been watched (a node without data makes the verdict "unknown", never "unused").

Only stored data is read, nothing is asked of a firewall. Times, not counters: every apply resets pf's counters.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rule import FirewallRule
from app.models.rule_hit import RuleHit
from app.services import rule_hit_service


async def usage_for_rules(db: AsyncSession, rules: list[FirewallRule], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    host_ids = {d.host_id for r in rules for d in r.deployments if d.opnsense_rule_uuid}
    hits: dict[tuple, RuleHit] = {}
    if host_ids:
        for row in (await db.scalars(select(RuleHit).where(RuleHit.host_id.in_(host_ids)))).all():
            hits[(row.host_id, row.rule_uuid.lower())] = row
    result = []
    for rule in rules:
        deployed = [d for d in rule.deployments if d.opnsense_rule_uuid]
        rows = [hits[k] for d in deployed if (k := (d.host_id, d.opnsense_rule_uuid.lower())) in hits]
        item = {"rule_id": str(rule.id), "state": "unknown", "last_hit_at": None, "watching_since": None,
                "hosts_total": len(deployed), "hosts_tracked": len(rows), "days": rule_hit_service.UNUSED_AFTER_DAYS}
        if rows:
            # merge_cluster_hits joins by rule label; a central rule has a different label on every node, so merge here
            hit_times = [r.last_hit_at for r in rows if r.last_hit_at is not None]
            combined = rule_hit_service.ClusterHit(
                rule_uuid=rows[0].rule_uuid, enabled=rule.enabled and any(r.enabled for r in rows),
                first_seen_at=max(r.first_seen_at for r in rows), last_hit_at=max(hit_times) if hit_times else None,
                last_sampled_at=min(r.last_sampled_at for r in rows), peer_missing=len(rows) < len(deployed))
            state = rule_hit_service.classify(combined, now)
            if state == "unused" and combined.peer_missing:
                state = "unknown"  # one target has no data: not provable
            item.update(state=state, last_hit_at=combined.last_hit_at, watching_since=combined.first_seen_at)
        elif not rule.enabled and deployed:
            item["state"] = "disabled"
        result.append(item)
    return result
