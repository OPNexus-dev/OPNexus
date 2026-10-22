"""Independent Outbound NAT deploy / confirm / rollback state machine."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.host import OpnsenseHost
from app.models.outbound_nat import OutboundNatRule
from app.models.outbound_nat_deployment import OutboundNatDeployment
from app.services.outbound_nat_mode import require_manual_mode
from app.services.audit_service import tracked_rollback
from app.services import opnsense_client, variables_service
from app.services.deploy_flow import push_to_host
from app.services.opnsense_client import OpnsenseAmbiguousError, OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_payload(rule: OutboundNatRule, host: OpnsenseHost | None = None) -> dict:
    payload = _base_payload(rule)
    return variables_service.resolve(payload, host) if host is not None else payload


def _base_payload(rule: OutboundNatRule) -> dict:
    return {
        "description": rule.description,
        "interface": rule.interface,
        "source_net": rule.source_net,
        "destination_net": rule.destination_net,
        "source_not": "1" if rule.source_not else "0",
        "destination_not": "1" if rule.destination_not else "0",
        "enabled": "1" if rule.enabled else "0",
        "ipprotocol": rule.ipprotocol,
        "protocol": rule.protocol,
        "source_port": rule.source_port or "",
        "destination_port": rule.destination_port or "",
        "target": rule.target,
        "target_port": rule.target_port or "",
    }


async def _deploy_to_host(db: AsyncSession, rule: OutboundNatRule, host: OpnsenseHost) -> OutboundNatDeployment:
    await require_manual_mode(host)
    payload = _build_payload(rule, host)
    existing = await db.scalar(
        select(OutboundNatDeployment).where(
            OutboundNatDeployment.outbound_nat_id == rule.id, OutboundNatDeployment.host_id == host.id
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

    prior_uuid = existing.opnsense_outbound_nat_uuid if existing else None
    prior_live_payload = existing.last_payload if existing else None

    async def write(uuid_):
        if uuid_:
            await opnsense_client.set_outbound_nat_rule(host, uuid_, payload)
            return uuid_
        result = await opnsense_client.add_outbound_nat_rule(host, payload)
        new_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not new_uuid:
            raise OpnsenseAmbiguousError("Firewall hat keine Regel-UUID zurückgegeben; Stand manuell prüfen")
        return new_uuid

    deployment = await push_to_host(
        db, existing=existing, uuid_attr="opnsense_outbound_nat_uuid",
        make=lambda **fields: OutboundNatDeployment(outbound_nat_id=rule.id, host_id=host.id, **fields),
        prior_uuid=prior_uuid, prior_live_payload=prior_live_payload, payload=payload,
        write=write, apply=lambda: opnsense_client.apply_outbound_nat(host),
    )
    return deployment


async def deploy_outbound_nat(db: AsyncSession, rule: OutboundNatRule, hosts: list[OpnsenseHost]) -> list[OutboundNatDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    from app.services.policy_hierarchy_deploy_service import reassert_after_managed_change
    await ensure_no_maintenance(db, hosts)
    variables_service.ensure_resolvable(_build_payload, rule, hosts)  # before anything is written
    deployments = []
    try:
        for host in hosts:
            deployments.append(await _deploy_to_host(db, rule, host))
        await db.flush()
        for host, deployment in zip(hosts, deployments):
            if deployment.status != "pending":
                continue
            try:
                await reassert_after_managed_change(db, host, {rule.interface})
            except OpnsenseAPIError as exc:
                deployment.status = "error"
                deployment.message = f"Outbound NAT wurde übernommen, Hierarchie-Reihenfolge aber nicht bestätigt: {exc}"
                # The change is live: keep the revert deadline so an unconfirmed result is still undone.
    except Exception:
        # A mode can change after fleet preflight. Preserve already applied
        # hosts and their rollback deadlines even if a later host now blocks.
        await db.commit()
        raise
    await db.commit()
    return deployments


async def confirm_outbound_nat(db: AsyncSession, rule: OutboundNatRule) -> list[OutboundNatDeployment]:
    result = await db.execute(
        select(OutboundNatDeployment).where(
            OutboundNatDeployment.outbound_nat_id == rule.id,
            OutboundNatDeployment.status == "pending",
            OutboundNatDeployment.revert_deadline > _now(),
        )
    )
    deployments = list(result.scalars().all())
    for deployment in deployments:
        deployment.status = "success"
        deployment.revert_deadline = None
    await db.commit()
    return deployments


async def sweep_expired_outbound_nat_deployments(db: AsyncSession) -> list[OutboundNatDeployment]:
    from app.services.policy_service import policy_lock
    await policy_lock(db)
    result = await db.execute(
        select(OutboundNatDeployment).where(
            OutboundNatDeployment.status.in_(("pending", "error")), OutboundNatDeployment.revert_deadline <= _now()
        )
    )
    expired = list(result.scalars().all())
    touched: list[OutboundNatDeployment] = []

    for deployment in expired:
        await revert_deployment(db, deployment)
        touched.append(deployment)

    await db.commit()
    return touched


async def undeploy_outbound_nat(db: AsyncSession, rule: OutboundNatRule) -> None:
    # Deleting writes to the firewalls: not while a rule cleanup (1.31) owns one of them (or its cluster).
    from app.services.host_maintenance_service import ensure_no_open_cleanup_on
    await ensure_no_open_cleanup_on(db, OutboundNatDeployment, OutboundNatDeployment.outbound_nat_id == rule.id)
    failures = []
    result = await db.execute(select(OutboundNatDeployment).where(OutboundNatDeployment.outbound_nat_id == rule.id))
    for deployment in result.scalars().all():
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host and deployment.opnsense_outbound_nat_uuid:
            try:
                await opnsense_client.del_outbound_nat_rule(host, deployment.opnsense_outbound_nat_uuid)
                await opnsense_client.apply_outbound_nat(host)
            except OpnsenseAPIError as exc:
                deployment.status = "error"
                deployment.message = str(exc)
                failures.append(f"{host.name} ({str(exc)[:200]})")
                continue
        await db.delete(deployment)
    await db.flush()
    if failures:
        await db.commit()
        raise OpnsenseAPIError("Outbound NAT-Regel konnte nicht von allen Hosts entfernt werden: " + ", ".join(failures))


@tracked_rollback("outbound_nat")
async def revert_deployment(db: AsyncSession, deployment: OutboundNatDeployment) -> bool:
    host = await db.get(OpnsenseHost, deployment.host_id)
    if host is None:
        await db.delete(deployment)
        return True
    try:
        if deployment.previous_payload is None:
            if deployment.opnsense_outbound_nat_uuid:
                await opnsense_client.del_outbound_nat_rule(host, deployment.opnsense_outbound_nat_uuid)
                await opnsense_client.apply_outbound_nat(host)
            await db.delete(deployment)
        else:
            await opnsense_client.set_outbound_nat_rule(host, deployment.opnsense_outbound_nat_uuid, deployment.previous_payload)
            await opnsense_client.apply_outbound_nat(host)
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
