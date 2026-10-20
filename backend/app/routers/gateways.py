import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.schemas.gateway import GatewayHealth
from app.services import gateway_health_service
from app.services.opnsense_client import OpnsenseAPIError

router = APIRouter(prefix="/hosts/{host_id}/gateways", tags=["host-gateways"])


@router.get("", response_model=list[GatewayHealth])
async def list_gateways(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Live gateway state (loss, delay, deviation, OPNsense' verdict) of one firewall."""
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    try:
        return await gateway_health_service.get_gateway_health(host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc
