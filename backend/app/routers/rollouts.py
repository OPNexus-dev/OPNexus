"""Firmware rollouts in waves (1.25): plans and runs.

Roles (enforced in AuthMiddleware, see api_token_service.is_rollout): everybody signed in reads;
operators prepare plans; starting/activating a plan and resuming/approving/skipping/aborting a run
need the deploy right (admin, or operator while operators may deploy). Pausing is deliberately NOT
a rollout action: anybody who may write at all may always brake.
"""
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.maintenance_window import MaintenanceWindow
from app.models.policy import DeviceGroup
from app.models.rollout import RolloutPlan, RolloutPlanWave, RolloutRun
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services import audit_service, rollout_engine, rollout_health, rollout_service
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/rollouts", tags=["rollouts"])

ACTIVE_RUN = rollout_engine.ACTIVE


class WaveIn(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    group_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    soak_minutes: int = Field(default=30, ge=0, le=rollout_service.MAX_SOAK_MINUTES)
    carp_handover: bool = False
    require_approval: bool = False


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    action: Literal["update", "major_update"] = "update"
    window_id: uuid.UUID | None = None
    auto_start: bool = False
    gate_checks: dict[str, bool] = Field(default_factory=dict)
    compliance_mode: Literal["warn", "fail"] = "warn"
    waves: list[WaveIn] = Field(min_length=1, max_length=rollout_service.MAX_WAVES)


class TokenIn(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)


async def _plan_or_404(db: AsyncSession, plan_id: uuid.UUID) -> RolloutPlan:
    plan = await rollout_service.load_plan(db, plan_id)
    if plan is None:
        raise api_error(404, ErrorCode.ROLLOUT_PLAN_NOT_FOUND)
    return plan


async def read_plan(db: AsyncSession, plan: RolloutPlan, resolved: dict | None = None) -> dict:
    resolved = resolved or await rollout_service.resolve(db, plan)
    active = plan.activated_at is not None
    last_run = await db.scalar(select(RolloutRun).where(RolloutRun.plan_id == plan.id).order_by(RolloutRun.created_at.desc()).limit(1))
    return {
        **rollout_service.definition(plan),
        "description": plan.description,
        "waves": [{"id": str(w.id), "position": w.position, "label": w.label, "soak_minutes": w.soak_minutes,
                   "carp_handover": w.carp_handover, "require_approval": w.require_approval,
                   "group_ids": [str(g.id) for g in w.groups], "groups": [g.name for g in w.groups],
                   "host_count": len(resolved["waves"][i]["hosts"]) if i < len(resolved["waves"]) else 0}
                  for i, w in enumerate(plan.waves)],
        "active": active, "activated_by": plan.activated_by, "activated_at": plan.activated_at,
        # Activated but the plan, its groups or its window changed since: the automatic start is held back.
        "fingerprint_ok": (not active) or plan.activated_fingerprint == rollout_service.fingerprint(plan, resolved),
        "auto_start_error": plan.auto_start_error,
        "window": resolved["window"], "errors": resolved["errors"], "warnings": resolved["warnings"],
        "host_count": sum(len(w["hosts"]) for w in resolved["waves"]),
        "last_run": {"id": str(last_run.id), "status": last_run.status, "created_at": last_run.created_at} if last_run else None,
        "created_by": plan.created_by, "created_at": plan.created_at, "updated_at": plan.updated_at,
    }


async def _apply(db: AsyncSession, plan: RolloutPlan, payload: PlanIn) -> None:
    name = payload.name.strip()
    if not name:
        raise api_error(422, ErrorCode.ROLLOUT_INVALID, field="name")
    other = await db.scalar(select(RolloutPlan.id).where(RolloutPlan.name == name))
    if other is not None and other != plan.id:
        raise api_error(409, ErrorCode.ROLLOUT_NAME_TAKEN)
    if payload.window_id is not None and await db.get(MaintenanceWindow, payload.window_id) is None:
        raise api_error(422, ErrorCode.ROLLOUT_INVALID, field="window_id")
    if any(k not in rollout_health.CHECKS for k in payload.gate_checks):
        raise api_error(422, ErrorCode.ROLLOUT_INVALID, field="gate_checks")
    wanted = {g for w in payload.waves for g in w.group_ids}
    groups = {g.id: g for g in (await db.scalars(select(DeviceGroup).where(DeviceGroup.id.in_(wanted)))).all()}
    if len(groups) != len(wanted):
        raise api_error(422, ErrorCode.ROLLOUT_INVALID, field="group_ids")
    plan.name, plan.description, plan.action = name, payload.description.strip(), payload.action
    plan.window_id, plan.auto_start = payload.window_id, payload.auto_start
    plan.gate_checks = {k: bool(v) for k, v in payload.gate_checks.items()}
    plan.compliance_mode = payload.compliance_mode
    plan.waves = [
        RolloutPlanWave(position=i, label=w.label.strip() or f"{i + 1}", soak_minutes=w.soak_minutes, carp_handover=w.carp_handover,
                        require_approval=w.require_approval, groups=[groups[g] for g in dict.fromkeys(w.group_ids)])
        for i, w in enumerate(payload.waves)
    ]
    # Any edit disarms the automatic start: an operator cannot quietly change what an admin activated.
    plan.activated_at = plan.activated_by = plan.activated_by_id = plan.activated_fingerprint = None
    plan.auto_start_error = None


@router.get("/plans")
async def list_plans(db: AsyncSession = Depends(get_db)):
    plans = (await db.scalars(select(RolloutPlan).order_by(RolloutPlan.name))).all()
    return [await read_plan(db, p) for p in plans]


@router.post("/plans", status_code=201)
async def create_plan(payload: PlanIn, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    plan = RolloutPlan(created_by=request.state.user.username, created_at=datetime.now(timezone.utc), waves=[])
    await _apply(db, plan, payload)
    db.add(plan)
    await db.commit()
    return await read_plan(db, await _plan_or_404(db, plan.id))


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await read_plan(db, await _plan_or_404(db, plan_id))


@router.put("/plans/{plan_id}")
async def update_plan(plan_id: uuid.UUID, payload: PlanIn, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    plan = await _plan_or_404(db, plan_id)
    was_active = plan.activated_at is not None
    await _apply(db, plan, payload)
    await db.commit()
    if was_active:
        await audit_service.persist(kind="rollout", action="deactivate", status="completed", entity_id=str(plan.id),
                                    entity_name=plan.name, message="Plan geändert: automatischer Start deaktiviert")
    return await read_plan(db, await _plan_or_404(db, plan_id))


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    plan = await _plan_or_404(db, plan_id)
    if await db.scalar(select(RolloutRun.id).where(RolloutRun.plan_id == plan.id, RolloutRun.status.in_(ACTIVE_RUN)).limit(1)):
        raise api_error(409, ErrorCode.ROLLOUT_PLAN_BUSY)
    await db.delete(plan)
    await db.commit()


@router.post("/plans/{plan_id}/preview")
async def preview_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    plan = await _plan_or_404(db, plan_id)
    resolved = await rollout_service.resolve(db, plan)
    return {**resolved, "preview_token": rollout_service.fingerprint(plan, resolved),
            "can_start": not resolved["errors"], "plan": await read_plan(db, plan, resolved)}


async def _checked_token(db: AsyncSession, plan: RolloutPlan, token: str) -> tuple[dict, str]:
    resolved = await rollout_service.resolve(db, plan)
    current = rollout_service.fingerprint(plan, resolved)
    if token != current:
        raise api_error(409, ErrorCode.ROLLOUT_PREVIEW_STALE)
    if resolved["errors"]:
        raise api_error(422, ErrorCode.ROLLOUT_PLAN_INVALID, reason=resolved["errors"][0]["code"])
    return resolved, current


@router.post("/plans/{plan_id}/activate")
async def activate_plan(plan_id: uuid.UUID, payload: TokenIn, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    plan = await _plan_or_404(db, plan_id)
    if not plan.auto_start:
        raise api_error(422, ErrorCode.ROLLOUT_INVALID, field="auto_start")
    _resolved, current = await _checked_token(db, plan, payload.preview_token)
    plan.activated_by, plan.activated_at, plan.activated_fingerprint = request.state.user.username, datetime.now(timezone.utc), current
    plan.activated_by_id = request.state.user.id
    plan.auto_start_error = None
    await db.commit()
    await audit_service.persist(kind="rollout", action="activate", status="completed", entity_id=str(plan.id),
                                entity_name=plan.name, details={"fingerprint": current})
    return await read_plan(db, await _plan_or_404(db, plan_id))


@router.post("/plans/{plan_id}/deactivate")
async def deactivate_plan(plan_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    plan = await _plan_or_404(db, plan_id)
    plan.activated_by = plan.activated_by_id = plan.activated_at = plan.activated_fingerprint = None
    await db.commit()
    await audit_service.persist(kind="rollout", action="deactivate", status="completed", entity_id=str(plan.id), entity_name=plan.name)
    return await read_plan(db, await _plan_or_404(db, plan_id))


# ── runs ─────────────────────────────────────────────────────────────────────

class StartIn(TokenIn):
    confirm_name: str = Field(min_length=1, max_length=120)
    acknowledge_unattended: Literal[True]


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class SkipIn(BaseModel):
    host_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)


class AbortIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    confirm_name: str = Field(min_length=1, max_length=120)


def _read_run_summary(run: RolloutRun) -> dict:
    counts: dict[str, int] = {}
    for h in run.hosts:
        counts[h.status] = counts.get(h.status, 0) + 1
    return {
        "id": str(run.id), "plan_id": str(run.plan_id) if run.plan_id else None, "plan_name": run.plan_name,
        "action": run.action, "status": run.status, "paused_from": run.paused_from, "current_wave": run.current_wave,
        "wave_count": len(run.waves), "current_wave_label": next((w.label for w in run.waves if w.index == run.current_wave), None),
        "target_version": run.target_version, "pause_reason": run.pause_reason, "pause_detail": run.pause_detail,
        "started_by": run.started_by, "trigger": run.trigger, "window_occurrence": run.window_occurrence,
        "created_at": run.created_at, "finished_at": run.finished_at, "step_at": run.step_at, "host_counts": counts,
        "active": run.status in rollout_engine.ACTIVE,
    }


async def read_run(db: AsyncSession, run: RolloutRun) -> dict:
    scheds = {}
    ids = [h.scheduled_maintenance_id for h in run.hosts if h.scheduled_maintenance_id]
    if ids:
        scheds = {s.id: s for s in (await db.scalars(select(ScheduledMaintenance).where(ScheduledMaintenance.id.in_(ids)))).all()}
    soak_until = next((w.soak_until for w in run.waves if w.index == run.current_wave), None)
    return {
        **_read_run_summary(run),
        "soak_until": soak_until, "progress": {k: v for k, v in (run.progress or {}).items()
                                               if k in ("gate_recheck_at", "cluster_recheck_at", "blocked_since", "resume_gate",
                                                        "cluster_gate", "preflight_started", "resume_recheck_at")},
        "snapshot": run.plan_snapshot,
        "waves": [{"index": w.index, "label": w.label, "status": w.status, "gate_result": w.gate_result,
                   "gate_attempts": w.gate_attempts, "soak_until": w.soak_until, "approved_by": w.approved_by,
                   "started_at": w.started_at, "finished_at": w.finished_at,
                   "spec": run.plan_snapshot["waves"][w.index] if w.index < len(run.plan_snapshot.get("waves", [])) else None}
                  for w in run.waves],
        "hosts": [{"id": str(h.id), "host_id": str(h.host_id) if h.host_id else None, "host_name": h.host_name,
                   "cluster_name": h.cluster_name, "wave_index": h.wave_index, "order": h.order, "status": h.status,
                   "version_before": h.version_before, "version_after": h.version_after, "detail": h.detail,
                   "started_at": h.started_at, "finished_at": h.finished_at,
                   "schedule": ({"id": str(s.id), "status": s.status, "phase": s.phase, "carp_handover": s.carp_handover,
                                 "host_operation_id": str(s.host_operation_id) if s.host_operation_id else None}
                                if (s := scheds.get(h.scheduled_maintenance_id)) else None)}
                  for h in sorted(run.hosts, key=lambda h: (h.wave_index, h.order, h.host_name))],
    }


async def _cancel_if_pending(db: AsyncSession, sched_id) -> bool:
    """Cancel a host's schedule only if it has not fired. Locked: the poll loop may be firing it right now
    (fire_due holds the row), and a blind write would turn a running action into "cancelled"."""
    if sched_id is None:
        return False
    sched = await db.scalar(select(ScheduledMaintenance).where(ScheduledMaintenance.id == sched_id)
                            .with_for_update().execution_options(populate_existing=True))
    if sched is not None and sched.status == "pending":
        sched.status = "cancelled"
        return True
    return False


async def _run_or_404(db: AsyncSession, run_id: uuid.UUID, *, lock: bool = False) -> RolloutRun:
    stmt = select(RolloutRun).where(RolloutRun.id == run_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    run = await db.scalar(stmt)
    if run is None:
        raise api_error(404, ErrorCode.ROLLOUT_RUN_NOT_FOUND)
    return run


def _not_now(run: RolloutRun):
    return api_error(409, ErrorCode.ROLLOUT_ACTION_NOT_ALLOWED, status=run.status)


async def _done(db: AsyncSession, run: RolloutRun, action: str, status: str, request: Request, **details) -> dict:
    run.revision += 1
    await db.commit()
    await audit_service.persist(kind="rollout", action=action, status=status, entity_id=str(run.id), entity_name=run.plan_name,
                                details={"wave": run.current_wave, **details})
    return await read_run(db, await _run_or_404(db, run.id))


@router.post("/plans/{plan_id}/start", status_code=201)
async def start_plan(plan_id: uuid.UUID, payload: StartIn, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    plan = await _plan_or_404(db, plan_id)
    if payload.confirm_name != plan.name:
        raise api_error(409, ErrorCode.ROLLOUT_CONFIRM_NAME)
    resolved, _token = await _checked_token(db, plan, payload.preview_token)
    if (other := await rollout_engine.active_run(db)) is not None:
        # One run at a time: two runs could otherwise touch the same cluster at once.
        raise api_error(409, ErrorCode.ROLLOUT_ALREADY_ACTIVE, plan=other.plan_name)
    run = await rollout_engine.create_run(db, plan, resolved, started_by=request.state.user.username,
                                          started_by_id=request.state.user.id, trigger="manual")
    await db.commit()
    await audit_service.persist(kind="rollout", action="start", status="started", entity_id=str(run.id), entity_name=plan.name,
                                details={"trigger": "manual", "hosts": sum(len(w["hosts"]) for w in resolved["waves"]),
                                         "waves": [w["label"] for w in resolved["waves"]]})
    return await read_run(db, await _run_or_404(db, run.id))


@router.get("/runs")
async def list_runs(db: AsyncSession = Depends(get_db)):
    runs = (await db.scalars(select(RolloutRun).order_by(RolloutRun.created_at.desc()).limit(50))).all()
    return [_read_run_summary(r) for r in runs]


@router.get("/status")
async def rollout_status(db: AsyncSession = Depends(get_db)):
    """For /updates and the host pages: the active run and the hosts it still owns."""
    run = await rollout_engine.active_run(db)
    if run is None:
        return {"active": None, "host_ids": [], "current_host_ids": []}
    return {
        "active": _read_run_summary(run),
        "host_ids": [str(h.host_id) for h in run.hosts if h.host_id and h.status in ("queued", "blocked", "running", "verifying", "failed")],
        "current_host_ids": [str(h.host_id) for h in run.hosts if h.host_id and h.wave_index == run.current_wave
                             and h.status in ("queued", "blocked", "running", "verifying", "failed")],
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await read_run(db, await _run_or_404(db, run_id))


@router.post("/runs/{run_id}/pause")
async def pause_run(run_id: uuid.UUID, payload: ReasonIn, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    run = await _run_or_404(db, run_id, lock=True)
    if run.status not in rollout_engine.ACTIVE or run.status == "paused":
        raise _not_now(run)
    # A brake that lets the next host fire anyway is no brake: a step not fired yet goes back to the queue.
    requeued = [h.host_name for h in run.hosts if await rollout_engine.requeue_if_pending(db, h)]
    rollout_engine.pause(run, "manual", by=request.state.user.username, text=payload.reason.strip()[:300])
    return await _done(db, run, "pause", "paused", request, reason="manual", text=payload.reason.strip()[:300], requeued=requeued)


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    run = await _run_or_404(db, run_id, lock=True)
    if run.status != "paused":
        raise _not_now(run)
    failed = [h.host_name for h in run.hosts if h.status == "failed"]
    if failed:
        # A failed host is never retried by itself: check it, then skip it explicitly.
        raise api_error(409, ErrorCode.ROLLOUT_HOST_FAILED, hosts=", ".join(failed))
    previous = run.started_by
    # Whoever resumes takes over the responsibility (and the permission check) from here on.
    run.started_by, run.started_by_id = request.state.user.username, request.state.user.id
    run.status = "resuming"
    run.pause_reason = run.pause_detail = None
    run.progress = {**(run.progress or {}), "resume_attempts": 0, "resume_recheck_at": None, "paused_at": None}
    return await _done(db, run, "resume", "resuming", request, previous_owner=previous)


@router.post("/runs/{run_id}/approve")
async def approve_wave(run_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    run = await _run_or_404(db, run_id, lock=True)
    if run.status != "awaiting_approval":
        raise _not_now(run)
    wave = rollout_engine.wave_row(run, run.current_wave)
    wave.approved_by, wave.status = request.state.user.username, "preflight"
    run.started_by, run.started_by_id = request.state.user.username, request.state.user.id
    run.status = "preflight"
    run.progress = {**(run.progress or {}), "preflight_started": None, "preflight_requested_at": None}
    return await _done(db, run, "approve", "approved", request, wave_label=wave.label)


@router.post("/runs/{run_id}/skip-host")
async def skip_host(run_id: uuid.UUID, payload: SkipIn, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    run = await _run_or_404(db, run_id, lock=True)
    if run.status not in rollout_engine.ACTIVE:
        raise _not_now(run)
    host = next((h for h in run.hosts if h.host_id == payload.host_id), None)
    if host is None:
        raise api_error(404, ErrorCode.ROLLOUT_HOST_NOT_IN_RUN)
    if host.status in ("running", "verifying"):
        if not await _cancel_if_pending(db, host.scheduled_maintenance_id):
            raise api_error(409, ErrorCode.ROLLOUT_HOST_RUNNING, host=host.host_name)
        # not fired yet: nothing happened on the firewall
    elif host.status not in ("queued", "blocked", "failed"):
        raise api_error(409, ErrorCode.ROLLOUT_HOST_RUNNING, host=host.host_name)
    host.status = "skipped"
    host.detail = {"code": "skipped_by", "params": {"user": request.state.user.username, "reason": payload.reason.strip()[:300]}}
    host.finished_at = datetime.now(timezone.utc)
    return await _done(db, run, "skip_host", "skipped", request, host=host.host_name, reason=payload.reason.strip()[:300])


@router.post("/runs/{run_id}/abort")
async def abort_run(run_id: uuid.UUID, payload: AbortIn, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    run = await _run_or_404(db, run_id, lock=True)
    if run.status not in rollout_engine.ACTIVE:
        raise _not_now(run)
    if payload.confirm_name != run.plan_name:
        raise api_error(409, ErrorCode.ROLLOUT_CONFIRM_NAME)
    now = datetime.now(timezone.utc)
    still_running = []
    for host in run.hosts:
        if host.status in ("running", "verifying") and host.scheduled_maintenance_id:
            if await _cancel_if_pending(db, host.scheduled_maintenance_id):
                host.status, host.finished_at = "cancelled", now
            else:
                still_running.append(host.host_name)  # an action already on the firewall is finished in a controlled way
        elif host.status in ("queued", "blocked"):
            host.status, host.finished_at = "cancelled", now
    for wave in run.waves:
        if wave.status not in ("passed", "skipped"):
            wave.status = "cancelled"
    run.status, run.finished_at = "cancelled", now
    run.pause_reason, run.pause_detail = "aborted", {"by": request.state.user.username, "reason": payload.reason.strip()[:300],
                                                     "still_running": still_running}
    return await _done(db, run, "abort", "cancelled", request, reason=payload.reason.strip()[:300], still_running=still_running)
