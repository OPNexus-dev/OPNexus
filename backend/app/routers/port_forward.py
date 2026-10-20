import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup, PortForwardRevision
from app.services.policy_service import effective_hosts, policy_lock, ensure_editable
from app.services.port_forward_policy_service import PORT_FORWARD_FIELDS, snapshot, revision, preview, workflow
from app.models.port_forward import PortForwardRule
from app.models.port_forward_deployment import PortForwardDeployment
from app.schemas.port_forward import DeploymentResult, PortForwardCreate, PortForwardRead, PortForwardEdit, DeployApproval
from app.services.alias_service import check_alias_consistency, describe_alias_contents, ensure_aliases_settled
from app.services.interface_service import check_interface_consistency
from app.services.opnsense_client import OpnsenseAPIError
from app.services.port_forward_deploy_service import confirm_port_forward, deploy_port_forward, undeploy_port_forward, revert_deployment
from app.services.policy_hierarchy_target_service import hydrate_hierarchy_targets, validate_hierarchy_group
from app.routers.adopt_routes import add_adopt_routes

router = APIRouter(prefix="/port-forward", tags=["port-forward"])


def _load_options():
    return [
        selectinload(PortForwardRule.targets),
        selectinload(PortForwardRule.groups).selectinload(DeviceGroup.hosts),
        selectinload(PortForwardRule.deployments).selectinload(PortForwardDeployment.host),
    ]


async def _load_rule(db: AsyncSession, rule_id: uuid.UUID) -> PortForwardRule:
    await policy_lock(db)
    rule = await db.get(PortForwardRule, rule_id, options=_load_options())
    if not rule:
        raise api_error(404, ErrorCode.PORT_FORWARD_REGEL_NICHT_GEFUNDEN)
    await hydrate_hierarchy_targets(db, [rule])
    return rule


def _to_read(rule: PortForwardRule, hosts: list[OpnsenseHost], deployments: list[PortForwardDeployment]) -> PortForwardRead:
    hosts_by_id = {host.id: host for host in effective_hosts(rule)}
    hosts_by_id.update({d.host_id: d.host for d in deployments if d.host is not None})
    state, counts = workflow(rule, deployments)
    return PortForwardRead(
        workflow_state=state,
        workflow_counts=counts,
        id=rule.id,
        description=rule.description,
        interface=rule.interface,
        ipprotocol=rule.ipprotocol,
        protocol=rule.protocol,
        source_net=rule.source_net,
        source_port=rule.source_port,
        destination_net=rule.destination_net,
        destination_port=rule.destination_port,
        target=rule.target,
        local_port=rule.local_port,
        natreflection=rule.natreflection,
        enabled=rule.enabled,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        target_host_ids=[host.id for host in rule.targets],
        target_group_ids=[g.id for g in rule.groups],
        policy_hierarchy_group_id=rule.policy_hierarchy_group_id,
        effective_host_ids=[h.id for h in effective_hosts(rule)],
        deployments=[
            DeploymentResult(
                host_id=d.host_id,
                host_name=hosts_by_id[d.host_id].name if d.host_id in hosts_by_id else "?",
                status=d.status,
                message=d.message,
                revert_deadline=d.revert_deadline,
            )
            for d in deployments
        ],
    )


add_adopt_routes(router, kind="port_forward", model=PortForwardRule, load_options=_load_options, to_read=_to_read, read_model=PortForwardRead)


@router.get("", response_model=list[PortForwardRead])
async def list_port_forward_rules(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PortForwardRule).options(*_load_options()))
    rules = result.scalars().unique().all()
    await hydrate_hierarchy_targets(db, rules)
    return [_to_read(rule, rule.targets, rule.deployments) for rule in rules]


@router.post("", response_model=PortForwardRead, status_code=201)
async def create_port_forward_rule(payload: PortForwardCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    hosts, groups = await resolve_targets(db, payload)
    await validate_hierarchy_group(db, payload.policy_hierarchy_group_id)

    rule = PortForwardRule(
        description=payload.description,
        interface=payload.interface,
        ipprotocol=payload.ipprotocol,
        protocol=payload.protocol,
        source_net=payload.source_net,
        source_port=payload.source_port,
        destination_net=payload.destination_net,
        destination_port=payload.destination_port,
        target=payload.target,
        local_port=payload.local_port,
        natreflection=payload.natreflection,
        enabled=payload.enabled,
        targets=hosts,
        groups=groups,
        policy_hierarchy_group_id=payload.policy_hierarchy_group_id,
    )
    db.add(rule)
    await db.flush()
    revision(db, rule, "Regel angelegt")
    await db.commit()
    await hydrate_hierarchy_targets(db, [rule])
    return _to_read(rule, hosts, [])


@router.post("/{rule_id}/deploy", response_model=PortForwardRead)
async def deploy(rule_id: uuid.UUID, approval: DeployApproval | None = Body(default=None), db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    plan = preview(rule)
    if approval and approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    if any(d.status in ("pending", "revert_failed") for d in rule.deployments):
        raise api_error(409, ErrorCode.OFFENES_DEPLOYMENT_ZUERST_BESTAETIGEN_ODER_ROLLBACK_KLAEREN)
    hosts = [h for h in effective_hosts(rule) if h.enabled]
    if not hosts:
        raise api_error(400, ErrorCode.REGEL_HAT_KEINE_ZIEL_HOSTS)
    if len(hosts) > 1:
        labels = await check_interface_consistency(hosts, rule.interface)
        distinct = set(labels.values())
        if len(distinct) > 1:
            detail = ", ".join(f"{name}={label}" for name, label in sorted(labels.items()))
            raise api_error(409, ErrorCode.INTERFACE_MEANING_DIFFERS, interface=rule.interface, detail=detail)
        for field_label, value in (
            ("Source", rule.source_net),
            ("Destination", rule.destination_net),
            ("Ziel (Target)", rule.target),
        ):
            if not value:
                continue
            contents = await check_alias_consistency(hosts, value)
            if len(set(contents.values())) > 1:
                detail = describe_alias_contents(contents)
                raise api_error(409, ErrorCode.ALIAS_CONTENT_DIFFERS, field_label=field_label, alias=value, detail=detail)
    await ensure_aliases_settled(db, hosts, [rule.source_net, rule.destination_net, rule.target])
    await deploy_port_forward(db, rule, hosts)
    await db.refresh(rule, attribute_names=["deployments"])
    for d in rule.deployments:
        await db.refresh(d, attribute_names=["host"])
    return _to_read(rule, hosts, rule.deployments)


@router.post("/{rule_id}/confirm", response_model=PortForwardRead)
async def confirm(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    await confirm_port_forward(db, rule)
    return _to_read(rule, rule.targets, rule.deployments)


@router.delete("/{rule_id}", status_code=204)
async def delete_port_forward_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    if any(d.status in ("pending", "revert_failed") for d in rule.deployments):
        raise api_error(409, ErrorCode.OFFENES_DEPLOYMENT_ZUERST_KLAEREN)
    try:
        await undeploy_port_forward(db, rule)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc
    await db.refresh(rule, attribute_names=["deployments"])
    await db.delete(rule)
    await db.commit()


async def resolve_targets(db, payload):
    hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.id.in_(payload.target_host_ids)))).all())
    groups = list((await db.scalars(select(DeviceGroup).where(DeviceGroup.id.in_(payload.target_group_ids)).options(selectinload(DeviceGroup.hosts)))).all())
    if len(hosts) != len(set(payload.target_host_ids)) or len(groups) != len(set(payload.target_group_ids)):
        raise api_error(400, ErrorCode.EIN_ODER_MEHRERE_ZIELE_WURDEN_NICHT_GEFUNDEN)
    return hosts, groups


@router.put("/{rule_id}", response_model=PortForwardRead)
async def edit_port_forward_rule(rule_id: uuid.UUID, payload: PortForwardEdit, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    ensure_editable(rule)
    if rule.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.REGEL_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_BF51B6B0)
    hosts, groups = await resolve_targets(db, payload)
    await validate_hierarchy_group(db, payload.policy_hierarchy_group_id)
    if not await db.scalar(select(PortForwardRevision.id).where(PortForwardRevision.port_forward_id == rule.id).limit(1)):
        revision(db, rule, "Vor erster Bearbeitung")
    for field in PORT_FORWARD_FIELDS:
        setattr(rule, field, getattr(payload, field))
    rule.targets, rule.groups = hosts, groups
    rule.policy_hierarchy_group_id = payload.policy_hierarchy_group_id
    rule.updated_at = datetime.now(timezone.utc)
    revision(db, rule, "Entwurf gespeichert")
    await db.commit()
    await hydrate_hierarchy_targets(db, [rule])
    return _to_read(rule, hosts, rule.deployments)


@router.get("/{rule_id}/preview")
async def preview_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return preview(await _load_rule(db, rule_id))


@router.get("/{rule_id}/revisions")
async def list_revisions(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _load_rule(db, rule_id)
    rows = (await db.scalars(select(PortForwardRevision).where(PortForwardRevision.port_forward_id == rule_id).order_by(PortForwardRevision.created_at.desc(), PortForwardRevision.id))).all()
    return [{"id": r.id, "created_at": r.created_at, "note": r.note, "snapshot": r.snapshot} for r in rows]


@router.post("/{rule_id}/deployments/{host_id}/remove", status_code=204)
async def remove_previous_target(rule_id: uuid.UUID, host_id: uuid.UUID, approval: DeployApproval, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    plan = preview(rule)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ZIELE_ODER_DEPLOYMENT_WURDEN_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_remove"]:
        raise api_error(409, ErrorCode.NUR_FRUEHERE_AKTIVE_ZIELE_OHNE_OFFENEN_ROLLBACK_KOENNEN_06DE0ADB)
    deployment = next(d for d in rule.deployments if d.host_id == host_id)
    from app.services import opnsense_client
    try:
        await opnsense_client.del_port_forward_rule(deployment.host, deployment.opnsense_port_forward_uuid)
        await opnsense_client.apply_port_forward(deployment.host)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        await db.commit()
        raise api_error(502, ErrorCode.ENTFERNEN_FEHLGESCHLAGEN_DIE_ZUORDNUNG_BLEIBT_FUER_EINEN_NEUEN_4CF90E4C) from exc
    await db.delete(deployment)
    await db.commit()


@router.post("/{rule_id}/deployments/{host_id}/retry-rollback", status_code=204)
async def retry_rollback(rule_id: uuid.UUID, host_id: uuid.UUID, approval: DeployApproval, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    plan = preview(rule)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.DEPLOYMENT_WURDE_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_retry_rollback"]:
        raise api_error(409, ErrorCode.FUER_DIESES_ZIEL_LIEGT_KEIN_WIEDERHOLBARER_ROLLBACK_VOR)
    deployment = next(d for d in rule.deployments if d.host_id == host_id)
    reverted = await revert_deployment(db, deployment)
    await db.commit()
    if not reverted:
        raise api_error(502, ErrorCode.ROLLBACK_ERNEUT_FEHLGESCHLAGEN_PRUEFE_DIE_ERREICHBARKEIT_DER_FIREWALL)


@router.get("/hosts/{host_id}/live")
async def live_rules(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    from app.services import opnsense_client
    from app.services.nat_live_service import split_rows
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    try:
        return split_rows(await opnsense_client.search_port_forward_rules(host))
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc
