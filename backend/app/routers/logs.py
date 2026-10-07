import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.host import OpnsenseHost
from app.models.log_entry import FirewallLogEntry
from app.schemas.logs import LogFilterOptions, LogSearchResult, LogSettingsRead, LogSettingsUpdate, LogStatCount, LogStats
from app.services import log_ingest_service, log_settings_service

router = APIRouter(prefix="/logs", tags=["logs"])


async def _settings_with_usage(db: AsyncSession, row) -> LogSettingsRead:
    usage = await log_ingest_service.storage_usage(db)
    data = LogSettingsRead.model_validate(row)
    oldest = await db.scalar(select(func.min(FirewallLogEntry.timestamp)))
    return data.model_copy(update={"used_bytes": usage["used_bytes"], "entries": usage["entries"], "oldest_at": oldest})


@router.get("/settings", response_model=LogSettingsRead)
async def read_log_settings(db: AsyncSession = Depends(get_db)):
    return await _settings_with_usage(db, await log_settings_service.get_settings(db))


@router.put("/settings", response_model=LogSettingsRead)
async def update_log_settings(payload: LogSettingsUpdate, db: AsyncSession = Depends(get_db)):
    return await _settings_with_usage(db, await log_settings_service.update_settings(db, payload))

# Aggregated over the same filter set as /logs itself (host/cluster/time
# range), not a generic analytics engine — a handful of fixed queries.
_TOP_N = 10
# Actions counted as "blocked" for the top-sources aggregate. OPNsense's
# firewall log action vocabulary is effectively pass/block/reject.
_BLOCKED_ACTIONS = ("block", "reject")


def _base_filters(
    host_id: uuid.UUID | None,
    action: str | None,
    interface: str | None,
    protocol: str | None,
    q: str,
    since: datetime | None,
    until: datetime | None,
) -> list:
    clauses = []
    if host_id is not None:
        clauses.append(FirewallLogEntry.host_id == host_id)
    if action:
        clauses.append(FirewallLogEntry.action == action)
    if interface:
        clauses.append(FirewallLogEntry.interface == interface)
    if protocol:
        clauses.append(FirewallLogEntry.protocol == protocol)
    if since:
        clauses.append(FirewallLogEntry.timestamp >= since)
    if until:
        clauses.append(FirewallLogEntry.timestamp <= until)
    if q:
        pattern = f"%{q}%"
        clauses.append(or_(
            FirewallLogEntry.label.ilike(pattern),
            FirewallLogEntry.source_net.ilike(pattern),
            FirewallLogEntry.destination_net.ilike(pattern),
        ))
    return clauses


async def _cluster_host_ids(db: AsyncSession, cluster_name: str) -> list[uuid.UUID]:
    result = await db.execute(select(OpnsenseHost.id).where(OpnsenseHost.cluster_name == cluster_name))
    return [row[0] for row in result.all()]


@router.get("", response_model=LogSearchResult)
async def search_logs(
    host_id: uuid.UUID | None = None,
    cluster_name: str | None = None,
    q: str = Query(default="", max_length=200),
    action: str | None = None,
    interface: str | None = None,
    protocol: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    clauses = _base_filters(host_id, action, interface, protocol, q, since, until)
    if cluster_name:
        clauses.append(FirewallLogEntry.host_id.in_(await _cluster_host_ids(db, cluster_name)))

    total = await db.scalar(select(func.count()).select_from(FirewallLogEntry).where(*clauses))
    rows = (await db.scalars(
        select(FirewallLogEntry)
        .where(*clauses)
        .order_by(FirewallLogEntry.timestamp.desc(), FirewallLogEntry.id.desc())
        .offset(offset)
        .limit(limit)
    )).all()
    return LogSearchResult(total=total or 0, offset=offset, limit=limit, items=list(rows))


@router.get("/stats", response_model=LogStats)
async def log_stats(
    host_id: uuid.UUID | None = None,
    cluster_name: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    db: AsyncSession = Depends(get_db),
):
    clauses = _base_filters(host_id, None, None, None, "", since, until)
    if cluster_name:
        clauses.append(FirewallLogEntry.host_id.in_(await _cluster_host_ids(db, cluster_name)))

    by_action_rows = (await db.execute(
        select(FirewallLogEntry.action, func.count())
        .where(*clauses)
        .group_by(FirewallLogEntry.action)
    )).all()
    by_interface_rows = (await db.execute(
        select(FirewallLogEntry.interface, func.count())
        .where(*clauses)
        .group_by(FirewallLogEntry.interface)
    )).all()

    source_count = func.count().label("source_count")
    top_sources_rows = (await db.execute(
        select(FirewallLogEntry.source_net, source_count)
        .where(*clauses, FirewallLogEntry.action.in_(_BLOCKED_ACTIONS), FirewallLogEntry.source_net.is_not(None))
        .group_by(FirewallLogEntry.source_net)
        .order_by(source_count.desc())
        .limit(_TOP_N)
    )).all()

    port_count = func.count().label("port_count")
    top_ports_rows = (await db.execute(
        select(FirewallLogEntry.destination_port, port_count)
        .where(*clauses, FirewallLogEntry.destination_port.is_not(None))
        .group_by(FirewallLogEntry.destination_port)
        .order_by(port_count.desc())
        .limit(_TOP_N)
    )).all()

    return LogStats(
        by_action={(a or "unbekannt"): c for a, c in by_action_rows},
        by_interface={(i or "unbekannt"): c for i, c in by_interface_rows},
        top_blocked_sources=[LogStatCount(value=s, count=c) for s, c in top_sources_rows],
        top_destination_ports=[LogStatCount(value=p, count=c) for p, c in top_ports_rows],
    )


@router.get("/filter-options", response_model=LogFilterOptions)
async def filter_options(db: AsyncSession = Depends(get_db)):
    """Distinct action/interface/protocol values from OPNexus's own
    persisted log data — replaces the old live viewer's reliance on
    OPNsense's `logFiltersAction`, since that endpoint only ever reflects
    one host at a time and this feature now searches across all of them."""
    actions = (await db.scalars(
        select(FirewallLogEntry.action).where(FirewallLogEntry.action.is_not(None)).distinct()
    )).all()
    interfaces = (await db.scalars(
        select(FirewallLogEntry.interface).where(FirewallLogEntry.interface.is_not(None)).distinct()
    )).all()
    protocols = (await db.scalars(
        select(FirewallLogEntry.protocol).where(FirewallLogEntry.protocol.is_not(None)).distinct()
    )).all()
    return LogFilterOptions(
        actions=sorted(actions), interfaces=sorted(interfaces), protocols=sorted(protocols),
    )
