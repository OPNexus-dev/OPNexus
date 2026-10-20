"""Takes over a firewall rule that was created by hand on a host, so it can be
edited, versioned and deployed from OPNexus like any other rule.

Adoption itself writes NOTHING to the firewall: it records the rule as it is
right now (read fresh from the host at adoption time, never from what the
browser sent) together with its OPNsense UUID. The first change afterwards goes
through the normal preview → push → confirm/auto-revert flow.

Only rules OPNexus can describe without distortion are adopted. A rule using
something the model cannot express (several interfaces, an inverted source, a
non-quick rule, ...) is refused with the reason instead of being imported in a
way that would silently change its meaning on the next edit. Fields the model
does not know about (gateway, state options, schedules, tags, ...) are left
alone on the firewall: setRule only overwrites the fields it is sent.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from app.services import opnsense_client
from app.services.adopt_common import NotAdoptable, NotFoundOnHost, clean, on  # noqa: F401  (NotAdoptable is re-exported for routers/rules.py)
from app.services.policy_service import revision
from app.services.rule_deploy_service import _build_payload


class RuleNotFoundOnHost(NotFoundOnHost):
    pass


_on = on
_clean = clean


def reasons_not_adoptable(row: dict, managed_uuids: set[str]) -> list[str]:
    """Machine-readable reasons (the UI translates them); empty = adoptable."""
    reasons = []
    if row.get("is_automatic"):
        reasons.append("automatic")
    if _clean(row.get("uuid")) in managed_uuids:
        reasons.append("already_managed")
    interface = _clean(row.get("interface"))
    if not interface or "," in interface:
        reasons.append("interface_not_single")
    if _on(row.get("interfacenot")):
        reasons.append("negated_interface")
    if _on(row.get("source_not")):
        reasons.append("negated_source")
    if _on(row.get("destination_not")):
        reasons.append("negated_destination")
    if _clean(row.get("received-on")):
        reasons.append("received_on")
    if _clean(row.get("quick")) != "1":
        reasons.append("not_quick")
    if _clean(row.get("direction")) not in ("in", "out"):
        reasons.append("direction_unsupported")
    if _clean(row.get("action")) not in ("pass", "block", "reject"):
        reasons.append("action_unsupported")
    if _clean(row.get("ipprotocol")) not in ("inet", "inet6"):
        reasons.append("ipprotocol_unsupported")
    if not _clean(row.get("description")):
        reasons.append("no_description")
    return reasons


async def _managed_uuids(db: AsyncSession, host: OpnsenseHost) -> set[str]:
    rows = await db.scalars(
        select(RuleDeployment.opnsense_rule_uuid).where(
            RuleDeployment.host_id == host.id, RuleDeployment.opnsense_rule_uuid.is_not(None)
        )
    )
    return set(rows.all())


async def _live_rows(host: OpnsenseHost) -> list[dict]:
    data = await opnsense_client.search_rules(host, all_rows=True)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [r for r in rows if isinstance(r, dict) and r.get("uuid")]


async def list_unmanaged(db: AsyncSession, host: OpnsenseHost) -> list[dict]:
    managed = await _managed_uuids(db, host)
    result = []
    for row in await _live_rows(host):
        if row.get("is_automatic"):
            continue  # built-in/system rules never show up here at all
        reasons = reasons_not_adoptable(row, managed)
        if "already_managed" in reasons:
            continue
        result.append({
            "uuid": _clean(row.get("uuid")),
            "description": _clean(row.get("description")),
            "interface": _clean(row.get("interface")),
            "interface_label": _clean(row.get("%interface")) or _clean(row.get("interface")),
            "direction": _clean(row.get("direction")),
            "action": _clean(row.get("action")),
            "protocol": _clean(row.get("protocol")) or "any",
            "source_net": _clean(row.get("source_net")) or "any",
            "source_port": _clean(row.get("source_port")),
            "destination_net": _clean(row.get("destination_net")) or "any",
            "destination_port": _clean(row.get("destination_port")),
            "enabled": _on(row.get("enabled")),
            "log": _on(row.get("log")),
            "adoptable": not reasons,
            "reasons": reasons,
        })
    return result


async def adopt(db: AsyncSession, host: OpnsenseHost, rule_uuid: str) -> FirewallRule:
    managed = await _managed_uuids(db, host)
    row = next((r for r in await _live_rows(host) if _clean(r.get("uuid")) == rule_uuid), None)
    if row is None:
        raise RuleNotFoundOnHost(rule_uuid)
    reasons = reasons_not_adoptable(row, managed)
    if reasons:
        raise NotAdoptable(reasons)

    rule = FirewallRule(
        description=_clean(row.get("description")),
        interface=_clean(row.get("interface")),
        direction=_clean(row.get("direction")),
        ipprotocol=_clean(row.get("ipprotocol")),
        protocol=_clean(row.get("protocol")) or "any",
        action=_clean(row.get("action")),
        source_net=_clean(row.get("source_net")) or "any",
        source_port=_clean(row.get("source_port")) or None,
        destination_net=_clean(row.get("destination_net")) or "any",
        destination_port=_clean(row.get("destination_port")) or None,
        enabled=_on(row.get("enabled")),
        log=_on(row.get("log")),
        targets=[host],
        groups=[],
    )
    db.add(rule)
    await db.flush()
    revision(db, rule, "Von Firewall übernommen")
    db.add(RuleDeployment(
        rule_id=rule.id,
        host_id=host.id,
        status="success",
        message="Von der Firewall übernommen",
        opnsense_rule_uuid=rule_uuid,
        last_payload=_build_payload(rule),
        previous_payload=None,
    ))
    await db.flush()
    return rule
