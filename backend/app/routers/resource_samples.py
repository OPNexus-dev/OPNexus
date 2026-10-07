import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.resource_sample import HostResourceSample

router = APIRouter(prefix="/hosts/{host_id}/resource-samples", tags=["host-resources"])


def _read(sample: HostResourceSample) -> dict:
    return {
        "id": str(sample.id),
        "sampled_at": sample.sampled_at,
        "mem_used_pct": sample.mem_used_pct,
        "load_avg_1m": sample.load_avg_1m,
        "disk_used_pct": sample.disk_used_pct,
        "raw": sample.raw,
    }


@router.get("")
async def list_samples(
    host_id: uuid.UUID,
    since_hours: int = Query(default=24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    rows = (
        await db.scalars(
            select(HostResourceSample)
            .where(HostResourceSample.host_id == host_id, HostResourceSample.sampled_at >= cutoff)
            .order_by(HostResourceSample.sampled_at.asc())
        )
    ).all()
    return [_read(r) for r in rows]
