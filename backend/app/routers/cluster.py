from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.carp_event import CarpEvent
from app.schemas.carp_event import CarpEventRead

router = APIRouter(prefix="/cluster", tags=["cluster"])


@router.get("/carp-events", response_model=list[CarpEventRead])
async def list_carp_events(
    cluster_name: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    clauses = [] if cluster_name is None else [CarpEvent.cluster_name == cluster_name]
    result = await db.execute(
        select(CarpEvent).where(*clauses).order_by(CarpEvent.detected_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
