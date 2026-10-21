import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.models.resource_sample import HostResourceSample
from app.schemas.gateway import ApiRightsState, GatewayAlertState, GatewayAlertUpdate, GatewayHealth
from app.services import audit_service, gateway_health_service, notification_settings_service
from app.services.monitor_checks import STALE_AFTER
from app.services.opnsense_client import OpnsenseAPIError, OpnsenseForbiddenError

router = APIRouter(prefix="/hosts/{host_id}/gateways", tags=["host-gateways"])

# OPNsense gateway names are letters, digits and underscores (WAN_GW, WAN_DHCP6); a little more is allowed.
_GATEWAY_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_MUTED_GATEWAYS = 200


@router.get("", response_model=list[GatewayHealth])
async def list_gateways(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Live gateway state (loss, delay, deviation, OPNsense' verdict) of one firewall."""
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    try:
        gateways = await gateway_health_service.get_gateway_health(host)
    except OpnsenseForbiddenError as exc:
        # The API key may not read the gateway state: say so instead of a generic upstream failure.
        raise api_error(403, ErrorCode.API_RIGHTS_MISSING) from exc
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc
    muted = set((await notification_settings_service.get_settings(db)).gateway_alert_muted or [])
    return [g.model_copy(update={"alert_enabled": f"{host.id}:{g.name}" not in muted}) for g in gateways]


@router.get("/api-rights", response_model=ApiRightsState)
async def api_rights(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Side readings the newest FRESH resource sample could not take because the API key lacks the right
    (null = no fresh sample, so nothing is known). Stored data only: no call to the firewall."""
    if await db.get(OpnsenseHost, host_id) is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    row = (
        await db.execute(
            select(HostResourceSample.sampled_at, HostResourceSample.raw["denied"].as_json())
            .where(HostResourceSample.host_id == host_id)
            .order_by(HostResourceSample.sampled_at.desc())
            .limit(1)
        )
    ).first()
    if row is None or datetime.now(timezone.utc) - row[0] > STALE_AFTER or not isinstance(row[1], list):
        return ApiRightsState(missing=None)
    return ApiRightsState(missing=[str(x) for x in row[1]])


@router.put("/{name}/alert", response_model=GatewayAlertState)
async def set_gateway_alert(host_id: uuid.UUID, name: str, payload: GatewayAlertUpdate, db: AsyncSession = Depends(get_db)):
    """Switch the gateway-down alert on or off for ONE gateway (admins only, see api_token_service)."""
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    if not _GATEWAY_NAME.fullmatch(name):
        raise api_error(422, ErrorCode.GATEWAY_NAME_INVALID)
    if not payload.enabled:
        # Only a gateway the firewall really reported can be muted ("wan_gw" vs "WAN_GW" would silently do nothing).
        # Without a usable sample nothing is known, so the request goes through.
        known = (
            await db.execute(
                select(HostResourceSample.raw["gateways"].as_json())
                .where(HostResourceSample.host_id == host_id)
                .order_by(HostResourceSample.sampled_at.desc())
                .limit(1)
            )
        ).scalar()
        if isinstance(known, list) and name not in {g.get("name") for g in known if isinstance(g, dict)}:
            raise api_error(404, ErrorCode.GATEWAY_UNKNOWN)
    row = await notification_settings_service.get_settings(db)
    await db.refresh(row, with_for_update=True)
    muted = list(row.gateway_alert_muted or [])
    key = f"{host.id}:{name}"
    if payload.enabled:
        muted = [m for m in muted if m != key]
    elif key not in muted:
        if len(muted) >= MAX_MUTED_GATEWAYS:
            raise api_error(422, ErrorCode.GATEWAY_MUTE_LIMIT, limit=str(MAX_MUTED_GATEWAYS))
        muted.append(key)
    row.gateway_alert_muted = muted  # a new list, so the JSON column notices the change
    await db.commit()
    # The request middleware logs the call itself; this entry says what it did (only the validated name goes in).
    await audit_service.persist(
        kind="host", action="gateway_alert_on" if payload.enabled else "gateway_alert_off", entity_id=str(host.id),
        host_id=str(host.id), host_name=host.name, status="accepted", message=f"Gateway {name}",
    )
    return GatewayAlertState(name=name, alert_enabled=payload.enabled)
