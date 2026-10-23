import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup, IpsecTunnelRevision
from app.models.ipsec_tunnel import IpsecTunnel
from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
from app.schemas.ipsec import IpsecDeployApproval, IpsecDeploymentResult, IpsecTunnelCreate, IpsecTunnelEdit, IpsecTunnelRead
from app.services import ipsec_psk_store
from app.services.ipsec_service import effective_hosts, preview, revision, snapshot, workflow, IPSEC_FIELDS
from app.services.ipsec_deploy_service import deploy_tunnel, undeploy_tunnel
from app.services.opnsense_client import OpnsenseAPIError
from app.services.deploy_lease import resource_lease
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/ipsec/tunnels", tags=["ipsec"])


def _load_options():
    return [
        selectinload(IpsecTunnel.targets),
        selectinload(IpsecTunnel.groups).selectinload(DeviceGroup.hosts),
        selectinload(IpsecTunnel.deployments).selectinload(IpsecTunnelDeployment.host),
    ]


async def _load_tunnel(db: AsyncSession, tunnel_id: uuid.UUID) -> IpsecTunnel:
    # Same advisory lock as rules/aliases/NAT — serializes concurrent
    # deploy-affecting changes across processes. Unlike rules there is no
    # ensure_editable() gate here: this entity has no pending/revert_failed
    # state to ever block editing on (see ipsec_deploy_service.py's module
    # docstring) — editing is always allowed, same scope decision already
    # made for rule DELETE ("already an explicit action, not gated").
    await policy_lock(db)
    tunnel = await db.get(IpsecTunnel, tunnel_id, options=_load_options())
    if not tunnel:
        raise api_error(404, ErrorCode.TUNNEL_NICHT_GEFUNDEN)
    return tunnel


def _to_read(tunnel: IpsecTunnel, deployments: list[IpsecTunnelDeployment]) -> IpsecTunnelRead:
    hosts_by_id = {host.id: host for host in effective_hosts(tunnel)}
    hosts_by_id.update({d.host_id: d.host for d in deployments if d.host is not None})
    state, counts = workflow(tunnel, deployments)
    return IpsecTunnelRead(
        workflow_state=state,
        workflow_counts=counts,
        id=tunnel.id,
        description=tunnel.description,
        local_address=tunnel.local_address,
        remote_address=tunnel.remote_address,
        ike_version=tunnel.ike_version,
        proposal=tunnel.proposal,
        local_identity=tunnel.local_identity,
        remote_identity=tunnel.remote_identity,
        local_networks=tunnel.local_networks,
        remote_networks=tunnel.remote_networks,
        esp_proposal=tunnel.esp_proposal,
        enabled=tunnel.enabled,
        pre_shared_key_set=tunnel.pre_shared_key_set,
        created_at=tunnel.created_at,
        updated_at=tunnel.updated_at,
        target_host_ids=[host.id for host in tunnel.targets],
        target_group_ids=[g.id for g in tunnel.groups],
        effective_host_ids=[h.id for h in effective_hosts(tunnel)],
        deployments=[
            IpsecDeploymentResult(
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


@router.get("", response_model=list[IpsecTunnelRead])
async def list_tunnels(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(IpsecTunnel).options(*_load_options()))
    tunnels = result.scalars().unique().all()
    return [_to_read(tunnel, tunnel.deployments) for tunnel in tunnels]


@router.post("", response_model=IpsecTunnelRead, status_code=201)
async def create_tunnel(payload: IpsecTunnelCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    hosts, groups = await resolve_targets(db, payload)

    tunnel = IpsecTunnel(
        **{field: getattr(payload, field) for field in IPSEC_FIELDS},
        targets=hosts,
        groups=groups,
    )
    db.add(tunnel)
    await db.flush()
    ipsec_psk_store.save_secret(tunnel.id, payload.pre_shared_key)
    tunnel.pre_shared_key_set = True
    revision(db, tunnel, "Tunnel angelegt")
    await db.commit()
    return _to_read(tunnel, [])


@router.put("/{tunnel_id}", response_model=IpsecTunnelRead)
async def edit_tunnel(tunnel_id: uuid.UUID, payload: IpsecTunnelEdit, db: AsyncSession = Depends(get_db)):
    tunnel = await _load_tunnel(db, tunnel_id)
    if tunnel.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.TUNNEL_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_50830F70)
    hosts, groups = await resolve_targets(db, payload)
    if not await db.scalar(select(IpsecTunnelRevision.id).where(IpsecTunnelRevision.tunnel_id == tunnel.id).limit(1)):
        revision(db, tunnel, "Vor erster Bearbeitung")
    for field in IPSEC_FIELDS:
        setattr(tunnel, field, getattr(payload, field))
    tunnel.targets, tunnel.groups = hosts, groups
    if payload.pre_shared_key is not None:
        ipsec_psk_store.save_secret(tunnel.id, payload.pre_shared_key)
        tunnel.pre_shared_key_set = True
    from datetime import datetime, timezone
    tunnel.updated_at = datetime.now(timezone.utc)
    revision(db, tunnel, "Entwurf gespeichert")
    await db.commit()
    return _to_read(tunnel, tunnel.deployments)


@router.get("/{tunnel_id}/preview")
async def preview_tunnel(tunnel_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return preview(await _load_tunnel(db, tunnel_id))


@router.get("/{tunnel_id}/revisions")
async def list_revisions(tunnel_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _load_tunnel(db, tunnel_id)
    rows = (await db.scalars(
        select(IpsecTunnelRevision).where(IpsecTunnelRevision.tunnel_id == tunnel_id)
        .order_by(IpsecTunnelRevision.created_at.desc(), IpsecTunnelRevision.id)
    )).all()
    return [{"id": r.id, "created_at": r.created_at, "note": r.note, "snapshot": r.snapshot} for r in rows]


@router.post("/{tunnel_id}/deploy", response_model=IpsecTunnelRead)
async def deploy(tunnel_id: uuid.UUID, approval: IpsecDeployApproval, db: AsyncSession = Depends(get_db)):
    tunnel = await _load_tunnel(db, tunnel_id)
    plan = preview(tunnel)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    if not tunnel.pre_shared_key_set:
        raise api_error(400, ErrorCode.FUER_DIESEN_TUNNEL_IST_NOCH_KEIN_PRE_SHARED_4AE1FC63)
    hosts = [h for h in effective_hosts(tunnel) if h.enabled]
    if not hosts:
        raise api_error(400, ErrorCode.TUNNEL_HAT_KEINE_ZIEL_HOSTS)
    await deploy_tunnel(db, tunnel, hosts)
    await db.refresh(tunnel, attribute_names=["deployments"])
    for d in tunnel.deployments:
        await db.refresh(d, attribute_names=["host"])
    return _to_read(tunnel, tunnel.deployments)


@router.delete("/{tunnel_id}", status_code=204)
async def delete_tunnel(tunnel_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    tunnel = await _load_tunnel(db, tunnel_id)
    try:
        await undeploy_tunnel(db, tunnel)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc
    await db.refresh(tunnel, attribute_names=["deployments"])
    ipsec_psk_store.delete_secret(tunnel.id)
    await db.delete(tunnel)
    await db.commit()


@router.post("/{tunnel_id}/deployments/{host_id}/remove", status_code=204)
async def remove_previous_target(tunnel_id: uuid.UUID, host_id: uuid.UUID, approval: IpsecDeployApproval, db: AsyncSession = Depends(get_db)):
    tunnel = await _load_tunnel(db, tunnel_id)
    plan = preview(tunnel)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ZIELE_ODER_DEPLOYMENT_WURDEN_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_remove"]:
        raise api_error(409, ErrorCode.NUR_FRUEHERE_AKTIVE_ZIELE_KOENNEN_BEREINIGT_WERDEN)
    async with resource_lease("ipsec", tunnel.id):
        deployment = next(d for d in tunnel.deployments if d.host_id == host_id)
        from app.services.host_maintenance_service import ensure_no_open_cleanup
        await ensure_no_open_cleanup(db, [deployment.host])  # a rule cleanup (1.31) owns the firewall: no other writes
        from app.services import opnsense_client
        try:
            if deployment.opnsense_child_uuid:
                await opnsense_client.del_ipsec_child(deployment.host, deployment.opnsense_child_uuid)
            if deployment.opnsense_local_uuid:
                await opnsense_client.del_ipsec_local(deployment.host, deployment.opnsense_local_uuid)
            if deployment.opnsense_remote_uuid:
                await opnsense_client.del_ipsec_remote(deployment.host, deployment.opnsense_remote_uuid)
            if deployment.opnsense_connection_uuid:
                await opnsense_client.del_ipsec_connection(deployment.host, deployment.opnsense_connection_uuid)
            if deployment.opnsense_psk_uuid:
                await opnsense_client.del_ipsec_psk(deployment.host, deployment.opnsense_psk_uuid)
            await opnsense_client.reconfigure_ipsec(deployment.host)
        except OpnsenseAPIError as exc:
            deployment.status = "error"
            deployment.message = str(exc)
            await db.commit()
            raise api_error(502, ErrorCode.ENTFERNEN_FEHLGESCHLAGEN_DIE_ZUORDNUNG_BLEIBT_FUER_EINEN_NEUEN_4CF90E4C) from exc
        await db.delete(deployment)
        await db.commit()
