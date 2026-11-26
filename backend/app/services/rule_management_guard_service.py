"""Would this rule cut OPNexus (or an admin) off the firewall? (checked before the push, R11)

The automatic revert of a deploy runs in OPNexus and needs the way OPNexus -> firewall API. A rule that blocks that
way makes the revert fail too (``revert_failed``). So the preview asks, per target host and read-only, the same
question the rule analysis answers for a packet: for the packets OPNexus (its own address as the firewall sees it)
and every configured management source send to the firewall's management ports, what is the verdict BEFORE and
AFTER this rule is in place? ``pass`` before and a block after = the rule would lock that path.

This is a before/after comparison of the whole rule list (the draft sits where it would land, like the overlap
check), not "does the rule match": a default-deny block at the end of the list does not warn when an earlier rule
still lets the management packet through. A host that cannot be read is ``unavailable`` and never blocks anything
here; whether the answer is only "unknown" (no source known, order of the list in doubt, interface unresolved) is
reported as ``unknown`` so the interface can ask for an explicit acknowledgement.

Nothing is written to a firewall.
"""
from __future__ import annotations

import copy
import ipaddress
import logging
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.rule import FirewallRule
from app.services import opnsense_client, variables_service
from app.services.opnsense_client import OpnsenseAPIError
from app.services.rule_analysis import context as ctx_mod, coverage, protection
from app.services.rule_analysis.engine import UNKNOWN, Query, eval_leg, guess_iface
from app.services.rule_analysis.normalize import normalize
from app.services.rule_deploy_service import _build_payload
from app.services.rule_overlap_service import DRAFT_UUID, _new_sort_order, _row_from_payload, _tail

logger = logging.getLogger(__name__)

MAX_QUERIES = 240          # per host: sources x destination addresses x ports x interfaces
MAX_DESTINATIONS = 4       # the address in base_url first, then interface addresses
BLOCKING = ("block", "reject")


def _representative(net: ipaddress.IPv4Network | ipaddress.IPv6Network):
    """One address of a management network (the first usable one); a single address is itself."""
    if net.num_addresses <= 2:
        return net.network_address
    return net.network_address + 1


def _interfaces(src, ctx: ctx_mod.Context) -> list[str]:
    guessed = guess_iface(src, ctx)
    return [guessed] if guessed else protection._interfaces(ctx)  # noqa: SLF001 - same list the cleanup tries


def _verdict(rules, ctx, q: Query, iface: str) -> tuple[str | None, str]:
    leg = eval_leg(rules, q, ctx, "in", iface)
    return leg.action, leg.confidence


async def _sources(db: AsyncSession, host: OpnsenseHost) -> tuple[list[tuple[str, object]], bool]:
    """[(who, address)] to test: OPNexus's own address on this firewall plus the configured management sources.
    The bool says whether OPNexus's own address is known."""
    from app.services.rule_cleanup_service import _limits
    found: list[tuple[str, object]] = []
    own_known = False
    try:
        own = await opnsense_client.own_source_address(host)
    except OpnsenseAPIError:
        own = None
    if own:
        try:
            found.append(("opnexus", ipaddress.ip_address(own)))
            own_known = True
        except ValueError:
            pass
    limits = await _limits(db)
    for net in protection.parse_sources(limits.sources):
        addr = _representative(net)
        if not any(a == addr for _, a in found):
            found.append(("admin", addr))
    return found, own_known


def _ports(host: OpnsenseHost, ctx: ctx_mod.Context, extra) -> tuple[list, list[int]]:
    targets, ports = protection.management_targets(host.base_url, ctx, extra)
    return targets[:MAX_DESTINATIONS], ports


async def _host_result(db: AsyncSession, rule: FirewallRule, host: OpnsenseHost, deployed_uuid: str | None) -> dict:
    result = {"host_id": str(host.id), "host_name": host.name, "status": "ok", "risk": "none",
              "opnexus_address_known": None, "findings": []}
    try:
        payload = _build_payload(rule, host)
    except variables_service.MissingVariables:
        return {**result, "status": "skipped"}
    try:
        ctx = await ctx_mod.load_context(host)
        sources, own_known = await _sources(db, host)
        from app.services.rule_cleanup_service import _limits
        extra_ports = (await _limits(db)).ports
        rows = copy.deepcopy(ctx.rows)
        existing = next((r for r in rows if deployed_uuid and str(r.get("uuid", "")).lower() == deployed_uuid.lower()), None)
        in_place = existing is not None and _tail(existing) is not None
        before_rows = copy.deepcopy(rows)
        if in_place:
            rows = [r for r in rows if r is not existing]
            sort_order = str(existing["sort_order"])
        else:
            sort_order = _new_sort_order(rows, payload, ctx)
        rows.append(_row_from_payload(payload, sort_order, DRAFT_UUID))
        before, before_notes = normalize(replace(ctx, rows=before_rows))
        after, after_notes = normalize(replace(ctx, rows=rows))
        draft = next(r for r in after if r.uuid == DRAFT_UUID)
        draft.loaded, draft.pf_numbers = None, []   # not deployed (yet): nothing is "sure" about its position
        doubtful = coverage.order_doubtful(after_notes) or coverage.order_doubtful(before_notes)
    except Exception as exc:  # noqa: BLE001 - a hint must never fail the preview
        logger.info("Management check of rule %s on %s not possible: %s", rule.id, host.name, exc)
        return {**result, "status": "unavailable"}

    result["opnexus_address_known"] = own_known
    # A new pass rule cannot take away a pass; only a blocking rule or the change of an existing one can.
    if payload.get("action") == "pass" and not in_place:
        return result
    if not sources:
        result["risk"] = "unknown"
        result["findings"].append({"who": "none", "reason": "no_source"})
        return result

    targets, ports = _ports(host, ctx, extra_ports)
    queries, unknown = 0, False
    for who, src in sources:
        for dst in (d for d in targets if d.version == src.version):
            for port in ports:
                q = Query(src=src, dst=dst, proto="6", proto_label="tcp", dport=port, direction="in", egress="none")
                for iface in _interfaces(src, ctx):
                    queries += 1
                    if queries > MAX_QUERIES:
                        unknown = True
                        break
                    was, _ = _verdict(before, ctx, q, iface)
                    now, conf = _verdict(after, ctx, q, iface)
                    if was != "pass":
                        continue        # nothing to lose on this path
                    if now in BLOCKING:
                        sure = conf != UNKNOWN and not doubtful
                        result["findings"].append({"who": who, "port": port, "interface": iface, "verdict": now,
                                                   "sure": sure})
                    elif now != "pass":
                        unknown = True
    if any(f.get("sure") for f in result["findings"]):
        result["risk"] = "blocks"
    elif result["findings"] or unknown or doubtful:
        result["risk"] = "unknown"
    if not own_known and result["risk"] == "none":
        # The path the revert needs is the one thing we could not test.
        result["risk"] = "unknown"
        result["findings"].append({"who": "opnexus", "reason": "own_address_unknown"})
    result["findings"] = result["findings"][:8]
    return result


async def check_rule(db: AsyncSession, rule: FirewallRule, hosts: list[OpnsenseHost]) -> dict:
    deployed = {d.host_id: d.opnsense_rule_uuid for d in rule.deployments if d.opnsense_rule_uuid}
    results = [await _host_result(db, rule, host, deployed.get(host.id)) for host in hosts]
    risk = "blocks" if any(r["risk"] == "blocks" for r in results) else (
        "unknown" if any(r["risk"] == "unknown" for r in results) else "none")
    return {"hosts": results, "risk": risk}
