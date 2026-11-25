"""Does OPNexus still know which live rule belongs to a central rule on a firewall? (deployment links)

OPNexus records the OPNsense UUID of every rule it deploys, per target host. Anything that replaces the rules of a
firewall behind its back makes those links stale: the typical case is OPNsense's own HA config sync (``System > High
Availability``, item "Firewall rules") - the primary pushes its rules to the secondary, the secondary then holds the
PRIMARY's UUIDs instead of the ones OPNexus recorded when it deployed there. The rules are all in place, but the live
view calls them "local" on that node and a later edit would hit a UUID that no longer exists.

This module finds such links and puts them right. It only ever changes OPNexus's own records, never a firewall:

* a **candidate** for a stale deployment is a live rule on that host that no deployment claims and whose content equals
  what OPNexus deployed there (description, action, interface, direction, protocol, source, destination, ports);
* it is **strong** when the candidate also carries the UUID of the same central rule's deployment on another node (that
  is exactly what a config sync produces). Strong repairs are made automatically by the observe loop; content-only
  candidates are offered in the interface and applied on request;
* a live rule that carries the partner's UUID but whose content differs from what OPNexus deployed (someone changed it by
  hand on the primary, the sync copied that) is a **drift** candidate: the assignment is certain, the rule differs from the
  central one. It is only offered in the interface, with the differing fields, never repaired automatically;
* more than one candidate for a deployment, or one candidate for several deployments, is ambiguous and left alone.
"""
import hashlib
import json
import logging
import uuid as uuid_mod

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from app.services import audit_service, rule_order_service

logger = logging.getLogger(__name__)

STRONG, CONTENT, DRIFT = "partner_uuid", "content", "partner_uuid_drift"
# deployments in flight (or whose rollback is open) are never touched
SETTLED = ("success", "reverted")


def _flat(value) -> str:
    return str(value if value is not None else "").strip().lower()


def _proto(value) -> str:
    return _flat(value) or "any"


def _net(value) -> str:
    return _flat(value) or "any"


def fingerprint_payload(payload: dict) -> tuple:
    return (_flat(payload.get("description")), _flat(payload.get("action")), _flat(payload.get("interface")),
            _flat(payload.get("direction")) or "in", _proto(payload.get("protocol")), _net(payload.get("source_net")),
            _flat(payload.get("source_port")), _net(payload.get("destination_net")), _flat(payload.get("destination_port")))


FIELDS = ("description", "action", "interface", "direction", "protocol", "source_net", "source_port", "destination_net", "destination_port")


def differences(recorded: tuple, live: tuple) -> list[dict]:
    return [{"field": name, "recorded": a, "live": b} for name, a, b in zip(FIELDS, recorded, live) if a != b]


def fingerprint_row(row: dict) -> tuple:
    return (_flat(row.get("description")), _flat(row.get("action")), _flat(row.get("interface")),
            _flat(row.get("direction")) or "in", _proto(row.get("protocol")), _net(row.get("source_net")),
            _flat(row.get("source_port")), _net(row.get("destination_net")), _flat(row.get("destination_port")))


async def proposals(db: AsyncSession, host: OpnsenseHost, search: dict) -> dict:
    """{"items": [...], "ambiguous": n} for ``host`` from a ``searchRule`` answer that is already read."""
    live = {str(r["uuid"]).lower(): r for r in (search or {}).get("rows") or []
            if isinstance(r, dict) and r.get("uuid") and not rule_order_service._truthy(r.get("is_automatic"))}
    mine = (await db.scalars(select(RuleDeployment).where(RuleDeployment.host_id == host.id,
                                                           RuleDeployment.opnsense_rule_uuid.is_not(None)))).all()
    claimed = {d.opnsense_rule_uuid.lower() for d in mine if d.opnsense_rule_uuid.lower() in live}
    claimed |= {u.lower() for u in await rule_order_service.hierarchy_uuids(db, host)}
    stale = [d for d in mine if d.opnsense_rule_uuid.lower() not in live and d.status in SETTLED and d.last_payload]
    items, ambiguous, used = [], 0, {}
    for dep in stale:
        want = fingerprint_payload(dep.last_payload)
        candidates = [u for u, r in live.items() if u not in claimed and fingerprint_row(r) == want]
        partners = {x.opnsense_rule_uuid.lower() for x in (await db.scalars(select(RuleDeployment).where(
            RuleDeployment.rule_id == dep.rule_id, RuleDeployment.host_id != host.id,
            RuleDeployment.opnsense_rule_uuid.is_not(None)))).all()}
        strong = [u for u in candidates if u in partners]
        pick = strong[0] if len(strong) == 1 else candidates[0] if len(candidates) == 1 and not strong else None
        diff: list[dict] = []
        if pick is None and not candidates:
            drifted = [u for u in partners if u in live and u not in claimed]   # the partner's rule is here, with other content
            if len(drifted) == 1:
                pick = drifted[0]
                diff = differences(want, fingerprint_row(live[pick]))
        if pick is None:
            ambiguous += 1
            continue
        used.setdefault(pick, []).append(dep.id)
        items.append({"deployment_id": str(dep.id), "rule_id": str(dep.rule_id), "stored_uuid": dep.opnsense_rule_uuid,
                      "live_uuid": pick, "strength": DRIFT if diff else STRONG if pick in partners else CONTENT,
                      "differences": diff, "live_description": live[pick].get("description") or ""})
    clash = {u for u, ids in used.items() if len(ids) > 1}   # one live rule claimed by two deployments: ambiguous
    kept = [i for i in items if i["live_uuid"] not in clash]
    ambiguous += len(items) - len(kept)
    names = {r.id: r.description for r in (await db.scalars(select(FirewallRule).where(
        FirewallRule.id.in_([uuid_mod.UUID(i["rule_id"]) for i in kept])))).all()} if kept else {}
    for i in kept:
        i["rule_description"] = names.get(uuid_mod.UUID(i["rule_id"]), "")
    return {"items": kept, "ambiguous": ambiguous, "token": token(host, kept)}


def token(host: OpnsenseHost, items: list[dict]) -> str:
    state = sorted((i["deployment_id"], i["stored_uuid"], i["live_uuid"]) for i in items)
    return hashlib.sha256(json.dumps([str(host.id), state]).encode()).hexdigest()


async def apply(db: AsyncSession, host: OpnsenseHost, items: list[dict], *, actor: str, only_strong: bool = False) -> int:
    """Point the deployments at the live rules found. Only OPNexus's own records change; one audit event per link."""
    done = 0
    token_ = audit_service.context.set({"actor": actor[:150], "id": str(uuid_mod.uuid4()), "action": "relink"})
    try:
        for item in items:
            if only_strong and item["strength"] != STRONG:
                continue
            dep = await db.get(RuleDeployment, uuid_mod.UUID(item["deployment_id"]))
            if dep is None or dep.opnsense_rule_uuid != item["stored_uuid"]:
                continue
            dep._skip_audit = True   # the generic deployment history would call this a "rollback"; it is logged below
            dep.opnsense_rule_uuid = item["live_uuid"]
            await db.flush()
            dep._skip_audit = False
            db.add(audit_service.make_event(
                kind="rule", action="relink", status="success", entity_id=item["rule_id"],
                entity_name=item.get("rule_description") or None, host_id=str(host.id), host_name=host.name,
                message=f"Verknüpfung auf {host.name} neu gesetzt ({item['strength']})",
                details={"stored_uuid": item["stored_uuid"], "live_uuid": item["live_uuid"], "strength": item["strength"]}))
            done += 1
    finally:
        audit_service.context.reset(token_)
    if done:
        await db.commit()
        logger.info("Rule links of %s repaired: %d", host.name, done)
    return done


async def reconcile(db: AsyncSession, host: OpnsenseHost, search: dict) -> int:
    """The observe loop's automatic part: strong links (the partner's UUID is live here, content equal) only."""
    found = await proposals(db, host, search)
    return await apply(db, host, found["items"], actor="system:relink", only_strong=True)
