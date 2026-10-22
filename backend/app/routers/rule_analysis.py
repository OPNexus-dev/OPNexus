"""Rule analysis: which rule decides a packet (read-only, GET so ``read`` tokens and MCP can use it).

Nothing is stored and nothing is written to a firewall; every request reads the rules, the
loaded pf ruleset and the interfaces (cached for 60 s per firewall, ``refresh=true`` skips
the cache) and evaluates them with services/rule_analysis/engine.py.
"""
import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup
from app.models.rule_history import LiveRuleState
from app.services import rule_history_service
from app.services.rule_analysis import service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["rule-analysis"])
_PARALLEL = 5


def _query(src: str, dst: str, proto: str, sport: str | None, dport: str | None, icmptype: str | None,
           iface: str | None, direction: str, egress: str | None):
    return service.build_query(src=src, dst=dst, proto=proto, sport=sport, dport=dport, icmptype=icmptype,
                               iface=iface, direction=direction, egress=egress)


async def _run(host: OpnsenseHost, q, refresh: bool) -> dict:
    head = {"host_id": str(host.id), "host_name": host.name, "cluster_name": host.cluster_name}
    try:
        return {**head, **await service.analyze_host(host, q, refresh=refresh), "error": None}
    except HTTPException as exc:
        return {**head, "error": exc.detail}
    except Exception as exc:  # noqa: BLE001 - one unreachable firewall must not fail the fleet answer
        logger.info("rule analysis failed for %s: %s", host.name, exc)
        return {**head, "error": {"code": ErrorCode.ANALYSE_FIREWALL_NICHT_LESBAR.value, "params": {}}}


@router.get("/hosts/{host_id}/rule-analysis/match")
async def host_match(
    host_id: uuid.UUID, src: str, dst: str, proto: str = "tcp", sport: str | None = None, dport: str | None = None,
    icmptype: str | None = None, iface: str | None = None, direction: str = "in", egress: str | None = None,
    refresh: bool = False, db: AsyncSession = Depends(get_db),
):
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    q = _query(src, dst, proto, sport, dport, icmptype, iface, direction, egress)
    result = await _run(host, q, refresh)
    error = result.get("error")
    if error:
        status = 422 if error.get("code") == ErrorCode.ANALYSE_INTERFACE_UNBEKANNT.value else 502
        raise HTTPException(status_code=status, detail=error)
    return result


@router.get("/rule-analysis/match")
async def fleet_match(
    src: str, dst: str, proto: str = "tcp", sport: str | None = None, dport: str | None = None,
    icmptype: str | None = None, iface: str | None = None, direction: str = "in", egress: str | None = None,
    host_ids: str | None = Query(None, description="comma separated host ids; default: all enabled hosts"),
    group_id: uuid.UUID | None = None, refresh: bool = False, db: AsyncSession = Depends(get_db),
):
    q = _query(src, dst, proto, sport, dport, icmptype, iface, direction, egress)
    if group_id is not None:
        group = await db.scalar(select(DeviceGroup).options(selectinload(DeviceGroup.hosts)).where(DeviceGroup.id == group_id))
        if group is None:
            raise api_error(404, ErrorCode.GRUPPE_NICHT_GEFUNDEN)
        hosts = sorted(group.hosts, key=lambda h: h.name)
    elif host_ids:
        try:
            ids = [uuid.UUID(part.strip()) for part in host_ids.split(",") if part.strip()]
        except ValueError:
            raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="host_ids", value=host_ids[:64]) from None
        hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.id.in_(ids)).order_by(OpnsenseHost.name))).all())
        if len(hosts) != len(set(ids)):
            raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    else:
        hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)).order_by(OpnsenseHost.name))).all())
    gate = asyncio.Semaphore(_PARALLEL)

    async def one(host):
        async with gate:
            return await _run(host, q, refresh)

    return list(await asyncio.gather(*(one(h) for h in hosts)))


@router.get("/hosts/{host_id}/rule-analysis/cleanup")
async def host_cleanup(
    host_id: uuid.UUID, unused_days: int = Query(30, ge=1, le=3650, description="observation window for 'unused' / 'disabled for a long time'"),
    refresh: bool = False, db: AsyncSession = Depends(get_db),
):
    """Cleanup candidates (unused, shadowed, duplicate, disabled for a long time) with confidence and whether
    each rule could be offered for removal. Read-only; removal is not part of this endpoint."""
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    try:
        return await service.cleanup_candidates(db, host, unused_days=unused_days, refresh=refresh)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreachable firewall is a 502, not a crash
        logger.info("cleanup candidates failed for %s: %s", host.name, exc)
        raise api_error(502, ErrorCode.ANALYSE_FIREWALL_NICHT_LESBAR) from exc


@router.get("/hosts/{host_id}/live-rules/{rule_uuid}/history")
async def live_rule_history(host_id: uuid.UUID, rule_uuid: str, limit: int = Query(100, ge=1, le=500),
                            db: AsyncSession = Depends(get_db)):
    """What happened to one rule on one firewall: observed changes (also hand-made ones), saved revisions of
    rules OPNexus manages and the audit entries, newest first. Reads the database only."""
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    baseline = await db.get(LiveRuleState, (host_id, rule_history_service.BASELINE_KEY))
    return {
        "host_id": str(host.id), "rule_uuid": rule_uuid.lower()[:64],
        "watching_since": baseline.first_seen_at if baseline else None,
        "events": await rule_history_service.rule_timeline(db, host, rule_uuid, limit),
    }
