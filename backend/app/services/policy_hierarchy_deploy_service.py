"""1:1 port of rule_deploy_service.py for PolicyHierarchyRule, plus the
positioning step this entity needs and plain rules don't: OPNsense has no
concept of "this rule belongs to hierarchy group X" — every rule from
every level of the tree lands in the same flat per-interface rule list on
each host. Since every OPNexus-pushed rule sets "quick": "1" (strict
first-match), the actual ORDER those rules end up in on the firewall is
what makes the inheritance real, not just cosmetic — see
policy_hierarchy_service.ordered_hierarchy_rules() for the target order,
and reassert_ordering() below for how it's actually established via
`moveRuleBefore`.

Deploy lockout protection is identical to rule_deploy_service.py: a
successful deploy goes to "pending" with a revert_deadline, confirm_rule()
is the human "I still have access" signal, and
sweep_expired_hierarchy_deployments() (called from main.py's background
loop) reverts it if that doesn't happen in time.

One thing this entity does differently: if the rule content itself lands
successfully (addRule/setRule + apply all succeed) but the follow-up
`moveRuleBefore` positioning fails, the deployment goes straight to
"error", not "pending" — the rule is live on the firewall but not
necessarily in the position needed for other "quick" rules on that
interface to still mean what they're supposed to mean. That's a
security-relevant fact, not "unconfirmed", so it must not be reported as a
mere pending state.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.host import OpnsenseHost
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.services.audit_service import tracked_rollback
from app.services import opnsense_client
from app.services.deploy_flow import push_to_host
from app.services.opnsense_client import OpnsenseAmbiguousError, OpnsenseAPIError
from app.services.policy_hierarchy_service import ancestor_chain, descendants, ordered_hierarchy_rules


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_payload(rule: PolicyHierarchyRule) -> dict:
    return {
        "enabled": "1" if rule.enabled else "0",
        "action": rule.action,
        "quick": "1",
        "interface": rule.interface,
        "direction": rule.direction,
        "ipprotocol": rule.ipprotocol,
        "protocol": rule.protocol,
        "source_net": rule.source_net,
        "source_port": rule.source_port or "",
        "destination_net": rule.destination_net,
        "destination_port": rule.destination_port or "",
        "description": rule.description,
        "log": "0",
    }


async def _load_all_groups(db: AsyncSession) -> list[PolicyHierarchyGroup]:
    return list((await db.scalars(
        select(PolicyHierarchyGroup).options(selectinload(PolicyHierarchyGroup.hosts))
    )).all())


async def _load_all_rules(db: AsyncSession) -> list[PolicyHierarchyRule]:
    return list((await db.scalars(select(PolicyHierarchyRule))).all())


async def reassert_ordering(db: AsyncSession, host: OpnsenseHost, interface: str) -> None:
    """Re-establishes the correct pre- AND post-rulebase order for every
    currently-deployed hierarchy rule this host inherits on `interface`.

    Raises OpnsenseAPIError (uncaught) on the first failed move — callers
    either propagate that as a deploy error (see _deploy_to_host) or
    collect it per-host (see the group-wide reassert-ordering endpoint).

    Uses backward chaining, not forward: moveRuleBefore(selected, target)
    is a pairwise primitive with no "move to start/end" sentinel. Given a
    target order [r1..rN], moving forward (r1 before r2, then r2 before
    r3, ...) re-disturbs pairs already fixed by an earlier move. Moving
    backward — for i from N down to 2, moveRuleBefore(r[i-2], r[i-1]) —
    each move only ever repositions a rule relative to one that is already
    in its final place and never touched again.
    """
    all_groups = await _load_all_groups(db)
    all_rules = await _load_all_rules(db)
    deployments_by_rule = {
        d.policy_hierarchy_id: d
        for d in (await db.scalars(
            select(PolicyHierarchyRuleDeployment).where(PolicyHierarchyRuleDeployment.host_id == host.id)
        )).all()
    }
    desired_by_rulebase: dict[str, list[str]] = {}
    for rulebase in ("pre", "post"):
        desired_by_rulebase[rulebase] = []
        for rule in ordered_hierarchy_rules(host, interface, rulebase, all_groups, all_rules):
            dep = deployments_by_rule.get(rule.id)
            if dep and dep.opnsense_policy_hierarchy_uuid and dep.status in ("pending", "success", "reverted"):
                desired_by_rulebase[rulebase].append(dep.opnsense_policy_hierarchy_uuid)

    hierarchy_uuids = set(desired_by_rulebase["pre"] + desired_by_rulebase["post"])
    if not hierarchy_uuids:
        return

    # searchRule is sorted by OPNsense's sort_order and returns MVC rules
    # with their prio_group. Preserve every non-hierarchy MVC rule in that
    # live order, then bracket it with inherited pre/post rules. Legacy and
    # automatic rules are not movable through moveRuleBefore and are left
    # untouched. Verified against FilterController::searchRuleAction and
    # moveRuleBeforeAction in opnsense/core.
    live = await opnsense_client.search_rules(host, all_rows=True)
    rows = live.get("rows", []) if isinstance(live, dict) else []
    uuid_to_group = {
        row.get("uuid"): row.get("prio_group")
        for row in rows
        if row.get("uuid") in hierarchy_uuids and row.get("prio_group")
    }

    # Compatibility fallback for an incomplete/older search response. It
    # still restores hierarchy-to-hierarchy ordering and avoids guessing
    # about external anchors that were not returned by the firewall.
    if set(uuid_to_group) != hierarchy_uuids:
        for anchors in desired_by_rulebase.values():
            for i in range(len(anchors), 1, -1):
                await opnsense_client.move_rule_before(host, anchors[i - 2], anchors[i - 1])
        return

    for prio_group in sorted(set(uuid_to_group.values())):
        pre = [uuid for uuid in desired_by_rulebase["pre"] if uuid_to_group.get(uuid) == prio_group]
        post = [uuid for uuid in desired_by_rulebase["post"] if uuid_to_group.get(uuid) == prio_group]
        other = [
            row["uuid"] for row in rows
            if row.get("prio_group") == prio_group
            and row.get("uuid")
            and row.get("uuid") not in hierarchy_uuids
            and not row.get("legacy")
        ]
        target = pre + other + post
        for i in range(len(target), 1, -1):
            await opnsense_client.move_rule_before(host, target[i - 2], target[i - 1])


async def reassert_after_managed_change(
    db: AsyncSession,
    host: OpnsenseHost,
    interfaces: set[str] | None = None,
) -> None:
    """Repair hierarchy ordering after another managed feature reloads a host.

    Aliases can cause a broad filter reconfigure, so ``interfaces=None``
    discovers every inherited hierarchy interface. Rule and NAT deployers
    pass the affected interface to avoid unnecessary API calls.
    """
    if host.policy_hierarchy_group_id is None:
        return
    if interfaces is None:
        all_groups = await _load_all_groups(db)
        all_rules = await _load_all_rules(db)
        chain_ids = {group.id for group in ancestor_chain(host, all_groups)}
        interfaces = {
            rule.interface for rule in all_rules
            if rule.hierarchy_group_id in chain_ids and rule.enabled
        }
    for interface in sorted(interfaces):
        await reassert_ordering(db, host, interface)


async def reassert_group_ordering(db: AsyncSession, group: PolicyHierarchyGroup) -> list[str]:
    """Explicit "Reassert ordering" action for a group. Reasserts every host in the group's own subtree,
    for every interface any rule in that host's ancestor chain uses.
    Returns a list of human-readable per-host/interface error strings
    (empty means every reassert succeeded)."""
    all_groups = await _load_all_groups(db)
    all_rules = await _load_all_rules(db)
    hosts = {}
    for g in descendants(group.id, all_groups):
        for h in g.hosts:
            hosts[h.id] = h
    errors: list[str] = []
    for host in sorted(hosts.values(), key=lambda h: (h.name, str(h.id))):
        if not host.enabled:
            continue
        chain_ids = {g.id for g in ancestor_chain(host, all_groups)}
        interfaces = sorted({r.interface for r in all_rules if r.hierarchy_group_id in chain_ids})
        for interface in interfaces:
            try:
                await reassert_ordering(db, host, interface)
            except OpnsenseAPIError as exc:
                errors.append(f"{host.name} ({interface}): {exc}")
    await db.commit()
    return errors


async def _deploy_to_host(db: AsyncSession, rule: PolicyHierarchyRule, host: OpnsenseHost) -> PolicyHierarchyRuleDeployment:
    payload = _build_payload(rule)
    existing = await db.scalar(
        select(PolicyHierarchyRuleDeployment).where(
            PolicyHierarchyRuleDeployment.policy_hierarchy_id == rule.id,
            PolicyHierarchyRuleDeployment.host_id == host.id,
        )
    )

    if existing and existing.status in ("success", "reverted") and existing.last_payload == payload:
        existing.status = "success"
        existing.message = None
        existing.revert_deadline = None
        existing.deployed_at = _now()
        return existing

    if existing and existing.status in ("pending", "revert_failed"):
        return existing

    prior_uuid = existing.opnsense_policy_hierarchy_uuid if existing else None
    prior_live_payload = existing.last_payload if existing else None

    async def write(uuid_):
        if uuid_:
            await opnsense_client.set_rule(host, uuid_, payload)
            return uuid_
        result = await opnsense_client.add_rule(host, payload)
        new_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not new_uuid:
            raise OpnsenseAmbiguousError("Firewall hat keine Regel-UUID zurückgegeben; Stand manuell prüfen")
        return new_uuid

    deployment = await push_to_host(
        db, existing=existing, uuid_attr="opnsense_policy_hierarchy_uuid",
        make=lambda **fields: PolicyHierarchyRuleDeployment(policy_hierarchy_id=rule.id, host_id=host.id, **fields),
        prior_uuid=prior_uuid, prior_live_payload=prior_live_payload, payload=payload,
        write=write, apply=lambda: opnsense_client.apply(host),
    )

    if deployment.status != "pending":
        return deployment

    try:
        await reassert_ordering(db, host, rule.interface)
    except OpnsenseAPIError as exc:
        # Live but not provably in the right position — this must not read
        # as a merely-unconfirmed pending change (see module docstring), yet
        # it stays recoverable: the deadline remains armed.
        deployment.status = "error"
        deployment.message = f"Regel wurde übernommen, Positionierung ('quick') aber nicht bestätigt: {exc}"
        deployment.deployed_at = _now()
    return deployment


async def deploy_hierarchy_rule(db: AsyncSession, rule: PolicyHierarchyRule, hosts: list[OpnsenseHost]) -> list[PolicyHierarchyRuleDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    await ensure_no_maintenance(db, hosts)
    deployments = [await _deploy_to_host(db, rule, host) for host in hosts]
    await db.commit()
    return deployments


async def confirm_hierarchy_rule(db: AsyncSession, rule: PolicyHierarchyRule) -> list[PolicyHierarchyRuleDeployment]:
    result = await db.execute(
        select(PolicyHierarchyRuleDeployment).where(
            PolicyHierarchyRuleDeployment.policy_hierarchy_id == rule.id,
            PolicyHierarchyRuleDeployment.status == "pending",
            PolicyHierarchyRuleDeployment.revert_deadline > _now(),
        )
    )
    deployments = list(result.scalars().all())
    for deployment in deployments:
        deployment.status = "success"
        deployment.revert_deadline = None
    await db.commit()
    return deployments


async def sweep_expired_hierarchy_deployments(db: AsyncSession) -> list[PolicyHierarchyRuleDeployment]:
    """Called periodically from main.py's background task — see
    rule_deploy_service.sweep_expired_deployments, which this mirrors."""
    from app.services.policy_service import policy_lock
    await policy_lock(db)
    result = await db.execute(
        select(PolicyHierarchyRuleDeployment).where(
            PolicyHierarchyRuleDeployment.status.in_(("pending", "error")),
            PolicyHierarchyRuleDeployment.revert_deadline <= _now(),
        )
    )
    expired = list(result.scalars().all())
    touched: list[PolicyHierarchyRuleDeployment] = []

    for deployment in expired:
        await revert_hierarchy_deployment(db, deployment)
        touched.append(deployment)

    await db.commit()
    return touched


async def undeploy_hierarchy_rule(db: AsyncSession, rule: PolicyHierarchyRule) -> None:
    failures = []
    result = await db.execute(select(PolicyHierarchyRuleDeployment).where(PolicyHierarchyRuleDeployment.policy_hierarchy_id == rule.id))
    for deployment in result.scalars().all():
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host and deployment.opnsense_policy_hierarchy_uuid:
            try:
                await opnsense_client.del_rule(host, deployment.opnsense_policy_hierarchy_uuid)
                await opnsense_client.apply(host)
            except OpnsenseAPIError as exc:
                deployment.status = "error"
                deployment.message = str(exc)
                failures.append(host.name)
                continue
        await db.delete(deployment)
    await db.flush()
    if failures:
        await db.commit()
        raise OpnsenseAPIError("Regel konnte nicht von allen Hosts entfernt werden: " + ", ".join(failures))


@tracked_rollback("policy_hierarchy")
async def revert_hierarchy_deployment(db: AsyncSession, deployment: PolicyHierarchyRuleDeployment) -> bool:
    """Retryable rollback. Keep the original payload on failures. Restoring
    a previous payload (or deleting the rule) does not re-run positioning —
    moveRuleBefore only affects ordering, and OPNsense doesn't move rules
    around a firewall's rule list on its own as a side effect of
    setRule/delRule, so whatever order was last established stays valid
    for the rules that remain."""
    host = await db.get(OpnsenseHost, deployment.host_id)
    if host is None:
        await db.delete(deployment)
        return True
    try:
        if deployment.previous_payload is None:
            if deployment.opnsense_policy_hierarchy_uuid:
                await opnsense_client.del_rule(host, deployment.opnsense_policy_hierarchy_uuid)
                await opnsense_client.apply(host)
            await db.delete(deployment)
        else:
            await opnsense_client.set_rule(host, deployment.opnsense_policy_hierarchy_uuid, deployment.previous_payload)
            await opnsense_client.apply(host)
            deployment.last_payload = deployment.previous_payload
            deployment.status = "reverted"
            deployment.message = None
            deployment.revert_deadline = None
        return True
    except OpnsenseAPIError as exc:
        deployment.status = "revert_failed"
        deployment.message = str(exc)
        deployment.revert_deadline = None
        return False
