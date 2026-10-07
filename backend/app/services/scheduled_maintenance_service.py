"""Firing of due one-shot scheduled maintenance actions — called by
main.py's `_scheduled_maintenance_poll_loop`. Mirrors
routers/maintenance.py's `execute()` step for step (same blocking-reason
check, same "consume/mark starting before the remote call" ordering, same
action dispatch table) rather than duplicating that logic differently,
since this is the exact same remote action, just fired by a timer instead
of a click.

v1 scope decision: a blocked or failed scheduled fire is logged and
recorded on the ScheduledMaintenance row's own `result` field (visible in
the UI) but is NOT wired into the NotificationSettings webhook/email
system — that system is built around persistent NotificationProblem rows
with their own admin-configurable toggles, and adding a new problem kind
for this is a bigger change than this feature's own scope. Flagged here,
not silently dropped, in case it's wanted later.
"""
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.host_operation import HostOperation
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services import audit_service, opnsense_client as client
from app.services.host_maintenance_service import ensure_no_maintenance, inspect_host

logger = logging.getLogger(__name__)

_ACTION_FN = {"reboot": client.reboot_host, "update": client.update_firmware, "major_update": client.upgrade_firmware}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def fire_due(db: AsyncSession) -> dict:
    due = list(
        (
            await db.scalars(
                select(ScheduledMaintenance).where(
                    ScheduledMaintenance.status == "pending", ScheduledMaintenance.run_at <= _now()
                )
            )
        ).all()
    )
    fired = 0
    blocked = 0
    for sched in due:
        try:
            await _fire_one(db, sched)
        except Exception:
            logger.exception("Geplante Wartung %s fehlgeschlagen", sched.id)
            sched.status = "failed"
            sched.result = {"error": "Unerwarteter Fehler beim Ausführen"}
            await db.commit()
        if sched.status == "failed":
            blocked += 1
        else:
            fired += 1
    return {"due": len(due), "fired": fired, "blocked_or_failed": blocked}


async def _fire_one(db: AsyncSession, sched: ScheduledMaintenance) -> None:
    host = await db.get(OpnsenseHost, sched.host_id)
    if not host or not host.enabled:
        sched.status = "failed"
        sched.result = {"error": "Host nicht gefunden oder deaktiviert"}
        await db.commit()
        logger.warning("Geplante Wartung %s: Host nicht verfügbar", sched.id)
        return
    try:
        await ensure_no_maintenance(db, [host])
    except HTTPException as exc:
        sched.status = "failed"
        sched.result = {"error": str(exc.detail)}
        await db.commit()
        logger.warning("Geplante Wartung %s blockiert: %s", sched.id, exc.detail)
        return
    try:
        state = await inspect_host(db, host)
    except client.OpnsenseAPIError as exc:
        sched.status = "failed"
        sched.result = {"error": f"Host nicht erreichbar: {exc}"}
        await db.commit()
        logger.warning("Geplante Wartung %s: Host nicht erreichbar", sched.id)
        return
    if state["actions"][sched.action]:
        sched.status = "failed"
        sched.result = {"error": " ".join(state["actions"][sched.action])}
        await db.commit()
        logger.warning("Geplante Wartung %s blockiert: %s", sched.id, sched.result["error"])
        return

    op = HostOperation(
        host_id=host.id, host_name=host.name, actor=sched.actor, action=sched.action,
        expires_at=_now(), fingerprint="", snapshot=state, status="starting", started_at=_now(),
    )
    db.add(op)
    sched.status = "running"
    await db.commit()
    await audit_service.persist(
        kind="host", action=sched.action, status="requested",
        host_id=str(host.id), host_name=host.name, entity_id=str(op.id),
        details={"scheduled": True, "schedule_id": str(sched.id), "snapshot": state},
    )
    try:
        result = await _ACTION_FN[sched.action](host)
        op.status = "submitted"
        op.message = "Geplant übergeben. Abschluss und Erreichbarkeit prüfen; anschließend Wartung abschließen."
        sched.status = "succeeded"
        sched.result = {"response": result}
    except client.OpnsenseAPIError as exc:
        op.status = "unknown"
        op.message = "Ausgang unklar: " + str(exc)[:800] + ". Nicht automatisch wiederholen."
        sched.status = "failed"
        sched.result = {"error": str(exc)[:800]}
        result = {}
    sched.host_operation_id = op.id
    await db.commit()
    await audit_service.persist(
        kind="host", action=sched.action, status=op.status,
        host_id=str(host.id), host_name=host.name, entity_id=str(op.id),
        message=op.message, details={"scheduled": True, "schedule_id": str(sched.id), "response": result},
    )
