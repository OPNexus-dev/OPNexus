"""Entry point used by the API: validate a query, load the firewall's context, evaluate."""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from app.models.rule_hit import RuleHit
from app.models.rule_history import LiveRuleState
from app.services import rule_hit_service
from app.services.rule_history_service import BASELINE_KEY
from app.services.rule_analysis import cleanup, coverage
from app.services.rule_analysis import context as ctx_mod
from app.services.rule_analysis.engine import Query, evaluate
from app.services.rule_analysis.normalize import normalize
from app.services.rule_analysis.normalize import PROTO_NUMBERS, norm_proto

logger = logging.getLogger(__name__)


def _ip(value: str | None, field: str):
    try:
        return ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        raise api_error(422, ErrorCode.ANALYSE_ADRESSE_UNGUELTIG, field=field, value=str(value or "")[:64]) from None


def _port(value: str | int | None, field: str) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text.isdigit() or int(text) > 65535:
        raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field=field, value=text[:32])
    return int(text)


def build_query(*, src: str, dst: str, proto: str = "tcp", sport=None, dport=None, icmptype: str | None = None,
                iface: str | None = None, direction: str = "in", egress: str | None = None) -> Query:
    source, destination = _ip(src, "src"), _ip(dst, "dst")
    if source.version != destination.version:
        raise api_error(422, ErrorCode.ANALYSE_FAMILIEN_MISCHUNG)
    label = str(proto or "tcp").strip().lower()
    if label == "icmp" and source.version == 6:
        label = "ipv6-icmp"
    number = norm_proto(label)
    if not (number.isdigit() and int(number) <= 255) and label not in PROTO_NUMBERS:
        raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="proto", value=label[:32])
    q = Query(src=source, dst=destination, proto=number, proto_label=label,
              sport=_port(sport, "sport"), dport=_port(dport, "dport"))
    if (q.sport is not None or q.dport is not None) and number not in ("6", "17"):
        raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="dport", value=str(dport or sport))
    if icmptype not in (None, ""):
        if number not in ("1", "58"):
            raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="icmptype", value=str(icmptype)[:32])
        q.icmptype = str(icmptype).strip().lower()
    if direction not in ("in", "out"):
        raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="direction", value=str(direction)[:8])
    q.direction = direction
    q.iface = None if str(iface or "").strip().lower() in ("", "auto") else str(iface).strip()
    egress_value = str(egress or "").strip().lower()
    q.egress = "none" if egress_value in ("", "none") else (None if egress_value == "auto" else str(egress).strip())
    return q


def check_interfaces(q: Query, ctx: ctx_mod.Context) -> None:
    for name in (q.iface, None if q.egress in (None, "none") else q.egress):
        if name is not None and name not in ctx.interfaces:
            raise api_error(422, ErrorCode.ANALYSE_INTERFACE_UNBEKANNT, interface=name[:64],
                            known=", ".join(sorted(i for i in ctx.interfaces if i != "lo0")))


async def analyze_host(host: OpnsenseHost, q: Query, *, refresh: bool = False) -> dict:
    """Raises api_error for a bad interface; OpnsenseAPIError/httpx errors when the firewall
    cannot be read (callers map that)."""
    ctx = await ctx_mod.load_context(host, use_cache=not refresh)
    check_interfaces(q, ctx)
    await ctx_mod.ensure_refs(host, ctx, [str(q.src), str(q.dst)])
    result = evaluate(ctx, q)
    if any(ctx.refs.get(ip) is None for ip in (str(q.src), str(q.dst))):
        result["notes"] = sorted({*result["notes"], "ALIAS_REFERENCES_UNAVAILABLE"})
    return result


async def _managed_rules(db: AsyncSession, host: OpnsenseHost) -> dict[str, dict]:
    """{label on the firewall: what manages it} for every rule OPNexus deployed to this host."""
    managed: dict[str, dict] = {}
    for dep_uuid, rule_id, description in (await db.execute(
            select(RuleDeployment.opnsense_rule_uuid, FirewallRule.id, FirewallRule.description)
            .join(FirewallRule, FirewallRule.id == RuleDeployment.rule_id)
            .where(RuleDeployment.host_id == host.id, RuleDeployment.opnsense_rule_uuid.is_not(None)))).all():
        managed[dep_uuid.lower()] = {"kind": "rule", "id": str(rule_id), "name": description}
    for dep_uuid, rule_id, description in (await db.execute(
            select(PolicyHierarchyRuleDeployment.opnsense_policy_hierarchy_uuid, PolicyHierarchyRule.id, PolicyHierarchyRule.description)
            .join(PolicyHierarchyRule, PolicyHierarchyRule.id == PolicyHierarchyRuleDeployment.policy_hierarchy_id)
            .where(PolicyHierarchyRuleDeployment.host_id == host.id,
                   PolicyHierarchyRuleDeployment.opnsense_policy_hierarchy_uuid.is_not(None)))).all():
        managed[dep_uuid.lower()] = {"kind": "policy_hierarchy", "id": str(rule_id), "name": description}
    return managed


async def cleanup_candidates(db: AsyncSession, host: OpnsenseHost, *, unused_days: int = cleanup.DEFAULT_UNUSED_DAYS,
                             refresh: bool = False, now: datetime | None = None) -> dict:
    """Read-only. Raises when the firewall cannot be read (callers map that)."""
    now = now or datetime.now(timezone.utc)
    ctx = await ctx_mod.load_context(host, use_cache=not refresh)
    rules, notes = normalize(ctx)
    hits = {r.rule_uuid.lower(): r for r in (await db.scalars(select(RuleHit).where(RuleHit.host_id == host.id))).all()}
    states = {s.rule_uuid: s for s in (await db.scalars(select(LiveRuleState).where(LiveRuleState.host_id == host.id))).all()}
    rows = cleanup.build(rules, ctx, hits=hits, states=states, managed=await _managed_rules(db, host), now=now,
                         unused_days=unused_days, order_guessed=coverage.order_doubtful(notes))
    all_notes = sorted({*notes, *ctx.notes})
    return {
        "host_id": str(host.id), "host_name": host.name, "cluster_name": host.cluster_name,
        "unused_days": max(cleanup.MIN_DAYS, min(cleanup.MAX_DAYS, int(unused_days))), "generated_at": now,
        "rules_total": sum(1 for r in rules if not r.automatic and r.section not in ("antispoof", "nat_auto", "invalid")),
        "candidates": rows, "counts": cleanup.summarize(rows), "notes": all_notes,
        "usage": {
            "tracked": len(hits),
            "collecting": sum(1 for h in hits.values() if rule_hit_service.classify(h, now, unused_days) == "collecting"),
            "watching_since": min((h.first_seen_at for h in hits.values()), default=None),
        },
        "history": {"watching_since": baseline.first_seen_at if (baseline := states.get(BASELINE_KEY)) else None},
    }
