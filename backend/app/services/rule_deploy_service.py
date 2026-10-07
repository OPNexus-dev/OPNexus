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
from app.services import opnsense_client
from app.services.opnsense_client import OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_payload(rule: FirewallRule) -> dict:
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


async def _deploy_to_host(db: AsyncSession, rule: FirewallRule, host: OpnsenseHost) -> RuleDeployment:
    payload = _build_payload(rule)
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
    new_uuid = prior_uuid
    try:
        if prior_uuid:
            await opnsense_client.set_rule(host, prior_uuid, payload)
        else:
            result = await opnsense_client.add_rule(host, payload)
            new_uuid = result.get("uuid") if isinstance(result, dict) else None
            if not new_uuid:
                raise OpnsenseAPIError("Firewall hat keine Regel-UUID zurückgegeben; Stand manuell prüfen")
        await opnsense_client.apply(host)
    except OpnsenseAPIError as exc:
        if existing:
            existing.status = "error"
            existing.opnsense_rule_uuid = new_uuid
            existing.message = str(exc)
            existing.revert_deadline = None
            existing.deployed_at = _now()
            return existing
        deployment = RuleDeployment(
            rule_id=rule.id, host_id=host.id, status="error", message=str(exc),
            opnsense_rule_uuid=new_uuid, deployed_at=_now(),
        )
        db.add(deployment)
        return deployment

    deadline = _now() + timedelta(seconds=settings.deploy_confirm_window_seconds)
    if existing:
        existing.status = "pending"
        existing.message = None
        existing.opnsense_rule_uuid = new_uuid
        existing.deployed_at = _now()
        existing.revert_deadline = deadline
        existing.previous_payload = prior_live_payload
        existing.last_payload = payload
        return existing

    deployment = RuleDeployment(
        rule_id=rule.id, host_id=host.id, status="pending", message=None,
        opnsense_rule_uuid=new_uuid, deployed_at=_now(), revert_deadline=deadline,
        previous_payload=None, last_payload=payload,
    )
    db.add(deployment)
    return deployment


async def deploy_rule(db: AsyncSession, rule: FirewallRule, hosts: list[OpnsenseHost]) -> list[RuleDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    from app.services.policy_hierarchy_deploy_service import reassert_after_managed_change
    await ensure_no_maintenance(db, hosts)
    deployments = [await _deploy_to_host(db, rule, host) for host in hosts]
    await db.flush()
    for host, deployment in zip(hosts, deployments):
        if deployment.status != "pending":
            continue
        try:
            await reassert_after_managed_change(db, host, {rule.interface})
        except OpnsenseAPIError as exc:
            deployment.status = "error"
            deployment.message = f"Regel wurde übernommen, Hierarchie-Reihenfolge aber nicht bestätigt: {exc}"
            deployment.revert_deadline = None
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
        select(RuleDeployment).where(RuleDeployment.status == "pending", RuleDeployment.revert_deadline <= _now())
    )
    expired = list(result.scalars().all())
    touched: list[RuleDeployment] = []

    for deployment in expired:
        await revert_deployment(db, deployment)
        touched.append(deployment)

    await db.commit()
    return touched


async def undeploy_rule(db: AsyncSession, rule: FirewallRule) -> None:
    failures = []
    result = await db.execute(select(RuleDeployment).where(RuleDeployment.rule_id == rule.id))
    for deployment in result.scalars().all():
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host and deployment.opnsense_rule_uuid:
            try:
                await opnsense_client.del_rule(host, deployment.opnsense_rule_uuid)
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
