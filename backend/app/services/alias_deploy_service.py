"""Pushes a centrally-authored alias to its target hosts via the OPNsense
API — the alias equivalent of rule_deploy_service.py. Same deploy lockout
protection: a successful push goes to "pending" with a revert_deadline,
not straight to "success", since editing an alias's content changes
enforcement for every rule that references it on that host. See
rule_deploy_service.py for the full rationale; this mirrors it exactly,
kept as a separate module rather than a shared generic implementation so a
bug in alias handling can't affect the already-relied-upon rule deploy
path.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.host import OpnsenseHost
from app.models.alias import FirewallAlias
from app.models.alias_deployment import AliasDeployment
from app.services.audit_service import tracked_rollback
from app.services import opnsense_client
from app.services.deploy_flow import push_to_host
from app.services.opnsense_client import OpnsenseAmbiguousError, OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_payload(alias: FirewallAlias) -> dict:
    return {
        "enabled": "1" if alias.enabled else "0",
        "name": alias.name,
        "type": alias.type,
        "content": alias.content,
        "description": alias.description,
    }


async def _deploy_to_host(db: AsyncSession, alias: FirewallAlias, host: OpnsenseHost) -> AliasDeployment:
    payload = _build_payload(alias)
    existing = await db.scalar(
        select(AliasDeployment).where(AliasDeployment.alias_id == alias.id, AliasDeployment.host_id == host.id)
    )

    if existing and existing.status in ("success", "reverted") and existing.last_payload == payload:
        existing.status = "success"
        existing.message = None
        existing.revert_deadline = None
        existing.deployed_at = _now()
        return existing

    if existing and existing.status in ("pending", "revert_failed"):
        return existing

    prior_uuid = existing.opnsense_alias_uuid if existing else None
    prior_live_payload = existing.last_payload if existing else None

    async def write(uuid_):
        if uuid_:
            await opnsense_client.set_alias_item(host, uuid_, payload)
            return uuid_
        result = await opnsense_client.add_alias_item(host, payload)
        new_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not new_uuid:
            raise OpnsenseAmbiguousError("Firewall hat keine Alias-UUID zurückgegeben; Stand manuell prüfen")
        return new_uuid

    deployment = await push_to_host(
        db, existing=existing, uuid_attr="opnsense_alias_uuid",
        make=lambda **fields: AliasDeployment(alias_id=alias.id, host_id=host.id, **fields),
        prior_uuid=prior_uuid, prior_live_payload=prior_live_payload, payload=payload,
        write=write, apply=lambda: opnsense_client.reconfigure_aliases(host),
    )
    return deployment


async def deploy_alias(db: AsyncSession, alias: FirewallAlias, hosts: list[OpnsenseHost]) -> list[AliasDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    from app.services.policy_hierarchy_deploy_service import reassert_after_managed_change
    await ensure_no_maintenance(db, hosts)
    deployments = [await _deploy_to_host(db, alias, host) for host in hosts]
    await db.flush()
    for host, deployment in zip(hosts, deployments):
        if deployment.status != "pending":
            continue
        try:
            await reassert_after_managed_change(db, host)
        except OpnsenseAPIError as exc:
            deployment.status = "error"
            deployment.message = f"Alias wurde übernommen, Hierarchie-Reihenfolge aber nicht bestätigt: {exc}"
            # The change is live: keep the revert deadline so an unconfirmed result is still undone.
    await db.commit()
    return deployments


async def confirm_alias(db: AsyncSession, alias: FirewallAlias) -> list[AliasDeployment]:
    result = await db.execute(
        select(AliasDeployment).where(AliasDeployment.alias_id == alias.id, AliasDeployment.status == "pending", AliasDeployment.revert_deadline > _now())
    )
    deployments = list(result.scalars().all())
    for deployment in deployments:
        deployment.status = "success"
        deployment.revert_deadline = None
    await db.commit()
    return deployments


async def sweep_expired_alias_deployments(db: AsyncSession) -> list[AliasDeployment]:
    from app.services.policy_service import policy_lock
    await policy_lock(db)
    result = await db.execute(
        select(AliasDeployment).where(AliasDeployment.status.in_(("pending", "error")), AliasDeployment.revert_deadline <= _now())
    )
    expired = list(result.scalars().all())
    touched: list[AliasDeployment] = []

    for deployment in expired:
        await revert_alias_deployment(db, deployment)
        touched.append(deployment)

    await db.commit()
    return touched


async def undeploy_alias(db: AsyncSession, alias: FirewallAlias) -> None:
    failures = []
    result = await db.execute(select(AliasDeployment).where(AliasDeployment.alias_id == alias.id))
    for deployment in result.scalars().all():
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host and deployment.opnsense_alias_uuid:
            try:
                await opnsense_client.del_alias_item(host, deployment.opnsense_alias_uuid)
                await opnsense_client.reconfigure_aliases(host)
            except OpnsenseAPIError as exc:
                deployment.status = "error"
                deployment.message = str(exc)
                failures.append(host.name)
                continue
        await db.delete(deployment)
    await db.flush()
    if failures:
        await db.commit()
        raise OpnsenseAPIError("Alias konnte nicht von allen Hosts entfernt werden: " + ", ".join(failures))


@tracked_rollback("alias")
async def revert_alias_deployment(db: AsyncSession, deployment: AliasDeployment) -> bool:
    host = await db.get(OpnsenseHost, deployment.host_id)
    if host is None:
        await db.delete(deployment)
        return True
    try:
        if deployment.previous_payload is None:
            if deployment.opnsense_alias_uuid:
                await opnsense_client.del_alias_item(host, deployment.opnsense_alias_uuid)
                await opnsense_client.reconfigure_aliases(host)
            await db.delete(deployment)
        else:
            await opnsense_client.set_alias_item(host, deployment.opnsense_alias_uuid, deployment.previous_payload)
            await opnsense_client.reconfigure_aliases(host)
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
