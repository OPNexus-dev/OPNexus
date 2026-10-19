"""GET /api/metrics — Prometheus text exposition (see services/metrics_service.py).

Authenticated like everything else; the intended client is a Prometheus job
with ``authorization: {credentials: <read token>}``.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import metrics_service

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(db: AsyncSession = Depends(get_db)):
    return PlainTextResponse(await metrics_service.render(db), media_type=metrics_service.CONTENT_TYPE)
