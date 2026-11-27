from datetime import datetime
from typing import Literal
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.audit import AuditEvent
from app.services.audit_group_service import Step, summarize
from app.services.audit_service import ADMIN_ONLY_DETAILS

router = APIRouter(prefix="/audit", tags=["audit"])

@router.get("")
async def history(request: Request, kind: str | None = None, host_id: str | None = None, entity_id: str | None = None,
                  status: str | None = None, action: str | None = None, actor: str | None = None,
                  before: datetime | None = None, since: datetime | None = None, correlation_id: str | None = Query(default=None, max_length=36),
                  group: Literal["operation"] | None = None,
                  q: str = Query(default="", max_length=200), offset: int = Query(default=0, ge=0),
                  limit: int = Query(default=50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    clauses = []
    for column, value in [(AuditEvent.kind,kind), (AuditEvent.host_id,host_id), (AuditEvent.entity_id,entity_id),
                          (AuditEvent.status,status), (AuditEvent.action,action), (AuditEvent.actor,actor),
                          (AuditEvent.correlation_id,correlation_id)]:
        if value:
            clauses.append(column == value)
    if before: clauses.append(AuditEvent.created_at <= before)
    if since: clauses.append(AuditEvent.created_at >= since)
    if q:
        clauses.append(or_(*(c.ilike('%'+q+'%') for c in [AuditEvent.entity_name, AuditEvent.host_name, AuditEvent.actor, AuditEvent.message])))
    if group == "operation":
        return await grouped(db, clauses, offset, limit)
    total = await db.scalar(select(func.count()).select_from(AuditEvent).where(*clauses))
    rows = (await db.scalars(select(AuditEvent).where(*clauses).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset(offset).limit(limit))).all()
    items = [{c.name: getattr(row, c.name) for c in AuditEvent.__table__.columns} for row in rows]
    if not full_view(request):
        for item in items:
            if isinstance(item.get("details"), dict) and ADMIN_ONLY_DETAILS in item["details"]:
                item["details"] = {**{k: v for k, v in item["details"].items() if k != ADMIN_ONLY_DETAILS},
                                   ADMIN_ONLY_DETAILS: "redacted"}
    return {"total": total, "offset": offset, "limit": limit, "items": items}


async def grouped(db: AsyncSession, clauses: list, offset: int, limit: int) -> dict:
    """One row per operation (``correlation_id``). The filters decide WHICH operations appear (an operation matches when any of its steps does);
    each shown operation is then summarised from ALL its steps, so the result does not depend on the filter. Read only; no details are returned."""
    matching = (select(AuditEvent.correlation_id.label("cid"), func.min(AuditEvent.created_at).label("started"))
                .where(*clauses).group_by(AuditEvent.correlation_id).subquery())
    total = await db.scalar(select(func.count()).select_from(matching))
    ids = (await db.scalars(select(matching.c.cid).order_by(matching.c.started.desc(), matching.c.cid.desc()).offset(offset).limit(limit))).all()
    if not ids:
        return {"total": total, "offset": offset, "limit": limit, "items": []}
    steps = await db.execute(
        select(AuditEvent.correlation_id, AuditEvent.created_at, AuditEvent.actor, AuditEvent.kind, AuditEvent.action, AuditEvent.status,
               AuditEvent.entity_id, AuditEvent.entity_name, AuditEvent.host_id, AuditEvent.host_name, AuditEvent.message,
               AuditEvent.details["comment"].as_string().label("comment"))
        .where(AuditEvent.correlation_id.in_(ids)).order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc()))
    by_operation: dict[str, list[Step]] = {i: [] for i in ids}
    for r in steps:
        by_operation[r.correlation_id].append(Step(r.created_at, r.actor, r.kind, r.action, r.status, r.entity_id, r.entity_name,
                                                   r.host_id, r.host_name, r.message, r.comment))
    items = [summarize(i, by_operation[i]) for i in ids if by_operation[i]]
    return {"total": total, "offset": offset, "limit": limit, "items": items}


def full_view(request: Request | None) -> bool:
    """An admin account, and with a token only one of scope admin."""
    user = getattr(getattr(request, "state", None), "user", None)
    token = getattr(getattr(request, "state", None), "api_token", None)
    return getattr(user, "role", None) == "admin" and (token is None or token.scope == "admin")
