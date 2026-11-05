"""Pushes a centrally-authored rule to its target hosts via the OPNsense
API, instead of relying on OPNsense's own HA config sync.

Each host is attempted independently and failures are caught per-host so
one unreachable firewall doesn't stop the rule from reaching the others.
The last result per (rule, host) is persisted in RuleDeployment, keyed by
the per-host OPNsense rule UUID so later updates/deletes target the right
copy on each box.

Deploy lockout protection: OPNsense's filter API has no native rollback/
confirm primitive (checked FilterController.php directly — nothing there),
so it's built here. A successful deploy doesn't go straight to "success" —
it goes to "pending" with a revert_deadline. confirm_rule() is the "I still
have access" signal a human gives by clicking Confirm; if that doesn't
happen before the deadline, sweep_expired_deployments() (called from a
background loop in main.py) rolls it back on its own: deletes the rule if
this was its first deploy to that host, or restores the previous payload
if it was a re-deploy.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.host import OpnsenseHost
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from app.services.audit_service import tracked_rollback
from app.services import opnsense_client, variables_service
from app.services.deploy_flow import push_to_host
from app.services.opnsense_client import OpnsenseAmbiguousError, OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_payload(rule: FirewallRule, host: OpnsenseHost | None = None) -> dict:
    payload = _base_payload(rule)
    return variables_service.resolve(payload, host) if host is not None else payload


def _base_payload(rule: FirewallRule) -> dict:
    payload = _wire_payload(rule)
    if rule.placement == "top":
        # Not sent to the firewall (opnsense_client strips it); part of the payload so that a changed placement shows up
        # in the preview, makes the deploy a real change and is remembered in last_payload.
        payload["placement"] = "top"
    return payload


def _wire_payload(rule: FirewallRule) -> dict:
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
        "log": "1" if rule.log else "0",
    }


async def _deploy_to_host(db: AsyncSession, rule: FirewallRule, host: OpnsenseHost) -> RuleDeployment:
    payload = _build_payload(rule, host)
    existing = await db.scalar(
        select(RuleDeployment).where(RuleDeployment.rule_id == rule.id, RuleDeployment.host_id == host.id)
    )

    if existing and existing.status in ("success", "reverted") and existing.last_payload == payload:
        # Nothing actually changed on this host — re-affirm, but no new
        # risk was introduced, so no pending/confirm dance needed.
        existing.status = "success"
        existing.message = None
        existing.revert_deadline = None
        existing.deployed_at = _now()
        return existing

    if existing and existing.status in ("pending", "revert_failed"):
        return existing

    prior_uuid = existing.opnsense_rule_uuid if existing else None
    prior_live_payload = existing.last_payload if existing else None
    written: dict[str, str] = {}

    async def write(uuid_):
        if uuid_:
            await opnsense_client.set_rule(host, uuid_, payload)
            written["uuid"] = uuid_
            return uuid_
        result = await opnsense_client.add_rule(host, payload)
        new_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not new_uuid:
            raise OpnsenseAmbiguousError("Firewall hat keine Regel-UUID zurückgegeben; Stand manuell prüfen")
        written["uuid"] = new_uuid
        return new_uuid

    async def apply():
        # Positioning runs after the UUID is recorded (push_to_host), so a refused move leaves an armed "error" row that
        # the revert sweeper undoes - never an unplaced block rule that looks fine.
        # Only when the position was asked for now (new rule, placement changed, or a retry after an error): a rule that was
        # "top" already stays wherever it has been moved to since (the order view lets people move rules).
        retry = existing is not None and existing.status == "error"
        if payload.get("placement") == "top" and (prior_uuid is None or retry or (prior_live_payload or {}).get("placement") != "top"):
            await place_on_top(host, written["uuid"])
        await opnsense_client.apply(host)

    deployment = await push_to_host(
        db, existing=existing, uuid_attr="opnsense_rule_uuid",
        make=lambda **fields: RuleDeployment(rule_id=rule.id, host_id=host.id, **fields),
        prior_uuid=prior_uuid, prior_live_payload=prior_live_payload, payload=payload,
        write=write, apply=apply,
    )
    return deployment


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


async def place_on_top(host: OpnsenseHost, rule_uuid: str) -> None:
    """Moves a rule in front of the first rule of its priority group (moveRuleBefore; OPNsense has no "to top" call).

    The prio_group is NOT the interface (every single-interface rule of every interface shares one group), which is
    harmless: pf evaluates per interface, so being first in the group means first on its own interface too. Rules that
    are not in the model (legacy, automatic) cannot be an anchor and are skipped. A rule that already is first is left
    alone. The move is read back (a refused moveRuleBefore is an HTTP 500 = ambiguous) and a rule that did not end up
    first raises, so the caller marks the deployment as failed instead of reporting success for a misplaced rule.
    """
    rows = [r for r in ((await opnsense_client.search_rules(host, all_rows=True)) or {}).get("rows") or []
            if isinstance(r, dict) and r.get("uuid")]
    own = next((r for r in rows if str(r["uuid"]).lower() == rule_uuid.lower()), None)
    if own is None:
        raise OpnsenseAPIError("Regel nach dem Anlegen nicht auffindbar, Position nicht gesetzt")

    def group(row) -> str:
        return str(row.get("prio_group") or str(row.get("sort_order") or "").partition(".")[0])

    def order(row) -> float:
        try:
            return float(row.get("sort_order"))
        except (TypeError, ValueError):
            return float("inf")

    movable = sorted((r for r in rows if group(r) == group(own) and not _truthy(r.get("legacy"))
                      and not _truthy(r.get("is_automatic"))), key=order)
    if not movable or str(movable[0]["uuid"]).lower() == rule_uuid.lower():
        return
    await opnsense_client.move_rule_before(host, rule_uuid, str(movable[0]["uuid"]))
    after = (await opnsense_client.search_rules(host, all_rows=True)) or {}
    rows = [r for r in after.get("rows") or [] if isinstance(r, dict) and r.get("uuid")]
    own = next((r for r in rows if str(r["uuid"]).lower() == rule_uuid.lower()), None)
    first = sorted((r for r in rows if own is not None and group(r) == group(own) and not _truthy(r.get("legacy"))
                    and not _truthy(r.get("is_automatic"))), key=order)
    if own is None or not first or str(first[0]["uuid"]).lower() != rule_uuid.lower():
        raise OpnsenseAPIError("Regel konnte nicht an die erste Stelle verschoben werden")


async def deploy_rule(db: AsyncSession, rule: FirewallRule, hosts: list[OpnsenseHost]) -> list[RuleDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    from app.services.policy_hierarchy_deploy_service import reassert_after_managed_change
    hosts = variables_service.applicable_hosts(_build_payload, rule, hosts)  # hosts without the interface role take no part
    await ensure_no_maintenance(db, hosts)
    variables_service.ensure_resolvable(_build_payload, rule, hosts)  # before anything is written
    deployments = [await _deploy_to_host(db, rule, host) for host in hosts]
    await db.flush()
    for host, deployment in zip(hosts, deployments):
        if deployment.status != "pending":
            continue
        try:
            await reassert_after_managed_change(db, host, {_build_payload(rule, host)["interface"]})
        except OpnsenseAPIError as exc:
            deployment.status = "error"
            deployment.message = f"Regel wurde übernommen, Hierarchie-Reihenfolge aber nicht bestätigt: {exc}"
            # The change is live: keep the revert deadline so an unconfirmed result is still undone.
    await db.commit()
    return deployments


async def confirm_rule(db: AsyncSession, rule: FirewallRule) -> list[RuleDeployment]:
    """The human confirming "I still have access" — makes every currently
    pending deployment of this rule permanent."""
    result = await db.execute(
        select(RuleDeployment).where(RuleDeployment.rule_id == rule.id, RuleDeployment.status == "pending", RuleDeployment.revert_deadline > _now())
    )
    deployments = list(result.scalars().all())
    for deployment in deployments:
        deployment.status = "success"
        deployment.revert_deadline = None
    await db.commit()
    return deployments


async def sweep_expired_deployments(db: AsyncSession) -> list[RuleDeployment]:
    """Reverts every deployment whose confirm window passed unconfirmed.
    Called periodically from main.py's background task, and directly by
    tests (after backdating revert_deadline) so this doesn't need a real
    timer in the test suite."""
    from app.services.policy_service import policy_lock
    await policy_lock(db)
    result = await db.execute(
        select(RuleDeployment).where(RuleDeployment.status.in_(("pending", "error")), RuleDeployment.revert_deadline <= _now())
    )
    expired = list(result.scalars().all())
    touched: list[RuleDeployment] = []

    for deployment in expired:
        await revert_deployment(db, deployment)
        touched.append(deployment)

    await db.commit()
    return touched


async def undeploy_rule(db: AsyncSession, rule: FirewallRule) -> None:
    from app.services.host_maintenance_service import ensure_no_open_cleanup
    failures = []
    result = await db.execute(select(RuleDeployment).where(RuleDeployment.rule_id == rule.id))
    deployments = list(result.scalars().all())
    # Deleting writes to the firewalls: not while a rule cleanup (1.31) owns one of them.
    await ensure_no_open_cleanup(db, [h for h in [await db.get(OpnsenseHost, d.host_id) for d in deployments] if h])
    for deployment in deployments:
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host and deployment.opnsense_rule_uuid:
            try:
                await opnsense_client.del_rule(host, deployment.opnsense_rule_uuid)
                await opnsense_client.apply(host)
            except OpnsenseAPIError as exc:
                deployment.status = "error"
                deployment.message = str(exc)
                failures.append(f"{host.name} ({str(exc)[:200]})")
                continue
        await db.delete(deployment)
    await db.flush()
    if failures:
        await db.commit()
        raise OpnsenseAPIError("Regel konnte nicht von allen Hosts entfernt werden: " + ", ".join(failures))


@tracked_rollback("rule")
async def revert_deployment(db: AsyncSession, deployment: RuleDeployment) -> bool:
    """Retryable rollback. Keep the original payload on failures."""
    host = await db.get(OpnsenseHost, deployment.host_id)
    if host is None:
        await db.delete(deployment)
        return True
    try:
        if deployment.previous_payload is None:
            if deployment.opnsense_rule_uuid:
                await opnsense_client.del_rule(host, deployment.opnsense_rule_uuid)
                await opnsense_client.apply(host)
            await db.delete(deployment)
        else:
            await opnsense_client.set_rule(host, deployment.opnsense_rule_uuid, deployment.previous_payload)
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
