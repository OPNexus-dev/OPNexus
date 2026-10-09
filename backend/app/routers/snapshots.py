import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.routers.hosts import _upstream_error
from app.services import snapshot_service
from app.services.host_maintenance_service import ensure_no_maintenance
from app.services.opnsense_client import OpnsenseAPIError
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/hosts/{host_id}/snapshots", tags=["snapshots"])


class SnapshotCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=200)


async def _host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


@router.get("")
async def list_snapshots(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _host(db, host_id)
    try:
        return await snapshot_service.list_snapshots(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.post("", status_code=201)
async def create_snapshot(host_id: uuid.UUID, payload: SnapshotCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await _host(db, host_id)
    await ensure_no_maintenance(db, [host])
    return await snapshot_service.create(host, payload.name, payload.note)


@router.delete("/{snapshot_uuid}", status_code=204)
async def delete_snapshot(host_id: uuid.UUID, snapshot_uuid: str, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await _host(db, host_id)
    await ensure_no_maintenance(db, [host])
    if not snapshot_uuid.replace("-", "").isalnum() or len(snapshot_uuid) > 64:
        raise api_error(404, ErrorCode.NETWORK_ITEM_NOT_FOUND)
    await snapshot_service.delete(host, snapshot_uuid)
