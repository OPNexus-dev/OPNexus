"""The rollout run engine (1.25): moves runs forward, one small step per poll tick.

Ground rules (an unattended engine that triggers firmware updates is the riskiest thing in OPNexus):

* It never calls a firmware action itself. Each host step is an ordinary ScheduledMaintenance
  (run_at=now, origin="rollout", auto_verify=True) — locks, backup before the update, CARP handover,
  audit and restart safety all come from that existing path.
* Never twice: a host gets a schedule only while its ``scheduled_maintenance_id`` is empty, written
  in the same transaction as the schedule itself, under the run's row lock.
* Never in parallel: one host at a time per run, and only one active run at all; nodes of a cluster
  go BACKUP first, the MASTER only with the wave's CARP handover, and before every node of an
  already touched cluster the cluster is checked again.
* When in doubt, stop: anything unexpected pauses the run (``pause_reason``) and notifies; nothing
  is retried on its own. A paused run only continues when a human resumes it, and resuming first
  checks the health of everything done so far.
* Every step re-checks that the person responsible for the run may still roll out.
* Hard limits: waves/hosts (rollout_service), soak time, preflight/host timeouts, run age.

Concurrency: a tick (1) does DB-only bookkeeping under the run's row lock, (2) decides with the
lock released (remote reads may take seconds), and (3) applies the decision only if the run's
``revision`` is still the one it decided on — a pause/abort in between, or a second backend, wins.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models.host import OpnsenseHost
from app.models.maintenance_window import MaintenanceWindow
from app.models.rollout import RolloutPlan, RolloutRun, RolloutRunHost, RolloutRunWave
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.models.user import AdminUser
from app.services import audit_service, rollout_health, rollout_service, window_service
from app.services import opnsense_client as client
from app.services.host_maintenance_service import ensure_no_maintenance, peers
from app.services.host_status_service import get_statuses
from app.services.policy_service import policy_lock

logger = logging.getLogger(__name__)

ACTIVE = ("waiting_window", "preflight", "wave_running", "soaking", "gate", "awaiting_approval", "resuming", "paused")
TERMINAL = ("succeeded", "cancelled")
# Statuses in which the poll loop should tick quickly.
BUSY = ("preflight", "wave_running", "gate", "resuming")
HOST_OPEN = ("running", "verifying")

PREFLIGHT_CHECK_DELAY = timedelta(seconds=20)  # after asking for an update check, before reading the result
PREFLIGHT_TIMEOUT = timedelta(minutes=10)
GATE_RECHECK = timedelta(minutes=2)  # a failed gate is looked at once more (slow start after a reboot)
BLOCKED_PATIENCE = timedelta(minutes=15)  # someone else's maintenance in the cluster
HOST_START_TIMEOUT = timedelta(minutes=10)  # the schedule never fired
HOST_MAX_DURATION = timedelta(hours=4)  # backstop above the schedule's own timeouts
MAX_RUN_AGE = timedelta(days=14)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _note(code: str, **params) -> dict:
    return {"code": code, "params": params}


# ── small helpers on a loaded run ─────────────────────────────────────────────

def wave_row(run: RolloutRun, index: int) -> RolloutRunWave:
    return next(w for w in run.waves if w.index == index)


def wave_spec(run: RolloutRun, index: int) -> dict:
    return run.plan_snapshot["waves"][index]


def hosts_of(run: RolloutRun, index: int) -> list[RolloutRunHost]:
    return sorted((h for h in run.hosts if h.wave_index == index), key=lambda h: (h.order, h.host_name))


def _named(run: RolloutRun, failed: list[str]) -> list[str]:
    """"<host-id>:<check>" -> "<host name>:<check>" for pause details people read."""
    names = {str(h.host_id): h.host_name for h in run.hosts if h.host_id}
    return [f"{names.get(item.split(':', 1)[0], item.split(':', 1)[0])}:{item.split(':', 1)[1]}" for item in failed]


def _progress(run: RolloutRun, **changes) -> None:
    run.progress = {**(run.progress or {}), **changes}


async def _audit(run: RolloutRun, action: str, status: str, message: str | None = None, **details) -> None:
    token = audit_service.context.set({"actor": f"{run.started_by} (Rollout)"[:150], "id": str(run.id)})
    try:
        await audit_service.persist(kind="rollout", action=action, status=status, entity_id=str(run.id),
                                    entity_name=run.plan_name, message=message,
                                    details={"wave": run.current_wave, **details})
    finally:
        audit_service.context.reset(token)


def _requeue(host: RolloutRunHost, code: str) -> None:
    """Back to the queue: only for a host whose schedule provably never fired (cancelled under its row lock,
    or refused by fire_due as overdue). Clearing the schedule id lets a resumed run plan it once more."""
    host.status, host.scheduled_maintenance_id, host.started_at, host.detail = "queued", None, None, _note(code)


async def requeue_if_pending(db: AsyncSession, host: RolloutRunHost) -> bool:
    """Cancel a host's schedule that has not fired yet and put the host back in the queue. Locked like the
    router's abort: fire_due holds the row while firing, and a running action is never touched."""
    if host.status not in HOST_OPEN or host.scheduled_maintenance_id is None:
        return False
    sched = await db.scalar(select(ScheduledMaintenance).where(ScheduledMaintenance.id == host.scheduled_maintenance_id)
                            .with_for_update().execution_options(populate_existing=True))
    if sched is None or sched.status != "pending":
        return False
    sched.status = "cancelled"
    sched.result = {"error": "Rollout angehalten, nicht ausgeführt", "not_fired": True}
    _requeue(host, "not_fired")
    return True


def pause(run: RolloutRun, reason: str, **detail) -> None:
    if run.status != "paused":
        run.paused_from = run.status
    run.status = "paused"
    run.pause_reason = reason
    run.pause_detail = detail
    _progress(run, paused_at=_iso(_now()))


async def may_roll_out(db: AsyncSession, username: str, user_id=None) -> bool:
    """Does this account (still) have the right to roll out? Same rule as AuthMiddleware's is_rollout.
    With the stable account id (1.29) the id decides: a deleted account that was re-created under the same name is
    someone else. Rows from before the id existed (and still without one) are matched by name as before."""
    if user_id is not None:
        user = await db.get(AdminUser, user_id)
    else:
        user = await db.scalar(select(AdminUser).where(AdminUser.username == username))
    if user is None:
        return False
    if user.role == "admin":
        return True
    if user.role == "operator":
        from app.routers import governance
        return (await governance.get_settings(db)).operators_may_deploy
    return False


async def _window(db: AsyncSession, run: RolloutRun) -> tuple[bool, MaintenanceWindow | None]:
    """(window configured, window). Configured but gone means: stop, never 'no time limit'."""
    window_id = run.plan_snapshot.get("window_id")
    if not window_id:
        return False, None
    return True, await db.get(MaintenanceWindow, window_id)


async def _may_start_now(db: AsyncSession, run: RolloutRun) -> str | None:
    """None if new host work may start now, else the reason ('window_closed' / 'window_missing')."""
    configured, window = await _window(db, run)
    if not configured:
        return None
    if window is None:
        return "window_missing"
    return None if window_service.startable(window, _now()) else "window_closed"


async def _lock(db: AsyncSession, run_id, revision: int | None = None) -> RolloutRun | None:
    stmt = select(RolloutRun).where(RolloutRun.id == run_id)
    if revision is not None:
        stmt = stmt.where(RolloutRun.revision == revision)
    return await db.scalar(stmt.with_for_update().execution_options(populate_existing=True))


# ── creating a run ────────────────────────────────────────────────────────────

async def active_run(db: AsyncSession) -> RolloutRun | None:
    return await db.scalar(select(RolloutRun).where(RolloutRun.status.in_(ACTIVE)).order_by(RolloutRun.created_at).limit(1))


async def create_run(db: AsyncSession, plan: RolloutPlan, resolved: dict, *, started_by: str, trigger: str,
                     occurrence: datetime | None = None, started_by_id=None) -> RolloutRun:
    """Freeze the plan and its resolved hosts. The caller holds policy_lock and has checked the token."""
    now = _now()
    snapshot = {
        "plan_id": str(plan.id), "name": plan.name, "action": plan.action,
        "window_id": str(plan.window_id) if plan.window_id else None,
        "gate_checks": rollout_service.effective_checks(plan.gate_checks), "compliance_mode": plan.compliance_mode,
        "waves": [{k: w[k] for k in ("label", "soak_minutes", "carp_handover", "require_approval", "groups")} for w in resolved["waves"]],
    }
    run = RolloutRun(plan_id=plan.id, plan_name=plan.name, plan_snapshot=snapshot, action=plan.action, status="preflight",
                     current_wave=0, started_by=started_by, started_by_id=started_by_id, trigger=trigger, window_occurrence=occurrence,
                     progress={}, created_at=now, step_at=now, revision=0)
    run.waves = [RolloutRunWave(index=w["index"], label=w["label"], status="preflight" if w["index"] == 0 else "pending")
                 for w in resolved["waves"]]
    run.hosts = [RolloutRunHost(wave_index=w["index"], host_id=uuid.UUID(h["id"]), host_name=h["name"], cluster_name=h["cluster_name"],
                                order=i, status="queued")
                 for w in resolved["waves"] for i, h in enumerate(w["hosts"])]
    db.add(run)
    await db.flush()
    return run


# ── the tick ──────────────────────────────────────────────────────────────────

async def advance_runs(db: AsyncSession) -> int:
    """One step for every run. Returns how many runs want a quick next tick."""
    try:
        await auto_start(db)
    except Exception:
        logger.exception("Rollout: automatischer Start fehlgeschlagen")
        await db.rollback()
    ids = list((await db.scalars(select(RolloutRun.id).where(
        (RolloutRun.status.in_(ACTIVE)) | RolloutRun.hosts.any(RolloutRunHost.status.in_(HOST_OPEN))))).all())
    await db.commit()
    busy = 0
    for run_id in ids:
        try:
            await tick(run_id)
        except Exception as exc:
            logger.exception("Rollout %s: Schritt fehlgeschlagen", run_id)
            async with SessionLocal() as s:
                run = await _lock(s, run_id)
                if run is not None and run.status in ACTIVE and run.status != "paused":
                    pause(run, "internal_error", error=type(exc).__name__)
                    run.revision += 1
                    await s.commit()
                    await _audit(run, "pause", "paused", "Interner Fehler: Rollout angehalten", reason="internal_error")
        async with SessionLocal() as s:
            status = await s.scalar(select(RolloutRun.status).where(RolloutRun.id == run_id))
        busy += status in BUSY
    return busy


async def tick(run_id) -> None:
    async with SessionLocal() as db:
        # (1) bookkeeping under the lock: what did the schedules do?
        run = await _lock(db, run_id)
        if run is None:
            return
        changed = await _sync_hosts(db, run)
        if (run.status in ACTIVE and run.status != "paused" and _now() - run.created_at > MAX_RUN_AGE
                and not any(h.status in HOST_OPEN for h in run.hosts)):
            pause(run, "run_too_old", days=MAX_RUN_AGE.days)
            changed = True
        if changed:
            run.revision += 1
        await db.commit()
        if changed and run.status == "paused" and run.pause_reason in ("host_failed", "host_timeout", "host_not_started", "run_too_old"):
            await _audit(run, "pause", "paused", reason=run.pause_reason, detail=run.pause_detail)
        if run.status not in ACTIVE or run.status in ("paused", "awaiting_approval"):
            return
        revision = run.revision
        # (2) decide, without holding the lock
        decision = await _HANDLERS[run.status](db, run)
        await db.commit()
        if decision is None:
            return
        # (3) apply only on the revision it was decided on
        run = await _lock(db, run_id, revision)
        if run is None:
            await db.commit()
            return
        events = await decision(db, run)
        run.revision += 1
        run.step_at = _now()
        await db.commit()
        for event in events or []:
            await _audit(run, *event[:2], **(event[2] if len(event) > 2 else {}))


Decision = Callable[[AsyncSession, RolloutRun], Awaitable[list | None]]


async def _sync_hosts(db: AsyncSession, run: RolloutRun) -> bool:
    """Carry the outcome of each host's schedule over to the run. Returns True if anything changed."""
    changed = False
    now = _now()
    for host in run.hosts:
        if host.status not in HOST_OPEN:
            continue
        sched = await db.get(ScheduledMaintenance, host.scheduled_maintenance_id, populate_existing=True) \
            if host.scheduled_maintenance_id else None
        problem = None
        if sched is None:
            host.status, host.detail, host.finished_at = "failed", _note("schedule_missing"), now
            problem = ("host_failed", {"host": host.host_name, "error": "schedule_missing"})
        elif sched.status == "pending":
            if host.started_at and now - host.started_at > HOST_START_TIMEOUT:
                # Never leave it pending behind a paused run: it would fire whenever the poll loop comes back.
                if await requeue_if_pending(db, host):
                    problem = ("host_not_started", {"host": host.host_name})
                    changed = True
        elif (sched.result or {}).get("not_fired"):
            _requeue(host, "not_fired")  # fire_due refused it as overdue: nothing happened on the firewall
            problem = ("host_not_started", {"host": host.host_name})
            changed = True
        elif sched.status == "running":
            want = "verifying" if sched.phase == "verify" else "running"
            if host.status != want:
                host.status = want
                changed = True
            if host.started_at and now - host.started_at > HOST_MAX_DURATION:
                problem = ("host_timeout", {"host": host.host_name})
        elif sched.status == "succeeded":
            result = sched.result or {}
            host.status, host.finished_at = "succeeded", now
            host.version_after = result.get("version_after")
            host.detail = _note("verified", version=result.get("version_after"), rebooted=bool(result.get("rebooted")))
            changed = True
        else:  # failed / cancelled
            error = (sched.result or {}).get("error") or sched.status
            host.status, host.finished_at = "failed", now
            host.detail = _note("host_failed", error=error[:300])
            problem = ("host_failed", {"host": host.host_name, "error": error[:300]})
        if problem and run.status in ACTIVE and run.status != "paused":
            pause(run, problem[0], **problem[1])
            changed = True
        elif problem and host.status == "failed":
            changed = True
    return changed


# ── handlers: one per run status; each returns an apply-function or None ──────

async def _actor_problem(db: AsyncSession, run: RolloutRun) -> Decision | None:
    if await may_roll_out(db, run.started_by, run.started_by_id):
        return None

    async def apply(db, run):
        pause(run, "actor_invalid", user=run.started_by)
        return [("pause", "paused", {"reason": "actor_invalid"})]
    return apply


def _to_waiting(reason: str) -> Decision:
    async def apply(db, run):
        if reason == "window_missing":
            pause(run, "window_missing")
            return [("pause", "paused", {"reason": reason})]
        run.paused_from = run.status
        run.status = "waiting_window"
        return [("waiting_window", "waiting")]
    return apply


async def _handle_waiting_window(db: AsyncSession, run: RolloutRun) -> Decision | None:
    reason = await _may_start_now(db, run)
    if reason == "window_missing":
        return _to_waiting(reason)
    if reason:
        return None

    async def apply(db, run):
        _progress(run, resume_recheck_at=None, resume_attempts=0)
        run.status = "resuming"  # the gate of everything done so far is checked again first
        return [("window_open", "resuming")]
    return apply


async def _handle_resuming(db: AsyncSession, run: RolloutRun) -> Decision | None:
    recheck = _ts((run.progress or {}).get("resume_recheck_at"))
    if recheck and _now() < recheck:
        return None
    done = [h for h in run.hosts if h.status == "succeeded" and h.host_id]
    gate = {"hosts": {}, "verdict": "ok"}
    if done:
        hosts = [h for h in [await db.get(OpnsenseHost, d.host_id) for d in done] if h is not None]
        baselines = {}
        for w in run.waves:
            baselines.update((w.baseline or {}))
        gate = await rollout_health.evaluate(db, hosts, baselines, target_version=run.target_version,
                                             checks=run.plan_snapshot.get("gate_checks"),
                                             compliance_mode=run.plan_snapshot.get("compliance_mode", "warn"))

    async def apply(db, run):
        if gate["verdict"] == "fail":
            attempts = (run.progress or {}).get("resume_attempts", 0) + 1
            _progress(run, resume_gate=gate, resume_attempts=attempts)
            if attempts < 2:
                _progress(run, resume_recheck_at=_iso(_now() + GATE_RECHECK))
                return [("resume_gate", "recheck", {"failed": rollout_health.failed_checks(gate)})]
            run.paused_from = run.paused_from or "preflight"
            _progress(run, resume_recheck_at=None)
            run.status = "paused"
            run.pause_reason, run.pause_detail = "gate_failed", {"failed": _named(run, rollout_health.failed_checks(gate)), "on_resume": True}
            return [("resume_gate", "failed", {"failed": rollout_health.failed_checks(gate)})]
        _progress(run, resume_gate=gate, resume_recheck_at=None, resume_attempts=0)
        target = run.paused_from if run.paused_from in ("preflight", "wave_running", "soaking", "gate") else "preflight"
        if target == "gate":
            wave_row(run, run.current_wave).gate_attempts = 0
            _progress(run, gate_recheck_at=None)
        if target == "preflight":
            _progress(run, preflight_started=None, preflight_requested_at=None)
        run.status, run.paused_from, run.pause_reason, run.pause_detail = target, None, None, None
        return [("resume_gate", "passed", {"continue_with": target})]
    return apply


def target_of(status: dict, action: str) -> str | None:
    """The version a firmware action will install, from core/firmware/status (read after a fresh check).

    Live finding (test-fw-3, 26.7.1_1, 2026-10-21): ``product.product_latest`` said 26.7.2 while the
    ``opnsense`` core package was offered as 26.7.7 — and the package is what gets installed. So the
    core package's new version comes first; product_latest is only a fallback."""
    if action == "major_update":
        return status.get("upgrade_major_version") or None
    packages = status.get("all_packages") if isinstance(status.get("all_packages"), dict) else {}
    core = packages.get("opnsense") if isinstance(packages.get("opnsense"), dict) else None
    if core and core.get("new") and core.get("new") != "N/A":
        return str(core["new"])
    for row in status.get("upgrade_packages") or []:
        if isinstance(row, dict) and row.get("name") == "opnsense" and row.get("new_version"):
            return str(row["new_version"])
    product = status.get("product") if isinstance(status.get("product"), dict) else {}
    return product.get("product_latest") or status.get("product_latest") or None


async def _handle_preflight(db: AsyncSession, run: RolloutRun) -> Decision | None:
    if problem := await _actor_problem(db, run):
        return problem
    reason = await _may_start_now(db, run)
    if reason:
        return _to_waiting(reason)
    index = run.current_wave
    prog = run.progress or {}
    started = _ts(prog.get("preflight_started")) or _now()
    requested = _ts(prog.get("preflight_requested_at"))
    candidates = [h for h in hosts_of(run, index) if h.status in ("queued", "blocked")]
    now = _now()

    # Which hosts are usable at all?
    usable: dict = {}
    skips: dict = {}
    for rh in candidates:
        host = await db.get(OpnsenseHost, rh.host_id) if rh.host_id else None
        if host is None:
            skips[rh.id] = _note("host_deleted")
        elif not host.enabled:
            skips[rh.id] = _note("host_disabled")
        else:
            usable[rh.id] = host

    if requested is None:
        # Step 1: ask every usable host to look for updates (a firmware *check*, changes nothing installed).
        asked = []
        for rh_id, host in usable.items():
            try:
                await ensure_no_maintenance(db, [host])
                if (await client.get_firmware_running(host)).get("status") == "ready":
                    await client.check_firmware_updates(host)
                    asked.append(str(rh_id))
            except (client.OpnsenseAPIError, HTTPException):
                pass  # judged in step 2 (and blocked after the timeout)

        async def apply(db, run):
            _progress(run, preflight_started=_iso(started), preflight_requested_at=_iso(_now()), preflight_asked=asked)
            w = wave_row(run, index)
            w.status = "preflight"
            w.started_at = w.started_at or _now()
            return [("preflight", "started", {"hosts": len(usable)})]
        return apply
    if now - requested < PREFLIGHT_CHECK_DELAY:
        return None

    # Step 2: read what each host offers.
    offers: dict = {}
    waiting: dict = {}
    for rh_id, host in usable.items():
        try:
            await ensure_no_maintenance(db, [host])
        except HTTPException:
            waiting[rh_id] = _note("locked")
            continue
        try:
            running = (await client.get_firmware_running(host)).get("status")
            if running != "ready":
                waiting[rh_id] = _note("firmware_busy")
                continue
            status = await client.get_firmware_status(host)
        except client.OpnsenseAPIError:
            waiting[rh_id] = _note("offline")
            continue
        fw = status.get("status")
        wanted = "upgrade" if run.action == "major_update" else "update"
        if fw == wanted:
            offers[rh_id] = target_of(status, run.action)
        elif fw in ("none", "update", "upgrade"):
            skips[rh_id] = _note("no_update_offered", status=fw)
        else:
            waiting[rh_id] = _note("firmware_status", status=str(fw)[:40])

    if waiting:
        if now - started < PREFLIGHT_TIMEOUT:
            return None

        async def apply_blocked(db, run):
            names = []
            for rh in hosts_of(run, index):
                if rh.id in waiting:
                    rh.status, rh.detail = "blocked", waiting[rh.id]
                    names.append(rh.host_name)
            pause(run, "preflight_blocked", hosts=", ".join(names))
            _progress(run, preflight_started=None, preflight_requested_at=None)
            return [("pause", "paused", {"reason": "preflight_blocked", "hosts": names})]
        return apply_blocked

    targets = {t for t in offers.values()}
    expected = run.target_version
    mismatch = None
    if None in targets:
        mismatch = {"offered": "?"}
    elif expected is None and len(targets) > 1:
        mismatch = {"offered": ", ".join(sorted(targets))}
    elif expected is not None and targets - {expected}:
        mismatch = {"expected": expected, "offered": ", ".join(sorted(targets - {expected}))}
    if mismatch:
        async def apply_mismatch(db, run):
            # OPNsense always installs the newest offered version; a later ring must not get one the earlier ring never ran.
            pause(run, "target_version_changed", **mismatch)
            _progress(run, preflight_started=None, preflight_requested_at=None)
            return [("pause", "paused", {"reason": "target_version_changed", **mismatch})]
        return apply_mismatch

    # Baselines and order (BACKUP before MASTER inside a cluster).
    baselines = {}
    versions = {}
    for rh_id in offers:
        host = usable[rh_id]
        baselines[str(host.id)] = await rollout_health.baseline(db, host)
        versions[rh_id] = baselines[str(host.id)].get("version")
    roles = await get_statuses([usable[i] for i in offers])
    rank = {"BACKUP": 0, None: 1, "MASTER": 2}

    def order_key(rh_id):
        host = usable[rh_id]
        role = roles.get(host.id, {}).get("carp_status") if host.cluster_name else None
        return (1 if host.cluster_name else 0, host.cluster_name or "", rank.get(role, 1), host.name)
    ordered = sorted(offers, key=order_key)
    target = next(iter(targets)) if targets else expected

    async def apply_ready(db, run):
        for rh in hosts_of(run, index):
            if rh.id in skips:
                rh.status, rh.detail = "skipped", skips[rh.id]
            elif rh.id in offers:
                rh.status, rh.detail = "queued", None
                rh.order = ordered.index(rh.id)
                rh.version_before = versions.get(rh.id)
        w = wave_row(run, index)
        w.baseline = baselines
        if run.target_version is None and target:
            run.target_version = target
        _progress(run, preflight_started=None, preflight_requested_at=None, blocked_since=None, cluster_recheck_at=None)
        if not offers:
            w.status, w.finished_at = "skipped", _now()
            events = [("wave", "skipped", {"wave": index})]
            events += _next_wave(run)
            return events
        w.status = "running"
        run.status = "wave_running"
        return [("wave", "started", {"wave": index, "target_version": run.target_version,
                                     "hosts": [usable[i].name for i in ordered], "skipped": len(skips)})]
    return apply_ready


def _next_wave(run: RolloutRun) -> list:
    index = run.current_wave + 1
    if index >= len(run.waves):
        run.status, run.finished_at = "succeeded", _now()
        return [("finish", "succeeded")]
    run.current_wave = index
    w = wave_row(run, index)
    if wave_spec(run, index).get("require_approval"):
        w.status = run.status = "awaiting_approval"
        return [("wave", "awaiting_approval", {"wave": index})]
    w.status, run.status = "preflight", "preflight"
    return []


async def _handle_wave_running(db: AsyncSession, run: RolloutRun) -> Decision | None:
    if any(h.status in HOST_OPEN for h in run.hosts):
        return None  # strictly one host at a time
    index = run.current_wave
    queued = [h for h in hosts_of(run, index) if h.status == "queued"]
    if not queued:
        soak = int(wave_spec(run, index).get("soak_minutes") or 0)

        async def apply_soak(db, run):
            w = wave_row(run, index)
            w.status, w.soak_until = "soaking", _now() + timedelta(minutes=soak)
            run.status = "soaking"
            return [("wave", "soaking", {"wave": index, "minutes": soak})]
        return apply_soak
    if problem := await _actor_problem(db, run):
        return problem
    reason = await _may_start_now(db, run)
    if reason:
        return _to_waiting(reason)
    nxt = queued[0]
    host = await db.get(OpnsenseHost, nxt.host_id) if nxt.host_id else None
    if host is None or not host.enabled:
        async def apply_skip(db, run):
            rh = next(h for h in run.hosts if h.id == nxt.id)
            rh.status, rh.detail = "skipped", _note("host_deleted" if host is None else "host_disabled")
            return [("host", "skipped", {"host": nxt.host_name})]
        return apply_skip
    spec = wave_spec(run, index)
    prog = run.progress or {}

    if host.cluster_name:
        fleet = await peers(db, host)
        statuses = await get_statuses(fleet)
        done_ids = {h.host_id for h in run.hosts if h.status == "succeeded"}
        done_peers = [p for p in fleet if p.id in done_ids and p.id != host.id]
        baselines = {}
        for w in run.waves:
            baselines.update(w.baseline or {})
        # The cluster as a whole must be healthy before a node is touched; nodes done earlier in this run
        # must pass their checks again (BACKUP updated -> now it may carry the traffic for the MASTER).
        gate = await rollout_health.evaluate(
            db, done_peers + [host], baselines, target_version=run.target_version,
            checks={**(run.plan_snapshot.get("gate_checks") or {}), "carp": True, "compliance": False})
        # The node itself is not updated yet: only its reachability, the cluster and the locks count for it.
        failed = [f"{hid}:{c}" for hid, row in gate["hosts"].items() for c, r in row.items()
                  if r["result"] == "fail" and not (hid == str(host.id) and c in ("version", "gateways", "services"))]
        if failed:
            recheck = _ts(prog.get("cluster_recheck_at"))
            if recheck is None:
                async def apply_recheck(db, run):
                    _progress(run, cluster_recheck_at=_iso(_now() + GATE_RECHECK), cluster_gate=gate)
                    return [("cluster_gate", "recheck", {"host": host.name, "failed": failed})]
                return apply_recheck
            if _now() < recheck:
                return None

            async def apply_cluster_fail(db, run):
                pause(run, "cluster_gate_failed", cluster=host.cluster_name, host=host.name, failed=_named(run, failed))
                _progress(run, cluster_recheck_at=None, cluster_gate=gate)
                return [("cluster_gate", "failed", {"host": host.name, "failed": failed})]
            return apply_cluster_fail
        if statuses.get(host.id, {}).get("carp_status") == "MASTER" and not spec.get("carp_handover"):
            async def apply_master(db, run):
                pause(run, "master_needs_handover", host=host.name, cluster=host.cluster_name)
                return [("pause", "paused", {"reason": "master_needs_handover", "host": host.name})]
            return apply_master

    try:
        await ensure_no_maintenance(db, [host])
    except HTTPException:
        since = _ts(prog.get("blocked_since"))
        if since is None:
            async def apply_blocked_start(db, run):
                _progress(run, blocked_since=_iso(_now()))
                return [("host", "waiting_for_lock", {"host": host.name})]
            return apply_blocked_start
        if _now() - since < BLOCKED_PATIENCE:
            return None

        async def apply_blocked(db, run):
            pause(run, "blocked_by_maintenance", host=host.name)
            _progress(run, blocked_since=None)
            return [("pause", "paused", {"reason": "blocked_by_maintenance", "host": host.name})]
        return apply_blocked

    async def apply_start(db, run):
        await policy_lock(db)
        rh = next(h for h in run.hosts if h.id == nxt.id)
        # Idempotency guard: a host that already has a schedule never gets a second one.
        if rh.status != "queued" or rh.scheduled_maintenance_id is not None:
            return []
        now = _now()
        sched = ScheduledMaintenance(
            host_id=host.id, host_name=host.name, action=run.action, run_at=now, status="pending",
            actor=run.started_by, created_at=now, carp_handover=bool(spec.get("carp_handover") and host.cluster_name),
            auto_verify=True, origin="rollout",
            progress={"rollout": {"run_id": str(run.id), "wave": index, "plan": run.plan_name}},
        )
        db.add(sched)
        await db.flush()
        rh.scheduled_maintenance_id, rh.status, rh.started_at = sched.id, "running", now
        _progress(run, blocked_since=None, cluster_recheck_at=None)
        return [("host", "started", {"host": host.name, "schedule_id": str(sched.id), "handover": sched.carp_handover})]
    return apply_start


async def _handle_soaking(db: AsyncSession, run: RolloutRun) -> Decision | None:
    w = wave_row(run, run.current_wave)
    if w.soak_until and _now() < w.soak_until:
        return None

    async def apply(db, run):
        w = wave_row(run, run.current_wave)
        w.status, w.gate_attempts = "gate", 0
        run.status = "gate"
        _progress(run, gate_recheck_at=None)
        return []
    return apply


async def _handle_gate(db: AsyncSession, run: RolloutRun) -> Decision | None:
    recheck = _ts((run.progress or {}).get("gate_recheck_at"))
    if recheck and _now() < recheck:
        return None
    index = run.current_wave
    w = wave_row(run, index)
    done = [h for h in hosts_of(run, index) if h.status == "succeeded" and h.host_id]
    hosts = [h for h in [await db.get(OpnsenseHost, d.host_id) for d in done] if h is not None]
    gate = await rollout_health.evaluate(db, hosts, w.baseline or {}, target_version=run.target_version,
                                         checks=run.plan_snapshot.get("gate_checks"),
                                         compliance_mode=run.plan_snapshot.get("compliance_mode", "warn"))

    async def apply(db, run):
        w = wave_row(run, index)
        w.gate_result = gate
        w.gate_attempts = (w.gate_attempts or 0) + 1
        failed = rollout_health.failed_checks(gate)
        events = [("gate", gate["verdict"], {"wave": index, "attempt": w.gate_attempts, "failed": failed, "result": gate})]
        if gate["verdict"] == "fail":
            if w.gate_attempts < 2:
                _progress(run, gate_recheck_at=_iso(_now() + GATE_RECHECK))
                return events
            _progress(run, gate_recheck_at=None)
            pause(run, "gate_failed", wave=w.label, failed=_named(run, failed))
            return events + [("pause", "paused", {"reason": "gate_failed"})]
        w.status, w.finished_at = "passed", _now()
        _progress(run, gate_recheck_at=None)
        return events + _next_wave(run)
    return apply


_HANDLERS = {
    "waiting_window": _handle_waiting_window,
    "resuming": _handle_resuming,
    "preflight": _handle_preflight,
    "wave_running": _handle_wave_running,
    "soaking": _handle_soaking,
    "gate": _handle_gate,
}


# ── automatic start at a window occurrence ────────────────────────────────────

async def auto_start(db: AsyncSession) -> int:
    started = 0
    plans = (await db.scalars(select(RolloutPlan).where(
        RolloutPlan.auto_start.is_(True), RolloutPlan.activated_at.is_not(None), RolloutPlan.window_id.is_not(None)))).all()
    for plan in plans:
        window = await db.get(MaintenanceWindow, plan.window_id)
        occ = window_service.startable(window, _now()) if window else None
        if occ is None:
            continue
        if await db.scalar(select(RolloutRun.id).where(RolloutRun.plan_id == plan.id, RolloutRun.window_occurrence == occ.start)):
            continue
        if (plan.auto_start_error or {}).get("occurrence") == _iso(occ.start):
            continue  # already reported for this occurrence
        await policy_lock(db)
        plan = await rollout_service.load_plan(db, plan.id)
        if await db.scalar(select(RolloutRun.id).where(RolloutRun.plan_id == plan.id, RolloutRun.window_occurrence == occ.start)):
            continue  # another backend started it while this one waited for the lock (not a refusal)
        resolved = await rollout_service.resolve(db, plan)
        problem = None
        if plan.action != "update":
            problem = _note("auto_start_update_only")
        elif plan.activated_fingerprint != rollout_service.fingerprint(plan, resolved):
            problem = _note("plan_changed")  # someone changed the plan, its groups or its window since activation
        elif not await may_roll_out(db, plan.activated_by or "", plan.activated_by_id):
            problem = _note("actor_invalid", user=plan.activated_by)
        elif resolved["errors"]:
            problem = _note("plan_invalid", reason=resolved["errors"][0]["code"])
        elif (other := await active_run(db)) is not None:
            problem = _note("other_run_active", plan=other.plan_name)
        if problem:
            plan.auto_start_error = {"occurrence": _iso(occ.start), "at": _iso(_now()), **problem}
            await db.commit()
            token = audit_service.context.set({"actor": "system:rollout", "id": str(plan.id)})
            try:
                await audit_service.persist(kind="rollout", action="auto_start", status="refused", entity_id=str(plan.id),
                                            entity_name=plan.name, details=plan.auto_start_error)
            finally:
                audit_service.context.reset(token)
            continue
        try:
            run = await create_run(db, plan, resolved, started_by=plan.activated_by, started_by_id=plan.activated_by_id,
                               trigger="window", occurrence=occ.start)
            plan.auto_start_error = None
            await db.commit()
        except IntegrityError:
            await db.rollback()  # another backend started it for this occurrence
            continue
        started += 1
        await _audit(run, "start", "started", trigger="window", occurrence=_iso(occ.start),
                     hosts=sum(len(w["hosts"]) for w in resolved["waves"]))
    await db.commit()
    return started


# ── human actions (called by the router under policy_lock) ────────────────────

async def count_active(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(RolloutRun).where(RolloutRun.status.in_(ACTIVE))) or 0
