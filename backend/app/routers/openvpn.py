import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup, OpenVpnInstanceRevision
from app.models.openvpn_instance import OpenVpnInstance
from app.models.openvpn_instance_deployment import OpenVpnInstanceDeployment
from app.schemas.openvpn import (
    OpenVpnDeployApproval, OpenVpnDeploymentResult, OpenVpnInstanceCreate, OpenVpnInstanceEdit, OpenVpnInstanceRead,
)
from app.services import openvpn_key_store
from app.services.openvpn_service import effective_hosts, preview, revision, snapshot, workflow, OPENVPN_FIELDS
from app.services.openvpn_deploy_service import deploy_instance, undeploy_instance
from app.services.opnsense_client import OpnsenseAPIError
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/openvpn/instances", tags=["openvpn"])


def _load_options():
    return [
        selectinload(OpenVpnInstance.targets),
        selectinload(OpenVpnInstance.groups).selectinload(DeviceGroup.hosts),
        selectinload(OpenVpnInstance.deployments).selectinload(OpenVpnInstanceDeployment.host),
    ]


async def _load_instance(db: AsyncSession, instance_id: uuid.UUID) -> OpenVpnInstance:
    # Same advisory lock as rules/aliases/NAT/IPsec — serializes concurrent
    # deploy-affecting changes across processes. No ensure_editable() gate
    # here either: this entity has no pending/revert_failed state to ever
    # block editing on (see openvpn_deploy_service.py's module docstring).
    await policy_lock(db)
    instance = await db.get(OpenVpnInstance, instance_id, options=_load_options())
    if not instance:
        raise api_error(404, ErrorCode.INSTANZ_NICHT_GEFUNDEN)
    return instance


def _to_read(instance: OpenVpnInstance, deployments: list[OpenVpnInstanceDeployment]) -> OpenVpnInstanceRead:
    hosts_by_id = {host.id: host for host in effective_hosts(instance)}
    hosts_by_id.update({d.host_id: d.host for d in deployments if d.host is not None})
    state, counts = workflow(instance, deployments)
    return OpenVpnInstanceRead(
        workflow_state=state,
        workflow_counts=counts,
        id=instance.id,
        description=instance.description,
        role=instance.role,
        proto=instance.proto,
        port=instance.port,
        local=instance.local,
        remote=instance.remote,
        server_network=instance.server_network,
        cert_refs=instance.cert_refs,
        ca_refs=instance.ca_refs,
        tls_key_enabled=instance.tls_key_enabled,
        tls_key_mode=instance.tls_key_mode,
        tls_key_material_set=instance.tls_key_material_set,
        enabled=instance.enabled,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
        target_host_ids=[host.id for host in instance.targets],
        target_group_ids=[g.id for g in instance.groups],
        effective_host_ids=[h.id for h in effective_hosts(instance)],
        deployments=[
            OpenVpnDeploymentResult(
                host_id=d.host_id,
                host_name=hosts_by_id[d.host_id].name if d.host_id in hosts_by_id else "?",
                status=d.status,
                message=d.message,
            )
            for d in deployments
        ],
    )


async def resolve_targets(db, payload):
    hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.id.in_(payload.target_host_ids)))).all())
    groups = list((await db.scalars(select(DeviceGroup).where(DeviceGroup.id.in_(payload.target_group_ids)).options(selectinload(DeviceGroup.hosts)))).all())
    if len(hosts) != len(set(payload.target_host_ids)) or len(groups) != len(set(payload.target_group_ids)):
        raise api_error(400, ErrorCode.EIN_ODER_MEHRERE_ZIELE_WURDEN_NICHT_GEFUNDEN)
    return hosts, groups


@router.get("", response_model=list[OpenVpnInstanceRead])
async def list_instances(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OpenVpnInstance).options(*_load_options()))
    instances = result.scalars().unique().all()
    return [_to_read(instance, instance.deployments) for instance in instances]


@router.post("", response_model=OpenVpnInstanceRead, status_code=201)
async def create_instance(payload: OpenVpnInstanceCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    hosts, groups = await resolve_targets(db, payload)

    instance = OpenVpnInstance(
        **{field: getattr(payload, field) for field in OPENVPN_FIELDS},
        targets=hosts,
        groups=groups,
    )
    db.add(instance)
    await db.flush()
    if payload.tls_key_material:
        openvpn_key_store.save_secret(instance.id, payload.tls_key_material)
        instance.tls_key_material_set = True
    revision(db, instance, "Instanz angelegt")
    await db.commit()
    return _to_read(instance, [])


@router.put("/{instance_id}", response_model=OpenVpnInstanceRead)
async def edit_instance(instance_id: uuid.UUID, payload: OpenVpnInstanceEdit, db: AsyncSession = Depends(get_db)):
    instance = await _load_instance(db, instance_id)
    if instance.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.INSTANZ_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_DB923B59)
    hosts, groups = await resolve_targets(db, payload)
    if not await db.scalar(select(OpenVpnInstanceRevision.id).where(OpenVpnInstanceRevision.instance_id == instance.id).limit(1)):
        revision(db, instance, "Vor erster Bearbeitung")
    for field in OPENVPN_FIELDS:
        setattr(instance, field, getattr(payload, field))
    instance.targets, instance.groups = hosts, groups
    if payload.tls_key_material is not None:
        openvpn_key_store.save_secret(instance.id, payload.tls_key_material)
        instance.tls_key_material_set = True
    from datetime import datetime, timezone
    instance.updated_at = datetime.now(timezone.utc)
    revision(db, instance, "Entwurf gespeichert")
    await db.commit()
    return _to_read(instance, instance.deployments)


@router.get("/{instance_id}/preview")
async def preview_instance(instance_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return preview(await _load_instance(db, instance_id))


@router.get("/{instance_id}/revisions")
async def list_revisions(instance_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _load_instance(db, instance_id)
    rows = (await db.scalars(
        select(OpenVpnInstanceRevision).where(OpenVpnInstanceRevision.instance_id == instance_id)
        .order_by(OpenVpnInstanceRevision.created_at.desc(), OpenVpnInstanceRevision.id)
    )).all()
    return [{"id": r.id, "created_at": r.created_at, "note": r.note, "snapshot": r.snapshot} for r in rows]


@router.post("/{instance_id}/deploy", response_model=OpenVpnInstanceRead)
async def deploy(instance_id: uuid.UUID, approval: OpenVpnDeployApproval, db: AsyncSession = Depends(get_db)):
    instance = await _load_instance(db, instance_id)
    plan = preview(instance)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    if instance.tls_key_enabled and not instance.tls_key_material_set:
        raise api_error(400, ErrorCode.TLS_HARDENING_IST_AKTIVIERT_ABER_ES_WURDE_NOCH_F53C81DD)
    hosts = [h for h in effective_hosts(instance) if h.enabled]
    if not hosts:
        raise api_error(400, ErrorCode.INSTANZ_HAT_KEINE_ZIEL_HOSTS)
    await deploy_instance(db, instance, hosts)
    await db.refresh(instance, attribute_names=["deployments"])
    for d in instance.deployments:
        await db.refresh(d, attribute_names=["host"])
    return _to_read(instance, instance.deployments)


@router.delete("/{instance_id}", status_code=204)
async def delete_instance(instance_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    instance = await _load_instance(db, instance_id)
    try:
        await undeploy_instance(db, instance)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc
    await db.refresh(instance, attribute_names=["deployments"])
    openvpn_key_store.delete_secret(instance.id)
    await db.delete(instance)
    await db.commit()


@router.post("/{instance_id}/deployments/{host_id}/remove", status_code=204)
async def remove_previous_target(instance_id: uuid.UUID, host_id: uuid.UUID, approval: OpenVpnDeployApproval, db: AsyncSession = Depends(get_db)):
    instance = await _load_instance(db, instance_id)
    plan = preview(instance)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ZIELE_ODER_DEPLOYMENT_WURDEN_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_remove"]:
        raise api_error(409, ErrorCode.NUR_FRUEHERE_AKTIVE_ZIELE_KOENNEN_BEREINIGT_WERDEN)
    deployment = next(d for d in instance.deployments if d.host_id == host_id)
    from app.services import opnsense_client
    try:
        if deployment.opnsense_instance_uuid:
            await opnsense_client.del_openvpn_instance(deployment.host, deployment.opnsense_instance_uuid)
        if deployment.opnsense_statickey_uuid:
            await opnsense_client.del_openvpn_static_key(deployment.host, deployment.opnsense_statickey_uuid)
        await opnsense_client.reconfigure_openvpn(deployment.host)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        await db.commit()
        raise api_error(502, ErrorCode.ENTFERNEN_FEHLGESCHLAGEN_DIE_ZUORDNUNG_BLEIBT_FUER_EINEN_NEUEN_4CF90E4C) from exc
    await db.delete(deployment)
    await db.commit()
