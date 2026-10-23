"""Firing of due one-shot scheduled maintenance actions — called by
main.py's `_scheduled_maintenance_poll_loop`. Mirrors
routers/maintenance.py's `execute()` step for step (same blocking-reason
check, same "consume/mark starting before the remote call" ordering, same
action dispatch table) rather than duplicating that logic differently,
since this is the exact same remote action, just fired by a timer instead
of a click.

Updates take a fresh config backup right before firing (a plan for the day after tomorrow
would otherwise always trip over the 24h freshness rule).

v1 scope decision: a blocked or failed scheduled fire is logged and
recorded on the ScheduledMaintenance row's own `result` field (visible in
the UI) but is NOT wired into the NotificationSettings webhook/email
system — that system is built around persistent NotificationProblem rows
with their own admin-configurable toggles, and adding a new problem kind
for this is a bigger change than this feature's own scope. Flagged here,
not silently dropped, in case it's wanted later. (Later: the failure is reported by
notification_checks.check_scheduled_maintenance_failed since 1.17.)

auto_verify (1.25, set for every rollout step): instead of leaving the operation "submitted"
for a human, the run waits for the job (phase job_wait) and then checks the result itself
(phase verify: firmware idle for a while, a reboot really happened when one was due, version
readable). Only then the operation is closed as "acknowledged"; anything unclear ends as
"unknown" (stays locked, notification). Without auto_verify nothing changes.

Concurrency: due rows and running rows are each taken with SELECT ... FOR UPDATE SKIP LOCKED,
one row per transaction, so a second backend (briefly possible during a web update) can never
fire or step the same row at the same time.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.host_operation import HostOperation
from app.models.scheduled_maintenance import ScheduledMaintenance
from contextlib import contextmanager

from app.services import audit_service, backup_store, opnsense_client as client, snapshot_service
from app.services.host_maintenance_service import ACTIVE, ensure_no_maintenance, inspect_carp_switch, inspect_host, peers
from app.services.host_status_service import get_statuses

logger = logging.getLogger(__name__)

_ACTION_FN = {"reboot": client.reboot_host, "update": client.update_firmware, "major_update": client.upgrade_firmware}
# Actions that need a config backup no older than 24h (see host_maintenance_service.inspect_host).
# Unattended, nobody can take it by hand beforehand, so it is taken right before firing.
_BACKUP_BEFORE = {"update", "major_update"}

# ── CARP handover run (opt-in per plan) ─────────────────────────────────────
# MASTER -> maintenance mode -> partner must become MASTER -> action -> wait until the node is
# back -> leave maintenance mode. One step per poll tick, state in the schedule row.
# Rules that keep it safe:
#  * the CARP toggle is a toggle: it is only ever sent against freshly read state and never
#    replayed after an unclear answer (see routers/maintenance.py carp_execute);
#  * until the action was submitted, any problem restores normal operation (nothing happened yet);
#  * after that, any problem leaves the node demoted — the partner keeps carrying traffic, which is
#    the safe side — and the run ends as failed (=> notification) with an "unknown" operation.
HANDOVER_TIMEOUT = timedelta(minutes=3)
LEAVE_TIMEOUT = timedelta(minutes=5)
JOB_GRACE = timedelta(minutes=5)  # no sign of a job after this long: treat a package update as already done
JOB_TIMEOUT = {"reboot": timedelta(minutes=20), "update": timedelta(minutes=60), "major_update": timedelta(minutes=150)}
# auto_verify: the firmware must report "ready" for this long in a row (a reboot at the end of an update
# can follow a short "ready" moment), and the whole check gives up after VERIFY_TIMEOUT.
VERIFY_SETTLE = timedelta(seconds=60)
VERIFY_TIMEOUT = timedelta(minutes=20)
# A rollout step is created with run_at=now and fires on the next poll. Found much later (OPNexus was down,
# e.g. during an update), it no longer belongs to the decision that created it: the window may have closed
# and the run may have been paused meanwhile. It is then not fired; the rollout engine re-queues the host.
ROLLOUT_FIRE_GRACE = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _as_actor(sched: ScheduledMaintenance):
    """Audit events of an unattended run name the person who planned it (not the rollback worker)."""
    token = audit_service.context.set({"actor": f"{sched.actor} (geplant)"[:150], "id": str(sched.id)})
    try:
        yield
    finally:
        audit_service.context.reset(token)


def _rollout_details(sched: ScheduledMaintenance) -> dict:
    rollout = (sched.progress or {}).get("rollout")
    return {"rollout": rollout} if rollout else {}


async def fire_due(db: AsyncSession) -> dict:
    due = fired = blocked = 0
    seen: set = set()
    while True:
        # One row per transaction, locked: a second backend skips it instead of firing it again.
        sched = await db.scalar(
            select(ScheduledMaintenance)
            .where(ScheduledMaintenance.status == "pending", ScheduledMaintenance.run_at <= _now(),
                   ScheduledMaintenance.id.not_in(seen) if seen else true())
            .order_by(ScheduledMaintenance.run_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if sched is None:
            await db.commit()
            break
        seen.add(sched.id)
        due += 1
        with _as_actor(sched):
            try:
                await _fire_one(db, sched)
            except Exception:
                logger.exception("Geplante Wartung %s fehlgeschlagen", sched.id)
                await db.rollback()
                sched = await db.get(ScheduledMaintenance, sched.id, populate_existing=True)
                if sched.status in ("pending", "running") and sched.phase is None:
                    sched.status = "failed"
                    sched.result = {"error": "Unerwarteter Fehler beim Ausführen"}
                await db.commit()
        await db.commit()  # never keep the row lock past this row
        if sched.status == "failed":
            blocked += 1
        else:
            fired += 1
    return {"due": due, "fired": fired, "blocked_or_failed": blocked}


def _reboot_expected(action: str, state: dict) -> bool:
    return action in ("reboot", "major_update") or bool(state.get("reboot_required"))


async def _boot_time(host: OpnsenseHost) -> str | None:
    """systemTime's "boottime" (Diagnostics SystemController::systemTimeAction, read-only, already used by the
    resource sampler). A different value afterwards proves a reboot even if no poll caught the host offline."""
    try:
        return (await client.get_system_time(host)).get("boottime") or None
    except client.OpnsenseAPIError:
        return None


async def _fire_one(db: AsyncSession, sched: ScheduledMaintenance) -> None:
    if sched.origin == "rollout" and _now() - sched.run_at > ROLLOUT_FIRE_GRACE:
        sched.status = "failed"
        sched.result = {"error": "Rollout-Schritt nicht rechtzeitig gestartet, nichts ausgeführt", "not_fired": True}
        await db.commit()
        logger.warning("Geplante Wartung %s: Rollout-Schritt überfällig, nicht ausgeführt", sched.id)
        return
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
    if sched.carp_handover and host.cluster_name:
        try:
            statuses = await get_statuses(await peers(db, host))
        except client.OpnsenseAPIError as exc:
            statuses = {}
            logger.warning("Geplante Wartung %s: Rolle nicht lesbar: %s", sched.id, exc)
        if statuses.get(host.id, {}).get("carp_status") == "MASTER":
            await _start_handover(db, sched, host)
            return
        # Already BACKUP (or role unclear): no handover needed, the normal path below decides.
    try:
        state = await inspect_host(db, host)
    except client.OpnsenseAPIError as exc:
        sched.status = "failed"
        sched.result = {"error": f"Host nicht erreichbar: {exc}"}
        await db.commit()
        logger.warning("Geplante Wartung %s: Host nicht erreichbar", sched.id)
        return
    if sched.action in _BACKUP_BEFORE and not state["blocked"]:
        # Read-only on the firewall. Fail closed: no backup, no update. Taken only after the
        # other blockers passed, so a plan that is going to fail anyway does not pile up backups.
        try:
            backup_store.save_snapshot(host.id, await client.download_backup(host))
            state = await inspect_host(db, host)
        except client.OpnsenseAPIError as exc:
            sched.status = "failed"
            sched.result = {"error": f"Automatisches Backup vor dem Update fehlgeschlagen: {str(exc)[:300]}"}
            await db.commit()
            logger.warning("Geplante Wartung %s: Backup vor dem Update fehlgeschlagen", sched.id)
            return
    if state["actions"][sched.action]:
        sched.status = "failed"
        sched.result = {"error": " ".join(state["actions"][sched.action])}
        await db.commit()
        logger.warning("Geplante Wartung %s blockiert: %s", sched.id, sched.result["error"])
        return
    if sched.action in _BACKUP_BEFORE:
        # Way back (1.32, B5): a boot environment of the running system, after every other check passed.
        # Fail closed like the backup: no rollback point on a ZFS firewall, no update.
        try:
            state["rollback_point"] = await snapshot_service.create_rollback_point(host, state.get("version"))
        except client.OpnsenseAPIError as exc:
            sched.status = "failed"
            sched.result = {"error": f"Rückweg-Punkt (Boot-Environment) vor dem Update nicht angelegt: {str(exc)[:300]}"}
            await db.commit()
            logger.warning("Geplante Wartung %s: Rückweg-Punkt fehlgeschlagen", sched.id)
            return

    boot_before = await _boot_time(host) if sched.auto_verify else None
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
        details={"scheduled": True, "schedule_id": str(sched.id), "snapshot": state, **_rollout_details(sched)},
    )
    try:
        result = await _ACTION_FN[sched.action](host)
        op.status = "submitted"
        if sched.auto_verify:
            # Not done yet: the poll loop waits for the job and checks the result (job_wait -> verify).
            op.message = "Geplant übergeben. Abschluss wird automatisch geprüft."
            sched.phase, sched.phase_since = "job_wait", _now()
            _progress(sched, handover=False, job_started_at=_now().isoformat(), seen_down=False, seen_running=False,
                      version_before=state.get("version"), boot_before=boot_before,
                      reboot_expected=_reboot_expected(sched.action, state))
            sched.result = {"response": result}
        else:
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
        message=op.message, details={"scheduled": True, "schedule_id": str(sched.id), "response": result, **_rollout_details(sched)},
    )


# ── handover run ─────────────────────────────────────────────────────────────

def _progress(sched: ScheduledMaintenance, **changes) -> dict:
    """JSON columns only notice reassignment, so always hand back a new dict."""
    sched.progress = {**(sched.progress or {}), **changes}
    return sched.progress


async def _audit(host, sched, op, action, status, message=None, **details):
    await audit_service.persist(
        kind="host", action=action, status=status, host_id=str(host.id), host_name=host.name,
        entity_id=str(op.id) if op else None, message=message,
        details={"scheduled": True, "schedule_id": str(sched.id), "handover": bool((sched.progress or {}).get("handover")),
                 **_rollout_details(sched), **details},
    )


async def _end(db, sched, host, op, *, ok, message, op_status=None, **result):
    handover = bool((sched.progress or {}).get("handover"))
    sched.status = "succeeded" if ok else "failed"
    sched.phase = None
    sched.result = ({"handover": handover, "auto_verified": bool(sched.auto_verify), **result} if ok
                    else {"error": message[:900], **result})
    if op is not None:
        op.status = op_status or ("acknowledged" if ok else "unknown")
        op.message = message[:1000]
    await db.commit()
    await _audit(host, sched, op, sched.action, "completed" if ok else (op_status or "unknown"), message)
    if not ok:
        logger.warning("Geplante Wartung %s beendet: %s", sched.id, message)


async def _toggle(host, expected: str) -> None:
    """One CARP maintenance toggle; raises OpnsenseAPIError for an unclear or wrong-direction answer."""
    result = await client.toggle_carp_maintenance(host)
    if result.get("action") != expected:
        raise client.OpnsenseAPIError("CARP-Antwort passt nicht zur angeforderten Richtung")


async def _start_handover(db: AsyncSession, sched: ScheduledMaintenance, host: OpnsenseHost) -> None:
    """The node is MASTER now. All preconditions are checked while nothing has been changed yet."""
    def fail(message: str):
        sched.status = "failed"
        sched.result = {"error": message[:900]}
        return db.commit()

    try:
        carp = await inspect_carp_switch(db, host)
        state = await inspect_host(db, host, skip_role_checks=True)
        blocked = list(dict.fromkeys(carp["blocked"] + state["blocked"]))
        if carp["action"] != "carp_enter":  # MASTER but already in maintenance mode: unclear, do not toggle blindly
            blocked.append(f"{host.name} steht bereits im CARP-Wartungsmodus. Rollen prüfen.")
        if not blocked and sched.action in _BACKUP_BEFORE:
            backup_store.save_snapshot(host.id, await client.download_backup(host))
            state = await inspect_host(db, host, skip_role_checks=True)
        blocked = list(dict.fromkeys(blocked + state["actions"][sched.action]))
        if not blocked and sched.action in _BACKUP_BEFORE:
            # Way back (1.32, B5) while nothing has been toggled yet; a failure ends here like a failed backup.
            state["rollback_point"] = await snapshot_service.create_rollback_point(host, state.get("version"))
    except client.OpnsenseAPIError as exc:
        await fail(f"Vorbereitung der CARP-Übergabe fehlgeschlagen, nichts wurde verändert: {str(exc)[:300]}")
        return
    if blocked:
        await fail(" ".join(blocked))
        return

    boot_before = await _boot_time(host) if sched.auto_verify else None
    now = _now()
    op = HostOperation(
        host_id=host.id, host_name=host.name, actor=sched.actor, action=sched.action,
        expires_at=now, fingerprint="", snapshot=state, status="starting", started_at=now,
    )
    db.add(op)
    sched.status = "running"
    _progress(sched, handover=True, boot_before=boot_before, reboot_expected=_reboot_expected(sched.action, state))
    await db.commit()
    sched.host_operation_id = op.id
    await _audit(host, sched, op, "carp_enter", "requested", snapshot=state)
    try:
        await _toggle(host, "enter_maintenance")
    except client.OpnsenseAPIError as exc:
        await _end(db, sched, host, op, ok=False,
                   message=f"CARP-Umschalten mit unklarem Ergebnis: {str(exc)[:300]}. Nicht automatisch wiederholt — "
                           f"Rollen von {host.name} und Partner prüfen.")
        return
    sched.phase, sched.phase_since = "handover_wait", _now()
    _progress(sched, version_before=state.get("version"))
    op.message = "CARP-Übergabe angefordert, warte auf Bestätigung."
    await db.commit()
    await _audit(host, sched, op, "carp_enter", "submitted")


async def advance_running(db: AsyncSession) -> int:
    """One step for every run in progress (handover and/or auto_verify). Returns how many are still running."""
    ids = list((await db.scalars(
        select(ScheduledMaintenance.id).where(ScheduledMaintenance.status == "running", ScheduledMaintenance.phase.is_not(None))
    )).all())
    await db.commit()
    still_running = 0
    for sched_id in ids:
        # Locked for the whole step: a second backend skips the row instead of toggling or submitting twice.
        sched = await db.scalar(
            select(ScheduledMaintenance)
            .where(ScheduledMaintenance.id == sched_id, ScheduledMaintenance.status == "running",
                   ScheduledMaintenance.phase.is_not(None))
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if sched is None:
            await db.commit()
            still_running += 1 if await db.scalar(select(ScheduledMaintenance.status).where(ScheduledMaintenance.id == sched_id)) == "running" else 0
            await db.commit()
            continue
        with _as_actor(sched):
            await _advance_one(db, sched)
        await db.commit()
        if sched.status == "running":
            still_running += 1
    return still_running


async def _advance_one(db: AsyncSession, sched: ScheduledMaintenance) -> None:
    host = await db.get(OpnsenseHost, sched.host_id)
    op = await db.get(HostOperation, sched.host_operation_id) if sched.host_operation_id else None
    try:
        if host is None:
            raise RuntimeError("Host nicht mehr vorhanden")
        if op is not None and op.status not in ACTIVE:
            # A human closed or cancelled the operation: stop here and touch nothing more.
            hint = f" CARP-Rolle von {host.name} prüfen." if (sched.progress or {}).get("handover") else ""
            sched.status, sched.phase = "failed", None
            sched.result = {"error": f"Vorgang von Hand beendet, Ablauf gestoppt.{hint}"}
            await db.commit()
            return
        await _STEPS[sched.phase](db, sched, host, op)
    except Exception as exc:  # a stuck run must not loop forever or take the poll loop down
        logger.exception("Geplante Wartung %s: Schritt %s fehlgeschlagen", sched.id, sched.phase)
        if host is not None:
            hint = (f"{host.name} kann im CARP-Wartungsmodus verblieben sein — Rollen prüfen."
                    if (sched.progress or {}).get("handover") else f"Zustand von {host.name} prüfen.")
            await _end(db, sched, host, op, ok=False,
                       message=f"Unerwarteter Fehler während der geplanten Wartung ({type(exc).__name__}). {hint}")

def _since(sched: ScheduledMaintenance) -> timedelta:
    return _now() - (sched.phase_since or _now())


async def _roles(db, host) -> tuple[dict, bool]:
    """(own status, a partner is online and MASTER)."""
    fleet = await peers(db, host)
    statuses = await get_statuses(fleet)
    partner_master = any(
        statuses.get(h.id, {}).get("status") == "online" and statuses.get(h.id, {}).get("carp_status") == "MASTER"
        for h in fleet if h.id != host.id
    )
    return statuses.get(host.id, {}), partner_master


async def _step_handover_wait(db, sched, host, op):
    me, partner_master = await _roles(db, host)
    if me.get("status") == "online" and me.get("carp_maintenance_mode") and me.get("carp_status") == "BACKUP" and partner_master:
        try:
            await _ACTION_FN[sched.action](host)
        except client.OpnsenseAPIError as exc:
            await _end(db, sched, host, op, ok=False,
                       message=f"Auftrag „{sched.action}“ mit unklarem Ergebnis: {str(exc)[:300]}. Nicht wiederholt. "
                               f"{host.name} bleibt im CARP-Wartungsmodus, der Partner trägt den Verkehr.")
            return
        sched.phase, sched.phase_since = "job_wait", _now()
        _progress(sched, job_started_at=_now().isoformat(), seen_down=False, seen_running=False)
        op.status, op.message = "submitted", "Übergabe bestätigt, Auftrag gestartet."
        await db.commit()
        await _audit(host, sched, op, sched.action, "submitted")
        return
    if _since(sched) < HANDOVER_TIMEOUT:
        return
    # The partner never took over. Nothing has run yet, so put the node back to normal.
    if me.get("status") == "online" and me.get("carp_maintenance_mode"):
        try:
            await _toggle(host, "leave_maintenance")
        except client.OpnsenseAPIError as exc:
            await _end(db, sched, host, op, ok=False,
                       message=f"CARP-Übergabe nicht bestätigt, und der Normalbetrieb konnte nicht wiederhergestellt werden "
                               f"({str(exc)[:200]}). {host.name} prüfen.")
            return
        await _end(db, sched, host, op, ok=False, op_status="cancelled",
                   message="CARP-Übergabe nicht bestätigt (der Partner wurde nicht MASTER). Normalbetrieb wiederhergestellt, nichts aktualisiert.")
    elif me.get("status") == "online":
        await _end(db, sched, host, op, ok=False, op_status="cancelled",
                   message="CARP-Übergabe hat nicht stattgefunden (Wartungsmodus nicht aktiv). Nichts aktualisiert.")
    else:
        await _end(db, sched, host, op, ok=False,
                   message=f"CARP-Übergabe nicht bestätigt und {host.name} nicht erreichbar. Rollen prüfen.")


async def _step_job_wait(db, sched, host, op):
    prog = dict(sched.progress or {})
    started = datetime.fromisoformat(prog["job_started_at"])
    elapsed = _now() - started
    try:
        running = (await client.get_firmware_running(host)).get("status")
    except client.OpnsenseAPIError:
        _progress(sched, seen_down=True)  # rebooting: exactly what a reboot or base update looks like
        running = None
    else:
        if running != "ready":
            _progress(sched, seen_running=True)
    prog = sched.progress
    if running == "ready":
        if sched.action == "reboot":
            done = bool(prog.get("seen_down"))
        else:
            done = bool(prog.get("seen_down") or prog.get("seen_running")) or elapsed >= JOB_GRACE
        if done:
            sched.phase = "verify" if sched.auto_verify else "leave"
            sched.phase_since = _now()
            await db.commit()
            return
    if elapsed >= JOB_TIMEOUT[sched.action]:
        if prog.get("handover"):
            text = (f"Auftrag „{sched.action}“ auf {host.name} nicht rechtzeitig abgeschlossen. {host.name} bleibt im "
                    f"CARP-Wartungsmodus, der Partner trägt den Verkehr. Bitte prüfen und von Hand zurückschalten.")
        else:
            text = f"Auftrag „{sched.action}“ auf {host.name} nicht rechtzeitig abgeschlossen. Bitte {host.name} prüfen."
        await _end(db, sched, host, op, ok=False, message=text)
        return
    await db.commit()


async def _step_verify(db, sched, host, op):
    """auto_verify: is the job really over and the host healthy? Read-only, never repeats the action."""
    prog = dict(sched.progress or {})
    handover = bool(prog.get("handover"))

    async def wait_or_give_up(why: str):
        if _since(sched) >= VERIFY_TIMEOUT:
            tail = (f" {host.name} bleibt im CARP-Wartungsmodus, der Partner trägt den Verkehr." if handover else "")
            await _end(db, sched, host, op, ok=False,
                       message=f"Automatische Prüfung nach „{sched.action}“ nicht bestanden: {why}.{tail} "
                               f"Bitte {host.name} prüfen und die Wartung von Hand abschließen.")
            return
        _progress(sched, waiting_for=why)
        await db.commit()

    try:
        running = (await client.get_firmware_running(host)).get("status")
    except client.OpnsenseAPIError:
        running = None
        _progress(sched, seen_down=True)
    if running != "ready":
        _progress(sched, ready_since=None)
        await wait_or_give_up("Firmware-Auftrag läuft noch oder Host nicht erreichbar")
        return
    if not prog.get("ready_since"):
        _progress(sched, ready_since=_now().isoformat())
        await db.commit()
        return
    if _now() - datetime.fromisoformat(prog["ready_since"]) < VERIFY_SETTLE:
        await db.commit()
        return
    boot_now = await _boot_time(host)
    rebooted = bool(prog.get("seen_down")) or bool(prog.get("boot_before") and boot_now and boot_now != prog["boot_before"])
    if prog.get("reboot_expected") and not rebooted:
        await wait_or_give_up("der erwartete Neustart wurde nicht beobachtet")
        return
    try:
        version = (await client.get_firmware_info(host)).get("product_version")
    except client.OpnsenseAPIError:
        version = None
    online = (await get_statuses([host])).get(host.id, {}).get("status") == "online"
    if not version or not online:
        await wait_or_give_up("Host nicht vollständig erreichbar (Version/Status nicht lesbar)")
        return
    _progress(sched, version_after=version, boot_after=boot_now, rebooted=rebooted, waiting_for=None)
    if handover:
        sched.phase, sched.phase_since = "leave", _now()  # only now take the traffic back
        await db.commit()
        return
    await _end(db, sched, host, op, ok=True,
               message=f"Automatisch geprüft: {host.name} erreichbar, Firmware-Auftrag beendet, Version {version}.",
               version_before=prog.get("version_before"), version_after=version, rebooted=rebooted)


async def _step_leave(db, sched, host, op):
    me, _partner = await _roles(db, host)
    if me.get("status") != "online":
        if _since(sched) >= LEAVE_TIMEOUT:
            await _end(db, sched, host, op, ok=False,
                       message=f"{host.name} ist nach dem Auftrag nicht wieder erreichbar. Der Partner trägt den Verkehr.")
        return
    if me.get("carp_maintenance_mode"):
        try:
            await _toggle(host, "leave_maintenance")
        except client.OpnsenseAPIError as exc:
            await _end(db, sched, host, op, ok=False,
                       message=f"Zurückschalten mit unklarem Ergebnis: {str(exc)[:300]}. Nicht automatisch wiederholt — "
                               f"{host.name} prüfen, ggf. „CARP-Normalbetrieb herstellen“.")
            return
        await _audit(host, sched, op, "carp_leave", "submitted")
    sched.phase, sched.phase_since = "leave_wait", _now()
    await db.commit()


async def _step_leave_wait(db, sched, host, op):
    fleet = await peers(db, host)
    statuses = await get_statuses(fleet)
    me = statuses.get(host.id, {})
    online = [h for h in fleet if statuses.get(h.id, {}).get("status") == "online"]
    masters = [h for h in online if statuses.get(h.id, {}).get("carp_status") == "MASTER"]
    if me.get("status") == "online" and not me.get("carp_maintenance_mode") and len(masters) == 1 and len(online) == len(fleet):
        extra = {}
        try:
            extra["version_after"] = (await client.get_firmware_info(host)).get("product_version")
            extra["reboot_required"] = str((await client.get_firmware_status(host)).get("status_reboot", "0")) == "1"
        except client.OpnsenseAPIError:
            pass
        extra["version_before"] = (sched.progress or {}).get("version_before")
        if sched.auto_verify:
            extra["rebooted"] = bool((sched.progress or {}).get("rebooted"))  # what verify saw before leaving
        text = "Automatisch abgeschlossen: Übergabe, Auftrag und Zurückschalten bestätigt."
        if extra.get("reboot_required"):
            text += " Ein Neustart ist noch erforderlich."
        await _end(db, sched, host, op, ok=True, message=text, **extra)
        return
    if _since(sched) >= LEAVE_TIMEOUT:
        await _end(db, sched, host, op, ok=False,
                   message=f"Normalbetrieb nach dem Zurückschalten nicht bestätigt. Rollen von {host.name} und Partner prüfen.")


_STEPS = {
    "handover_wait": _step_handover_wait,
    "job_wait": _step_job_wait,
    "verify": _step_verify,
    "leave": _step_leave,
    "leave_wait": _step_leave_wait,
}
