from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.audit import AuditEvent

router = APIRouter(prefix="/audit", tags=["audit"])

@router.get("")
async def history(kind: str | None = None, host_id: str | None = None, entity_id: str | None = None,
                  status: str | None = None, action: str | None = None, actor: str | None = None,
                  before: datetime | None = None, since: datetime | None = None,
                  q: str = Query(default="", max_length=200), offset: int = Query(default=0, ge=0),
                  limit: int = Query(default=50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    clauses = []
    for column, value in [(AuditEvent.kind,kind), (AuditEvent.host_id,host_id), (AuditEvent.entity_id,entity_id),
                          (AuditEvent.status,status), (AuditEvent.action,action), (AuditEvent.actor,actor)]:
        if value:
            clauses.append(column == value)
    if before: clauses.append(AuditEvent.created_at <= before)
    if since: clauses.append(AuditEvent.created_at >= since)
    if q:
        clauses.append(or_(*(c.ilike('%'+q+'%') for c in [AuditEvent.entity_name, AuditEvent.host_name, AuditEvent.actor, AuditEvent.message])))
    total = await db.scalar(select(func.count()).select_from(AuditEvent).where(*clauses))
    rows = (await db.scalars(select(AuditEvent).where(*clauses).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset(offset).limit(limit))).all()
    return {"total": total, "offset": offset, "limit": limit,
            "items": [{c.name: getattr(row, c.name) for c in AuditEvent.__table__.columns} for row in rows]}
