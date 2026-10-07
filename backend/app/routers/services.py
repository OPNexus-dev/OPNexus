import uuid
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models import OpnsenseHost
from app.services import opnsense_client as client, audit_service
from app.services.host_maintenance_service import ensure_no_maintenance
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/hosts/{host_id}/services", tags=["host-services"])


async def host_or_404(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


@router.get("")
async def list_services(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await host_or_404(db, host_id)
    try:
        return await client.search_services(host)
    except client.OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc


async def _control(host_id: uuid.UUID, name: str, action: str, fn, db: AsyncSession):
    await policy_lock(db)
    host = await host_or_404(db, host_id)
    await ensure_no_maintenance(db, [host])
    await audit_service.persist(
        kind="host", action=action, status="requested",
        host_id=str(host.id), host_name=host.name,
        details={"service": name},
    )
    try:
        result = await fn(host, name)
        message = f"Dienst '{name}' {action} übergeben."
        await audit_service.persist(
            kind="host", action=action, status="accepted",
            host_id=str(host.id), host_name=host.name,
            message=message, details={"service": name, "response": result},
        )
        return result
    except client.OpnsenseAPIError as exc:
        await audit_service.persist(
            kind="host", action=action, status="rejected",
            host_id=str(host.id), host_name=host.name,
            message=str(exc)[:800], details={"service": name},
        )
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc


@router.post("/{name}/start")
async def start_service(host_id: uuid.UUID, name: str, db: AsyncSession = Depends(get_db)):
    return await _control(host_id, name, "start", client.start_service, db)


@router.post("/{name}/stop")
async def stop_service(host_id: uuid.UUID, name: str, db: AsyncSession = Depends(get_db)):
    return await _control(host_id, name, "stop", client.stop_service, db)


@router.post("/{name}/restart")
async def restart_service(host_id: uuid.UUID, name: str, db: AsyncSession = Depends(get_db)):
    return await _control(host_id, name, "restart", client.restart_service, db)
