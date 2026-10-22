import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup, WireguardTunnelRevision
from app.models.wireguard_tunnel import WireguardTunnel
from app.models.wireguard_tunnel_deployment import WireguardTunnelDeployment
from app.schemas.wireguard import (
    WireguardDeployApproval, WireguardDeploymentResult, WireguardTunnelCreate, WireguardTunnelEdit, WireguardTunnelRead,
)
from app.services import wireguard_key_store
from app.services.wireguard_service import effective_hosts, preview, revision, snapshot, workflow, WIREGUARD_FIELDS
from app.services.wireguard_deploy_service import deploy_tunnel, undeploy_tunnel
from app.services.opnsense_client import OpnsenseAPIError
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/wireguard/tunnels", tags=["wireguard"])


def _load_options():
    return [
        selectinload(WireguardTunnel.targets),
        selectinload(WireguardTunnel.groups).selectinload(DeviceGroup.hosts),
        selectinload(WireguardTunnel.deployments).selectinload(WireguardTunnelDeployment.host),
    ]


async def _load_tunnel(db: AsyncSession, tunnel_id: uuid.UUID) -> WireguardTunnel:
    # Same advisory lock as rules/aliases/NAT/IPsec/OpenVPN — serializes
    # concurrent deploy-affecting changes across processes. No
    # ensure_editable() gate here either: this entity has no pending/
    # revert_failed state to ever block editing on (see
    # wireguard_deploy_service.py's module docstring).
    await policy_lock(db)
    tunnel = await db.get(WireguardTunnel, tunnel_id, options=_load_options())
    if not tunnel:
        raise api_error(404, ErrorCode.TUNNEL_NICHT_GEFUNDEN)
    return tunnel


def _to_read(tunnel: WireguardTunnel, deployments: list[WireguardTunnelDeployment]) -> WireguardTunnelRead:
    hosts_by_id = {host.id: host for host in effective_hosts(tunnel)}
    hosts_by_id.update({d.host_id: d.host for d in deployments if d.host is not None})
    state, counts = workflow(tunnel, deployments)
    return WireguardTunnelRead(
        workflow_state=state,
        workflow_counts=counts,
        id=tunnel.id,
        server_name=tunnel.server_name,
        server_public_key=tunnel.server_public_key,
        server_private_key_set=tunnel.server_private_key_set,
        listen_port=tunnel.listen_port,
        server_tunnel_address=tunnel.server_tunnel_address,
        dns=tunnel.dns,
        mtu=tunnel.mtu,
        peer_name=tunnel.peer_name,
        peer_public_key=tunnel.peer_public_key,
        peer_psk_enabled=tunnel.peer_psk_enabled,
        peer_psk_set=tunnel.peer_psk_set,
        peer_allowed_ips=tunnel.peer_allowed_ips,
        peer_endpoint_address=tunnel.peer_endpoint_address,
        peer_endpoint_port=tunnel.peer_endpoint_port,
        peer_keepalive=tunnel.peer_keepalive,
        enabled=tunnel.enabled,
        created_at=tunnel.created_at,
        updated_at=tunnel.updated_at,
        target_host_ids=[host.id for host in tunnel.targets],
        target_group_ids=[g.id for g in tunnel.groups],
        effective_host_ids=[h.id for h in effective_hosts(tunnel)],
        deployments=[
            WireguardDeploymentResult(
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


@router.get("", response_model=list[WireguardTunnelRead])
async def list_tunnels(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(WireguardTunnel).options(*_load_options()))
    tunnels = result.scalars().unique().all()
    return [_to_read(tunnel, tunnel.deployments) for tunnel in tunnels]


@router.post("", response_model=WireguardTunnelRead, status_code=201)
async def create_tunnel(payload: WireguardTunnelCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    hosts, groups = await resolve_targets(db, payload)

    tunnel = WireguardTunnel(
        **{field: getattr(payload, field) for field in WIREGUARD_FIELDS},
        targets=hosts,
        groups=groups,
    )
    db.add(tunnel)
    await db.flush()
    wireguard_key_store.save_private_key(tunnel.id, payload.server_private_key)
    tunnel.server_private_key_set = True
    if payload.peer_psk_material:
        wireguard_key_store.save_psk(tunnel.id, payload.peer_psk_material)
        tunnel.peer_psk_set = True
    revision(db, tunnel, "Tunnel angelegt")
    await db.commit()
    return _to_read(tunnel, [])


@router.put("/{tunnel_id}", response_model=WireguardTunnelRead)
async def edit_tunnel(tunnel_id: uuid.UUID, payload: WireguardTunnelEdit, db: AsyncSession = Depends(get_db)):
    tunnel = await _load_tunnel(db, tunnel_id)
    if tunnel.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.TUNNEL_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_50830F70)
    hosts, groups = await resolve_targets(db, payload)
    if not await db.scalar(select(WireguardTunnelRevision.id).where(WireguardTunnelRevision.tunnel_id == tunnel.id).limit(1)):
        revision(db, tunnel, "Vor erster Bearbeitung")
    for field in WIREGUARD_FIELDS:
        setattr(tunnel, field, getattr(payload, field))
    tunnel.targets, tunnel.groups = hosts, groups
    if payload.server_private_key is not None:
        wireguard_key_store.save_private_key(tunnel.id, payload.server_private_key)
        tunnel.server_private_key_set = True
    if payload.peer_psk_material is not None:
        wireguard_key_store.save_psk(tunnel.id, payload.peer_psk_material)
        tunnel.peer_psk_set = True
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
        select(WireguardTunnelRevision).where(WireguardTunnelRevision.tunnel_id == tunnel_id)
        .order_by(WireguardTunnelRevision.created_at.desc(), WireguardTunnelRevision.id)
    )).all()
    return [{"id": r.id, "created_at": r.created_at, "note": r.note, "snapshot": r.snapshot} for r in rows]


@router.post("/{tunnel_id}/deploy", response_model=WireguardTunnelRead)
async def deploy(tunnel_id: uuid.UUID, approval: WireguardDeployApproval, db: AsyncSession = Depends(get_db)):
    tunnel = await _load_tunnel(db, tunnel_id)
    plan = preview(tunnel)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    if not tunnel.server_private_key_set:
        raise api_error(400, ErrorCode.FUER_DIESEN_TUNNEL_IST_NOCH_KEIN_SERVER_PRIVATSCHLUESSEL_D7527998)
    if tunnel.peer_psk_enabled and not tunnel.peer_psk_set:
        raise api_error(400, ErrorCode.PEER_PRE_SHARED_KEY_IST_AKTIVIERT_ABER_ES_FCFAC129)
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
    wireguard_key_store.delete_private_key(tunnel.id)
    wireguard_key_store.delete_psk(tunnel.id)
    await db.delete(tunnel)
    await db.commit()


@router.post("/{tunnel_id}/deployments/{host_id}/remove", status_code=204)
async def remove_previous_target(tunnel_id: uuid.UUID, host_id: uuid.UUID, approval: WireguardDeployApproval, db: AsyncSession = Depends(get_db)):
    tunnel = await _load_tunnel(db, tunnel_id)
    plan = preview(tunnel)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ZIELE_ODER_DEPLOYMENT_WURDEN_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_remove"]:
        raise api_error(409, ErrorCode.NUR_FRUEHERE_AKTIVE_ZIELE_KOENNEN_BEREINIGT_WERDEN)
    deployment = next(d for d in tunnel.deployments if d.host_id == host_id)
    from app.services.host_maintenance_service import ensure_no_open_cleanup
    await ensure_no_open_cleanup(db, [deployment.host])  # a rule cleanup (1.31) owns the firewall: no other writes
    from app.services import opnsense_client
    try:
        if deployment.opnsense_peer_uuid:
            await opnsense_client.del_wireguard_peer(deployment.host, deployment.opnsense_peer_uuid)
        if deployment.opnsense_server_uuid:
            await opnsense_client.del_wireguard_server(deployment.host, deployment.opnsense_server_uuid)
        await opnsense_client.reconfigure_wireguard(deployment.host)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        await db.commit()
        raise api_error(502, ErrorCode.ENTFERNEN_FEHLGESCHLAGEN_DIE_ZUORDNUNG_BLEIBT_FUER_EINEN_NEUEN_4CF90E4C) from exc
    await db.delete(deployment)
    await db.commit()
