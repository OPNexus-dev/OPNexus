"""Management overlay API (see services/overlay_service.py). Writes and the pre-shared key are admin-only."""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.models.overlay import OverlayPeer
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.schemas.overlay import OverlaySettingsRead, OverlaySettingsUpdate, PeerCreate, PeerRead, PeerUpdate
from app.services import overlay_service as svc

router = APIRouter(prefix="/overlay", tags=["overlay"])


def _require_admin(user: AdminUser) -> None:
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)


async def _settings_read(db: AsyncSession) -> dict:
    cfg = await svc.get_settings(db)
    net = svc.parse_subnet(cfg.subnet)
    return {
        "enabled": cfg.enabled, "endpoint": cfg.endpoint, "subnet": cfg.subnet, "listen_port": cfg.listen_port,
        "hub_public_key": svc.hub_public_key() if cfg.enabled else None, "hub_address": str(svc.hub_address(net)),
        "hub_state": svc.hub_state(svc.read_status()),
    }


async def _peers(db: AsyncSession) -> list[dict]:
    status = svc.read_status()
    rows = (await db.execute(select(OverlayPeer, OpnsenseHost.name).outerjoin(OpnsenseHost, OverlayPeer.host_id == OpnsenseHost.id).order_by(OverlayPeer.ip))).all()
    return [
        {"id": p.id, "name": p.name, "ip": p.ip, "public_key": p.public_key, "host_id": p.host_id, "host_name": host_name,
         **svc.peer_status(status, p.public_key)}
        for p, host_name in rows
    ]


@router.get("")
async def overview(db: AsyncSession = Depends(get_db)):
    return {"settings": await _settings_read(db), "peers": await _peers(db)}


@router.put("/settings", response_model=OverlaySettingsRead)
async def update_settings(payload: OverlaySettingsUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    cfg = await svc.get_settings(db)
    if payload.subnet is not None and payload.subnet.strip() != cfg.subnet:
        try:
            svc.parse_subnet(payload.subnet)
        except svc.OverlayError as exc:
            raise api_error(422, ErrorCode.OVERLAY_INVALID, reason=str(exc)) from exc
        if (await db.scalar(select(OverlayPeer.id).limit(1))) is not None:
            raise api_error(409, ErrorCode.OVERLAY_SUBNET_LOCKED)  # peers already hold addresses in the old network
        cfg.subnet = payload.subnet.strip()
    for field in ("enabled", "listen_port"):
        value = getattr(payload, field)
        if value is not None:
            setattr(cfg, field, value)
    if payload.endpoint is not None:
        cfg.endpoint = payload.endpoint.strip() or None
    if cfg.enabled and not cfg.endpoint:
        raise api_error(422, ErrorCode.OVERLAY_INVALID, reason="Ohne Endpunkt wissen die Firewalls nicht, wohin sie sich verbinden sollen")
    await db.commit()
    await svc.sync(db)
    return await _settings_read(db)


@router.post("/peers", status_code=201)
async def create_peer(payload: PeerCreate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    cfg = await svc.get_settings(db)
    taken = set((await db.scalars(select(OverlayPeer.ip))).all())
    try:
        ip = svc.next_free_ip(svc.parse_subnet(cfg.subnet), taken)
    except svc.OverlayError as exc:
        raise api_error(409, ErrorCode.OVERLAY_INVALID, reason=str(exc)) from exc
    peer = OverlayPeer(name=payload.name.strip(), ip=ip)
    db.add(peer)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise api_error(409, ErrorCode.OVERLAY_NAME_TAKEN) from None
    await svc.sync(db)
    return {"id": peer.id, "name": peer.name, "ip": peer.ip, "instructions": svc.instructions(cfg, peer)}


@router.get("/peers/{peer_id}/instructions")
async def peer_instructions(peer_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    peer = await db.get(OverlayPeer, peer_id)
    if peer is None:
        raise api_error(404, ErrorCode.OVERLAY_PEER_NOT_FOUND)
    return svc.instructions(await svc.get_settings(db), peer)


@router.patch("/peers/{peer_id}")
async def update_peer(peer_id: uuid.UUID, payload: PeerUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    peer = await db.get(OverlayPeer, peer_id)
    if peer is None:
        raise api_error(404, ErrorCode.OVERLAY_PEER_NOT_FOUND)
    if payload.public_key is not None:
        key = payload.public_key.strip()
        if key and not svc.valid_public_key(key):
            raise api_error(422, ErrorCode.OVERLAY_INVALID, reason="Das ist kein WireGuard-Schlüssel (44 Zeichen, Base64)")
        if key and key == svc.hub_public_key():
            raise api_error(422, ErrorCode.OVERLAY_INVALID, reason="Das ist der Schlüssel des Hubs, nicht der der Firewall")
        peer.public_key = key or None
    if payload.clear_host:
        peer.host_id = None
    elif payload.host_id is not None:
        if await db.get(OpnsenseHost, payload.host_id) is None:
            raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
        peer.host_id = payload.host_id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise api_error(409, ErrorCode.OVERLAY_INVALID, reason="Dieser Schlüssel gehört schon zu einem anderen Eintrag") from None
    await svc.sync(db)
    return {"ok": True}


@router.delete("/peers/{peer_id}", status_code=204)
async def delete_peer(peer_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    peer = await db.get(OverlayPeer, peer_id)
    if peer is None:
        raise api_error(404, ErrorCode.OVERLAY_PEER_NOT_FOUND)
    await db.delete(peer)
    await db.commit()
    svc.delete_peer_secret(peer_id)
    await svc.sync(db)
