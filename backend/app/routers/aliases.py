import uuid

from fastapi import APIRouter, Depends, Body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup, AliasRevision
from app.services.policy_service import effective_hosts, policy_lock, ensure_editable
from app.services.alias_policy_service import ALIAS_FIELDS, snapshot, revision, preview, workflow
from app.models.alias import FirewallAlias
from app.models.alias_deployment import AliasDeployment
from app.schemas.alias import DeploymentResult, AliasCreate, AliasRead, AliasEdit, DeployApproval
from app.services.alias_deploy_service import confirm_alias, deploy_alias, undeploy_alias, revert_alias_deployment
from app.services.opnsense_client import OpnsenseAPIError
from app.services.policy_hierarchy_target_service import hydrate_hierarchy_targets, validate_hierarchy_group

router = APIRouter(prefix="/aliases", tags=["aliases"])


def _load_options():
    return [
        selectinload(FirewallAlias.targets),
        selectinload(FirewallAlias.groups).selectinload(DeviceGroup.hosts),
        selectinload(FirewallAlias.deployments).selectinload(AliasDeployment.host),
    ]


async def _load_alias(db: AsyncSession, alias_id: uuid.UUID) -> FirewallAlias:
    await policy_lock(db)
    alias = await db.get(FirewallAlias, alias_id, options=_load_options())
    if not alias:
        raise api_error(404, ErrorCode.ALIAS_NICHT_GEFUNDEN)
    await hydrate_hierarchy_targets(db, [alias])
    return alias


def _to_read(alias: FirewallAlias, hosts: list[OpnsenseHost], deployments: list[AliasDeployment]) -> AliasRead:
    hosts_by_id = {host.id: host for host in effective_hosts(alias)}
    hosts_by_id.update({d.host_id: d.host for d in deployments if d.host is not None})
    state, counts = workflow(alias, deployments)
    return AliasRead(
        workflow_state=state,
        workflow_counts=counts,
        id=alias.id,
        name=alias.name,
        type=alias.type,
        content=alias.content,
        description=alias.description,
        enabled=alias.enabled,
        created_at=alias.created_at,
        updated_at=alias.updated_at,
        target_host_ids=[host.id for host in alias.targets],
        target_group_ids=[g.id for g in alias.groups],
        policy_hierarchy_group_id=alias.policy_hierarchy_group_id,
        effective_host_ids=[h.id for h in effective_hosts(alias)],
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


@router.get("", response_model=list[AliasRead])
async def list_aliases(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(FirewallAlias).options(*_load_options()))
    aliases = result.scalars().unique().all()
    await hydrate_hierarchy_targets(db, aliases)
    return [_to_read(alias, alias.targets, alias.deployments) for alias in aliases]


async def resolve_targets(db, payload):
    hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.id.in_(payload.target_host_ids)))).all())
    groups = list((await db.scalars(select(DeviceGroup).where(DeviceGroup.id.in_(payload.target_group_ids)).options(selectinload(DeviceGroup.hosts)))).all())
    if len(hosts) != len(set(payload.target_host_ids)) or len(groups) != len(set(payload.target_group_ids)):
        raise api_error(400, ErrorCode.EIN_ODER_MEHRERE_ZIELE_WURDEN_NICHT_GEFUNDEN)
    return hosts, groups


@router.post("", response_model=AliasRead, status_code=201)
async def create_alias(payload: AliasCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    existing = await db.scalar(select(FirewallAlias).where(FirewallAlias.name == payload.name))
    if existing:
        raise api_error(409, ErrorCode.ALIAS_MIT_DIESEM_NAMEN_EXISTIERT_BEREITS)
    hosts, groups = await resolve_targets(db, payload)
    await validate_hierarchy_group(db, payload.policy_hierarchy_group_id)

    alias = FirewallAlias(
        name=payload.name,
        type=payload.type,
        content=payload.content,
        description=payload.description,
        enabled=payload.enabled,
        targets=hosts,
        groups=groups,
        policy_hierarchy_group_id=payload.policy_hierarchy_group_id,
    )
    db.add(alias)
    await db.flush()
    revision(db, alias, "Alias angelegt")
    await db.commit()
    await hydrate_hierarchy_targets(db, [alias])
    return _to_read(alias, hosts, [])


@router.post("/{alias_id}/deploy", response_model=AliasRead)
async def deploy(alias_id: uuid.UUID, approval: DeployApproval | None = Body(default=None), db: AsyncSession = Depends(get_db)):
    alias = await _load_alias(db, alias_id)
    plan = preview(alias)
    if approval and approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    if any(d.status in ("pending", "revert_failed") for d in alias.deployments):
        raise api_error(409, ErrorCode.OFFENES_DEPLOYMENT_ZUERST_BESTAETIGEN_ODER_ROLLBACK_KLAEREN)
    hosts = [h for h in effective_hosts(alias) if h.enabled]
    if not hosts:
        raise api_error(400, ErrorCode.ALIAS_HAT_KEINE_ZIEL_HOSTS)
    await deploy_alias(db, alias, hosts)
    await db.refresh(alias, attribute_names=["deployments"])
    for d in alias.deployments:
        await db.refresh(d, attribute_names=["host"])
    return _to_read(alias, hosts, alias.deployments)


@router.post("/{alias_id}/confirm", response_model=AliasRead)
async def confirm(alias_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    alias = await _load_alias(db, alias_id)
    await confirm_alias(db, alias)
    return _to_read(alias, alias.targets, alias.deployments)


@router.delete("/{alias_id}", status_code=204)
async def delete_alias(alias_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    alias = await _load_alias(db, alias_id)
    if any(d.status in ("pending", "revert_failed") for d in alias.deployments):
        raise api_error(409, ErrorCode.OFFENES_DEPLOYMENT_ZUERST_KLAEREN)
    try:
        await undeploy_alias(db, alias)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc
    await db.refresh(alias, attribute_names=["deployments"])
    await db.delete(alias)
    await db.commit()


@router.put("/{alias_id}", response_model=AliasRead)
async def edit_alias(alias_id: uuid.UUID, payload: AliasEdit, db: AsyncSession = Depends(get_db)):
    alias = await _load_alias(db, alias_id)
    ensure_editable(alias)
    if alias.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.ALIAS_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_5A2C1888)
    duplicate = await db.scalar(select(FirewallAlias).where(FirewallAlias.name == payload.name, FirewallAlias.id != alias_id))
    if duplicate:
        raise api_error(409, ErrorCode.ALIAS_MIT_DIESEM_NAMEN_EXISTIERT_BEREITS)
    hosts, groups = await resolve_targets(db, payload)
    await validate_hierarchy_group(db, payload.policy_hierarchy_group_id)
    if not await db.scalar(select(AliasRevision.id).where(AliasRevision.alias_id == alias.id).limit(1)):
        revision(db, alias, "Vor erster Bearbeitung")
    for field in ALIAS_FIELDS:
        setattr(alias, field, getattr(payload, field))
    alias.targets, alias.groups = hosts, groups
    alias.policy_hierarchy_group_id = payload.policy_hierarchy_group_id
    from datetime import datetime, timezone
    alias.updated_at = datetime.now(timezone.utc)
    revision(db, alias, "Entwurf gespeichert")
    await db.commit()
    await hydrate_hierarchy_targets(db, [alias])
    return _to_read(alias, hosts, alias.deployments)


@router.get("/{alias_id}/preview")
async def preview_alias(alias_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return preview(await _load_alias(db, alias_id))


@router.get("/{alias_id}/revisions")
async def list_revisions(alias_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _load_alias(db, alias_id)
    rows = (await db.scalars(select(AliasRevision).where(AliasRevision.alias_id == alias_id).order_by(AliasRevision.created_at.desc(), AliasRevision.id))).all()
    return [{"id": r.id, "created_at": r.created_at, "note": r.note, "snapshot": r.snapshot} for r in rows]


@router.post("/{alias_id}/deployments/{host_id}/remove", status_code=204)
async def remove_previous_target(alias_id: uuid.UUID, host_id: uuid.UUID, approval: DeployApproval, db: AsyncSession = Depends(get_db)):
    alias = await _load_alias(db, alias_id)
    plan = preview(alias)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ZIELE_ODER_DEPLOYMENT_WURDEN_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_remove"]:
        raise api_error(409, ErrorCode.NUR_FRUEHERE_AKTIVE_ZIELE_OHNE_OFFENEN_ROLLBACK_KOENNEN_06DE0ADB)
    deployment = next(d for d in alias.deployments if d.host_id == host_id)
    from app.services import opnsense_client
    try:
        await opnsense_client.del_alias_item(deployment.host, deployment.opnsense_alias_uuid)
        await opnsense_client.reconfigure_aliases(deployment.host)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        await db.commit()
        raise api_error(502, ErrorCode.ENTFERNEN_FEHLGESCHLAGEN_DIE_ZUORDNUNG_BLEIBT_FUER_EINEN_NEUEN_4CF90E4C) from exc
    await db.delete(deployment)
    await db.commit()


@router.post("/{alias_id}/deployments/{host_id}/retry-rollback", status_code=204)
async def retry_rollback(alias_id: uuid.UUID, host_id: uuid.UUID, approval: DeployApproval, db: AsyncSession = Depends(get_db)):
    alias = await _load_alias(db, alias_id)
    plan = preview(alias)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.DEPLOYMENT_WURDE_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_retry_rollback"]:
        raise api_error(409, ErrorCode.FUER_DIESES_ZIEL_LIEGT_KEIN_WIEDERHOLBARER_ROLLBACK_VOR)
    deployment = next(d for d in alias.deployments if d.host_id == host_id)
    reverted = await revert_alias_deployment(db, deployment)
    await db.commit()
    if not reverted:
        raise api_error(502, ErrorCode.ROLLBACK_ERNEUT_FEHLGESCHLAGEN_PRUEFE_DIE_ERREICHBARKEIT_DER_FIREWALL)
