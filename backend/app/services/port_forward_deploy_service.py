"""Pushes a centrally-authored Port Forward rule to its target hosts —
structurally a full mirror of rule_deploy_service.py's pending/confirm/
sweep/rollback state machine (kept as a fully separate module so a bug
here can't affect the already-relied-upon rule/alias deploy paths), with
two genuinely different details verified against OPNsense's DNat model:

1. The wire field is "disabled", not "enabled" — this module stores/reads
   PortForwardRule.enabled like every other entity here, but flips polarity
   only in _build_payload when talking to OPNsense.
2. Source/destination are nested objects on the wire ({"source": {"network",
   "port", "not"}, "destination": {...}}), not flat source_net/source_port
   fields like FirewallRule/FirewallAlias.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.host import OpnsenseHost
from app.models.port_forward import PortForwardRule
from app.models.port_forward_deployment import PortForwardDeployment
from app.services.audit_service import tracked_rollback
from app.services import opnsense_client, variables_service
from app.services.deploy_flow import push_to_host
from app.services.opnsense_client import OpnsenseAmbiguousError, OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_payload(rule: PortForwardRule, host: OpnsenseHost | None = None) -> dict:
    payload = _base_payload(rule)
    return variables_service.resolve(payload, host) if host is not None else payload


def _base_payload(rule: PortForwardRule) -> dict:
    return {
        "disabled": "0" if rule.enabled else "1",
        "interface": rule.interface,
        "ipprotocol": rule.ipprotocol,
        "protocol": rule.protocol,
        "source": {"network": rule.source_net, "port": rule.source_port or "", "not": "0"},
        "destination": {"network": rule.destination_net, "port": rule.destination_port or "", "not": "0"},
        "target": rule.target,
        "local-port": rule.local_port or "",
        "natreflection": rule.natreflection or "",
        "descr": rule.description,
        "pass": "",  # Never create an associated filter rule implicitly.
    }


async def _deploy_to_host(db: AsyncSession, rule: PortForwardRule, host: OpnsenseHost) -> PortForwardDeployment:
    payload = _build_payload(rule, host)
    existing = await db.scalar(
        select(PortForwardDeployment).where(
            PortForwardDeployment.port_forward_id == rule.id, PortForwardDeployment.host_id == host.id
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

    prior_uuid = existing.opnsense_port_forward_uuid if existing else None
    prior_live_payload = existing.last_payload if existing else None

    async def write(uuid_):
        if uuid_:
            await opnsense_client.set_port_forward_rule(host, uuid_, payload)
            return uuid_
        result = await opnsense_client.add_port_forward_rule(host, payload)
        new_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not new_uuid:
            raise OpnsenseAmbiguousError("Firewall hat keine Regel-UUID zurückgegeben; Stand manuell prüfen")
        return new_uuid

    deployment = await push_to_host(
        db, existing=existing, uuid_attr="opnsense_port_forward_uuid",
        make=lambda **fields: PortForwardDeployment(port_forward_id=rule.id, host_id=host.id, **fields),
        prior_uuid=prior_uuid, prior_live_payload=prior_live_payload, payload=payload,
        write=write, apply=lambda: opnsense_client.apply_port_forward(host),
    )
    return deployment


async def deploy_port_forward(db: AsyncSession, rule: PortForwardRule, hosts: list[OpnsenseHost]) -> list[PortForwardDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    from app.services.policy_hierarchy_deploy_service import reassert_after_managed_change
    await ensure_no_maintenance(db, hosts)
    variables_service.ensure_resolvable(_build_payload, rule, hosts)  # before anything is written
    deployments = [await _deploy_to_host(db, rule, host) for host in hosts]
    await db.flush()
    for host, deployment in zip(hosts, deployments):
        if deployment.status != "pending":
            continue
        try:
            await reassert_after_managed_change(db, host, {rule.interface})
        except OpnsenseAPIError as exc:
            deployment.status = "error"
            deployment.message = f"Port Forward wurde übernommen, Hierarchie-Reihenfolge aber nicht bestätigt: {exc}"
            # The change is live: keep the revert deadline so an unconfirmed result is still undone.
    await db.commit()
    return deployments


async def confirm_port_forward(db: AsyncSession, rule: PortForwardRule) -> list[PortForwardDeployment]:
    result = await db.execute(
        select(PortForwardDeployment).where(
            PortForwardDeployment.port_forward_id == rule.id,
            PortForwardDeployment.status == "pending",
            PortForwardDeployment.revert_deadline > _now(),
        )
    )
    deployments = list(result.scalars().all())
    for deployment in deployments:
        deployment.status = "success"
        deployment.revert_deadline = None
    await db.commit()
    return deployments


async def sweep_expired_port_forward_deployments(db: AsyncSession) -> list[PortForwardDeployment]:
    from app.services.policy_service import policy_lock
    await policy_lock(db)
    result = await db.execute(
        select(PortForwardDeployment).where(
            PortForwardDeployment.status.in_(("pending", "error")), PortForwardDeployment.revert_deadline <= _now()
        )
    )
    expired = list(result.scalars().all())
    touched: list[PortForwardDeployment] = []

    for deployment in expired:
        await revert_deployment(db, deployment)
        touched.append(deployment)

    await db.commit()
    return touched


async def undeploy_port_forward(db: AsyncSession, rule: PortForwardRule) -> None:
    failures = []
    result = await db.execute(select(PortForwardDeployment).where(PortForwardDeployment.port_forward_id == rule.id))
    for deployment in result.scalars().all():
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host and deployment.opnsense_port_forward_uuid:
            try:
                await opnsense_client.del_port_forward_rule(host, deployment.opnsense_port_forward_uuid)
                await opnsense_client.apply_port_forward(host)
            except OpnsenseAPIError as exc:
                deployment.status = "error"
                deployment.message = str(exc)
                failures.append(f"{host.name} ({str(exc)[:200]})")
                continue
        await db.delete(deployment)
    await db.flush()
    if failures:
        await db.commit()
        raise OpnsenseAPIError("Port-Forward-Regel konnte nicht von allen Hosts entfernt werden: " + ", ".join(failures))


@tracked_rollback("port_forward")
async def revert_deployment(db: AsyncSession, deployment: PortForwardDeployment) -> bool:
    host = await db.get(OpnsenseHost, deployment.host_id)
    if host is None:
        await db.delete(deployment)
        return True
    try:
        if deployment.previous_payload is None:
            if deployment.opnsense_port_forward_uuid:
                await opnsense_client.del_port_forward_rule(host, deployment.opnsense_port_forward_uuid)
                await opnsense_client.apply_port_forward(host)
            await db.delete(deployment)
        else:
            await opnsense_client.set_port_forward_rule(host, deployment.opnsense_port_forward_uuid, deployment.previous_payload)
            await opnsense_client.apply_port_forward(host)
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
