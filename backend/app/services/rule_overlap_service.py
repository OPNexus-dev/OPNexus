"""Will this central rule be useless where it is deployed? (shadowed / duplicate, before the push)

The preview of a rule is a pure database view (its token must not depend on the firewalls), so this check is a separate,
read-only call the interface makes next to it. For every target host it reads the rules the same way the cleanup
assistant does (services/rule_analysis), puts the rule as it would be deployed into the list - at its live position when
it already exists on that host, else at the end of its priority group (placement "end") or in front of it ("top") - and
asks the coverage engine:

* ``overlap``: an earlier rule already decides everything this rule matches (shadowed), or does the same (duplicate);
* ``makes_pointless``: the later rules this rule would make pointless (a block-any placed first, for example).

Nothing is written to a firewall. A host that cannot be read is reported as ``unavailable`` and never blocks anything: it
is a hint, like the cleanup candidates, and a draft that is not deployed yet is never judged better than "probable".
"""
import copy
import logging
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.rule import FirewallRule
from app.services import variables_service
from app.services.rule_analysis import context as ctx_mod, coverage
from app.services.rule_analysis.normalize import normalize
from app.services.rule_analysis.service import _managed_rules
from app.services.rule_deploy_service import _build_payload

logger = logging.getLogger(__name__)

DRAFT_UUID = "opnexus-draft"
MAX_LISTED = 5
GROUP_SINGLE, GROUP_INTERFACE_GROUP = 400000, 300000


def _row_from_payload(payload: dict, sort_order: str, uuid_: str) -> dict:
    """A ``searchRule`` row the normalizer reads, built from what OPNexus would send."""
    return {
        "uuid": uuid_, "description": payload.get("description") or "", "enabled": payload.get("enabled", "1"),
        "action": payload.get("action"), "quick": payload.get("quick", "1"), "interface": payload.get("interface"),
        "direction": payload.get("direction", "in"), "ipprotocol": payload.get("ipprotocol", "inet"),
        "protocol": payload.get("protocol", "any"), "source_net": payload.get("source_net", "any"),
        "source_port": payload.get("source_port", ""), "destination_net": payload.get("destination_net", "any"),
        "destination_port": payload.get("destination_port", ""), "sort_order": sort_order, "is_automatic": None,
    }


def _tail(row: dict) -> int | None:
    head, _, tail = str(row.get("sort_order") or "").partition(".")
    return int(tail) if head.isdigit() and tail.isdigit() else None


def _new_sort_order(rows: list[dict], payload: dict, ctx: ctx_mod.Context) -> str:
    """Where a new rule lands: the end (or the front) of the priority group of its interface."""
    group = GROUP_INTERFACE_GROUP if ctx.groups and payload.get("interface") in ctx.groups else GROUP_SINGLE
    tails = [t for r in rows if str(r.get("sort_order") or "").partition(".")[0] == str(group).zfill(6) and (t := _tail(r)) is not None]
    if payload.get("placement") == "top":
        return f"{group:06d}.{max(0, (min(tails) if tails else 1) - 1):07d}"
    return f"{group:06d}.{(max(tails) if tails else 0) + 1:07d}"


def _finding(overlap, managed: dict) -> dict:
    return {"kind": overlap.kind, "confidence": overlap.confidence,
            "by": {"uuid": overlap.by.uuid, "description": overlap.by.description, "enabled": overlap.by.enabled,
                   "managed": managed.get(overlap.by.uuid.lower())}}


async def _host_result(db: AsyncSession, rule: FirewallRule, host: OpnsenseHost, deployed_uuid: str | None) -> dict:
    result = {"host_id": str(host.id), "host_name": host.name, "status": "ok", "overlap": None,
              "makes_pointless": [], "makes_pointless_total": 0}
    try:
        payload = _build_payload(rule, host)
    except variables_service.MissingVariables:
        return {**result, "status": "skipped"}
    try:
        ctx = await ctx_mod.load_context(host)
        rows = copy.deepcopy(ctx.rows)
        existing = next((r for r in rows if deployed_uuid and str(r.get("uuid", "")).lower() == deployed_uuid.lower()), None)
        in_place = existing is not None and _tail(existing) is not None
        if in_place:
            rows = [r for r in rows if r is not existing]  # the new version takes the old one's place
            sort_order = str(existing["sort_order"])
        else:
            sort_order = _new_sort_order(rows, payload, ctx)
        rows.append(_row_from_payload(payload, sort_order, DRAFT_UUID))
        rules, notes = normalize(replace(ctx, rows=rows))
        draft = next(r for r in rules if r.uuid == DRAFT_UUID)
        draft.loaded, draft.pf_numbers = None, []  # not deployed (yet): nothing is "sure" about its position
        managed = await _managed_rules(db, host)
        overlaps = coverage.analyse(rules, ctx, order_guessed=coverage.order_doubtful(notes))
    except Exception as exc:  # noqa: BLE001 - a hint must never fail the preview
        logger.info("Overlap check of rule %s on %s not possible: %s", rule.id, host.name, exc)
        return {**result, "status": "unavailable"}
    if not in_place:   # where a new rule lands is OPNsense's decision (end/front of its group): assumed, never "sure"
        for o in overlaps:
            o.confidence = coverage.weakest(o.confidence, coverage.PROBABLE) or coverage.PROBABLE
    mine = next((o for o in overlaps if o.rule is draft), None)
    later = [o for o in overlaps if o.by is draft]
    result["overlap"] = _finding(mine, managed) if mine else None
    result["makes_pointless_total"] = len(later)
    result["makes_pointless"] = [
        {"uuid": o.rule.uuid, "description": o.rule.description, "kind": o.kind, "confidence": o.confidence,
         "managed": managed.get(o.rule.uuid.lower())}
        for o in later[:MAX_LISTED]]
    return result


async def check_rule(db: AsyncSession, rule: FirewallRule, hosts: list[OpnsenseHost]) -> dict:
    deployed = {d.host_id: d.opnsense_rule_uuid for d in rule.deployments if d.opnsense_rule_uuid}
    results = [await _host_result(db, rule, host, deployed.get(host.id)) for host in hosts]
    findings = [r for r in results if r["overlap"] or r["makes_pointless_total"]]
    return {"hosts": results, "has_findings": bool(findings)}
