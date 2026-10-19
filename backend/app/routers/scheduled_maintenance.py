import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models import OpnsenseHost
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services.host_maintenance_service import peers
from app.services.host_status_service import get_statuses
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/hosts/{host_id}/maintenance/schedule", tags=["host-maintenance"])


class Schedule(BaseModel):
    action: Literal["reboot", "update", "major_update"]
    run_at: datetime
    acknowledge_unattended: Literal[True]
    # Hand the traffic over to the CARP partner first and switch back afterwards (cluster nodes only).
    carp_handover: bool = False


def _read(sched: ScheduledMaintenance) -> dict:
    return {
        "id": sched.id, "host_id": sched.host_id, "action": sched.action, "run_at": sched.run_at,
        "status": sched.status, "actor": sched.actor, "created_at": sched.created_at,
        "result": sched.result, "host_operation_id": sched.host_operation_id,
        "carp_handover": sched.carp_handover, "phase": sched.phase,
    }


async def _host_or_404(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


@router.get("")
async def list_scheduled(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _host_or_404(db, host_id)
    rows = (
        await db.scalars(
            select(ScheduledMaintenance)
            .where(ScheduledMaintenance.host_id == host_id)
            .order_by(ScheduledMaintenance.created_at.desc())
            .limit(50)
        )
    ).all()
    return [_read(r) for r in rows]


@router.post("")
async def create_scheduled(host_id: uuid.UUID, payload: Schedule, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await _host_or_404(db, host_id)
    if not host.enabled:
        raise api_error(409, ErrorCode.HOST_IST_DEAKTIVIERT)
    now = datetime.now(timezone.utc)
    run_at = payload.run_at if payload.run_at.tzinfo else payload.run_at.replace(tzinfo=timezone.utc)
    if run_at <= now:
        raise api_error(422, ErrorCode.ZEITPUNKT_MUSS_IN_DER_ZUKUNFT_LIEGEN)
    if payload.carp_handover:
        if not host.cluster_name or len(await peers(db, host)) < 2:
            raise api_error(409, ErrorCode.CARP_UEBERGABE_BRAUCHT_EINEN_CLUSTER)
    elif host.cluster_name:
        # Without a handover, fire time refuses every cluster node that is not the CARP BACKUP, so a
        # plan for the current MASTER can only ever end as "failed" at 3 a.m. Say so now.
        # Only a *known* MASTER is rejected here: an unreachable node or an unclear role
        # is still judged by the full check when the plan fires.
        statuses = await get_statuses(await peers(db, host))
        if statuses.get(host.id, {}).get("carp_status") == "MASTER":
            raise api_error(409, ErrorCode.GEPLANTE_WARTUNG_NUR_AUF_DEM_CARP_BACKUP_MOEGLICH)
    sched = ScheduledMaintenance(
        host_id=host.id, host_name=host.name, action=payload.action, run_at=run_at,
        actor=request.state.user.username, created_at=now, carp_handover=payload.carp_handover,
    )
    db.add(sched)
    await db.commit()
    return _read(sched)


@router.delete("/{schedule_id}")
async def cancel_scheduled(host_id: uuid.UUID, schedule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    await _host_or_404(db, host_id)
    sched = await db.get(ScheduledMaintenance, schedule_id)
    if not sched or sched.host_id != host_id:
        raise api_error(404, ErrorCode.GEPLANTE_WARTUNG_NICHT_GEFUNDEN)
    if sched.status != "pending":
        raise api_error(409, ErrorCode.NUR_AUSSTEHENDE_GEPLANTE_WARTUNGEN_KOENNEN_ABGEBROCHEN_WERDEN)
    sched.status = "cancelled"
    await db.commit()
    return _read(sched)
