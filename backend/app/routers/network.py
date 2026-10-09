import re
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.routers.hosts import _upstream_error
from app.schemas.network import (
    HostNetwork, NetworkChangeExecute, NetworkChangePreview, NetworkChangeRequest, NetworkChangeResult,
)
from app.services import network_change_service, network_service
from app.services.policy_service import policy_lock
from app.services.opnsense_client import OpnsenseAPIError

router = APIRouter(prefix="/hosts/{host_id}/network", tags=["network"])


@router.get("", response_model=HostNetwork)
async def host_network(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    try:
        return await network_service.get_host_network(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


async def _host_or_404(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


@router.post("/changes/preview", response_model=NetworkChangePreview)
async def preview_change(host_id: uuid.UUID, payload: NetworkChangeRequest, db: AsyncSession = Depends(get_db)):
    host = await _host_or_404(db, host_id)
    try:
        plan = await network_change_service.preview(db, host, payload.kind, payload.action, payload.uuid, payload.fields)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc
    return {k: v for k, v in plan.items() if not k.startswith("_")}


@router.post("/changes", response_model=NetworkChangeResult)
async def execute_change(host_id: uuid.UUID, payload: NetworkChangeExecute, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await _host_or_404(db, host_id)
    return await network_change_service.execute(
        db, host, payload.kind, payload.action, payload.uuid, payload.fields, payload.preview_token, payload.acknowledge)


@router.get("/template/{kind}")
async def change_template(host_id: uuid.UUID, kind: str, db: AsyncSession = Depends(get_db)):
    """Blank form for one kind, with the choices this firewall accepts for its select fields."""
    if kind not in network_change_service.FIELDS:
        raise api_error(404, ErrorCode.NETWORK_ITEM_NOT_FOUND)
    host = await _host_or_404(db, host_id)
    try:
        return await network_change_service.template(host, kind)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.get("/item/{kind}/{item_uuid}")
async def current_item(host_id: uuid.UUID, kind: str, item_uuid: str, db: AsyncSession = Depends(get_db)):
    """The object's current values on the firewall, for pre-filling an edit form."""
    if kind not in network_change_service.FIELDS or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", item_uuid):
        raise api_error(404, ErrorCode.NETWORK_ITEM_NOT_FOUND)
    host = await _host_or_404(db, host_id)
    try:
        return await network_change_service.item(host, kind, item_uuid)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.get("/objects/{kind}")
async def list_objects(host_id: uuid.UUID, kind: str, db: AsyncSession = Depends(get_db)):
    """Rows of a generic object kind (tunables, cron jobs, LAGG, ...), reduced to their display columns."""
    if kind not in network_change_service.OBJECT_COLUMNS:
        raise api_error(404, ErrorCode.NETWORK_ITEM_NOT_FOUND)
    host = await _host_or_404(db, host_id)
    try:
        return await network_change_service.list_objects(host, kind)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc
