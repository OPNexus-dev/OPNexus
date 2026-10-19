"""Security posture ("Sicherheitslage"): stored results and on-demand checks.

Reads (GET) only return what the last check stored, so they are cheap and safe
for dashboards, Prometheus and MCP. A check (POST) downloads the host's config
read-only, evaluates it and stores the result; see services/compliance_service.py.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.compliance_result import ComplianceResult
from app.models.host import OpnsenseHost
from app.services import compliance_service

router = APIRouter(tags=["compliance"])


def _read(host: OpnsenseHost, row: ComplianceResult | None, *, detail: bool) -> dict:
    result = {
        "host_id": host.id, "host_name": host.name, "cluster_name": host.cluster_name, "enabled": host.enabled,
        "checked_at": row.checked_at if row else None,
        "score": row.score if row else None,
        "grade": compliance_service.grade(row.score) if row and row.score is not None else None,
        "error": row.error if row else None,
        "counts": compliance_service.counts(row.findings) if row else None,
    }
    if detail:
        result["findings"] = row.findings if row else []
    return result


@router.get("/compliance")
async def fleet_compliance(db: AsyncSession = Depends(get_db)):
    hosts = (await db.scalars(select(OpnsenseHost).order_by(OpnsenseHost.name))).all()
    rows = {r.host_id: r for r in (await db.scalars(select(ComplianceResult))).all()}
    return [_read(host, rows.get(host.id), detail=False) for host in hosts]


@router.post("/compliance/check")
async def check_fleet(db: AsyncSession = Depends(get_db)):
    await compliance_service.check_all(db)
    return await fleet_compliance(db)


async def _host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


@router.get("/hosts/{host_id}/compliance")
async def host_compliance(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _host(db, host_id)
    row = await db.scalar(select(ComplianceResult).where(ComplianceResult.host_id == host_id))
    return _read(host, row, detail=True)


@router.post("/hosts/{host_id}/compliance/check")
async def check_host(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _host(db, host_id)
    row = await compliance_service.check_host(db, host)
    return _read(host, row, detail=True)
