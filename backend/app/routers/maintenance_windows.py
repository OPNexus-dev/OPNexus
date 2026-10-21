"""Recurring maintenance windows and their exceptions (1.25).

Every write here counts as a rollout action (api_token_service.is_rollout): a window decides WHEN
unattended firmware actions may run, so an operator needs the same right as for deploying.
"""
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.maintenance_window import MaintenanceWindow, MaintenanceWindowException
from app.services import audit_service, window_service
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/maintenance-windows", tags=["maintenance-windows"])

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class WindowIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=64)
    recurrence: Literal["weekly", "monthly"]
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    month_mode: Literal["nth_weekday", "day"] | None = None
    month_nth: int | None = None
    month_weekday: int | None = None
    month_day: int | None = None
    start_time: str
    duration_minutes: int = Field(ge=15, le=window_service.MAX_DURATION_MINUTES)
    start_cutoff_minutes: int = Field(default=60, ge=5, le=window_service.MAX_DURATION_MINUTES)
    enabled: bool = True


class ExceptionIn(BaseModel):
    occurrence_date: date
    kind: Literal["skip", "move"]
    moved_start: datetime | None = None
    reason: str = Field(default="", max_length=300)


def _invalid(field: str):
    return api_error(422, ErrorCode.WINDOW_INVALID, field=field)


def _validated(payload: WindowIn) -> dict:
    name = payload.name.strip()
    if not name:
        raise _invalid("name")
    if not window_service.valid_timezone(payload.timezone):
        raise _invalid("timezone")
    match = _TIME.match(payload.start_time)
    if not match:
        raise _invalid("start_time")
    values = {
        "name": name, "timezone": payload.timezone, "recurrence": payload.recurrence,
        "weekdays": [], "month_mode": None, "month_nth": None, "month_weekday": None, "month_day": None,
        "start_time": time(int(match.group(1)), int(match.group(2))),
        "duration_minutes": payload.duration_minutes,
        "start_cutoff_minutes": min(payload.start_cutoff_minutes, payload.duration_minutes),
        "enabled": payload.enabled,
    }
    if payload.recurrence == "weekly":
        days = sorted(set(payload.weekdays))
        if not days or any(d < 0 or d > 6 for d in days):
            raise _invalid("weekdays")
        values["weekdays"] = days
    elif payload.month_mode == "day":
        if payload.month_day is None or not 1 <= payload.month_day <= 28:
            raise _invalid("month_day")
        values.update(month_mode="day", month_day=payload.month_day)
    elif payload.month_mode == "nth_weekday":
        if payload.month_nth not in (1, 2, 3, 4, -1):
            raise _invalid("month_nth")
        if payload.month_weekday is None or not 0 <= payload.month_weekday <= 6:
            raise _invalid("month_weekday")
        values.update(month_mode="nth_weekday", month_nth=payload.month_nth, month_weekday=payload.month_weekday)
    else:
        raise _invalid("month_mode")
    return values


async def _plans_using(db: AsyncSession, window_id) -> list[dict]:
    from app.models.rollout import RolloutPlan
    rows = (await db.scalars(select(RolloutPlan).where(RolloutPlan.window_id == window_id).order_by(RolloutPlan.name))).all()
    return [{"id": str(p.id), "name": p.name, "auto_start": p.auto_start, "active": p.activated_at is not None} for p in rows]


def read_window(window: MaintenanceWindow, *, now: datetime | None = None, count: int = 5) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "id": str(window.id), "name": window.name, "timezone": window.timezone, "recurrence": window.recurrence,
        "weekdays": window.weekdays or [], "month_mode": window.month_mode, "month_nth": window.month_nth,
        "month_weekday": window.month_weekday, "month_day": window.month_day,
        "start_time": window.start_time.strftime("%H:%M"), "duration_minutes": window.duration_minutes,
        "start_cutoff_minutes": window.start_cutoff_minutes, "enabled": window.enabled,
        "created_by": window.created_by, "created_at": window.created_at,
        "open_now": window_service.current(window, now) is not None,
        "next": [o.as_dict() for o in window_service.occurrences(window, now, count, with_skipped=True)] if window.enabled else [],
        "exceptions": [
            {"id": str(e.id), "occurrence_date": e.occurrence_date.isoformat(), "kind": e.kind,
             "moved_start": e.moved_start, "reason": e.reason, "actor": e.actor, "created_at": e.created_at}
            for e in window.exceptions
        ],
    }


async def _window_or_404(db: AsyncSession, window_id: uuid.UUID) -> MaintenanceWindow:
    window = await db.get(MaintenanceWindow, window_id)
    if window is None:
        raise api_error(404, ErrorCode.WINDOW_NOT_FOUND)
    return window


async def _name_taken(db: AsyncSession, name: str, own_id=None) -> bool:
    other = await db.scalar(select(MaintenanceWindow.id).where(MaintenanceWindow.name == name))
    return other is not None and other != own_id


@router.get("")
async def list_windows(db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(MaintenanceWindow).order_by(MaintenanceWindow.name))).all()
    result = []
    for w in rows:
        item = read_window(w)
        item["used_by"] = await _plans_using(db, w.id)
        result.append(item)
    return result


@router.post("", status_code=201)
async def create_window(payload: WindowIn, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    values = _validated(payload)
    if await _name_taken(db, values["name"]):
        raise api_error(409, ErrorCode.WINDOW_NAME_TAKEN)
    window = MaintenanceWindow(**values, created_by=request.state.user.username, created_at=datetime.now(timezone.utc))
    window.exceptions = []
    db.add(window)
    await db.commit()
    item = read_window(await _window_or_404(db, window.id))
    item["used_by"] = []
    return item


@router.get("/{window_id}")
async def get_window(window_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    item = read_window(await _window_or_404(db, window_id))
    item["used_by"] = await _plans_using(db, window_id)
    return item


@router.put("/{window_id}")
async def update_window(window_id: uuid.UUID, payload: WindowIn, db: AsyncSession = Depends(get_db)):
    """Changing a window changes the fingerprint of every plan that uses it, so an activated
    automatic start stops until the plan is activated again (see rollout_service.fingerprint)."""
    await policy_lock(db)
    window = await _window_or_404(db, window_id)
    values = _validated(payload)
    if await _name_taken(db, values["name"], window.id):
        raise api_error(409, ErrorCode.WINDOW_NAME_TAKEN)
    for key, value in values.items():
        setattr(window, key, value)
    await db.commit()
    item = read_window(await _window_or_404(db, window_id))
    item["used_by"] = await _plans_using(db, window_id)
    return item


@router.delete("/{window_id}", status_code=204)
async def delete_window(window_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    window = await _window_or_404(db, window_id)
    # A plan without a window means "no time limit": deleting its window must never turn it into one.
    if await _plans_using(db, window_id):
        raise api_error(409, ErrorCode.WINDOW_IN_USE)
    await db.delete(window)
    await db.commit()


@router.get("/{window_id}/occurrences")
async def window_occurrences(window_id: uuid.UUID, count: int = Query(10, ge=1, le=50), db: AsyncSession = Depends(get_db)):
    window = await _window_or_404(db, window_id)
    return [o.as_dict() for o in window_service.occurrences(window, datetime.now(timezone.utc), count, with_skipped=True)]


@router.post("/{window_id}/exceptions", status_code=201)
async def add_exception(window_id: uuid.UUID, payload: ExceptionIn, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    window = await _window_or_404(db, window_id)
    now = datetime.now(timezone.utc)
    if not window_service.is_occurrence_date(window, payload.occurrence_date):
        raise api_error(422, ErrorCode.WINDOW_EXCEPTION_INVALID, field="occurrence_date")
    # Only future occurrences (or the one open now) can be changed; history stays as it was.
    if window_service.local_start(window, payload.occurrence_date) + timedelta(minutes=window.duration_minutes) <= now:
        raise api_error(422, ErrorCode.WINDOW_EXCEPTION_INVALID, field="occurrence_date")
    moved = None
    if payload.kind == "move":
        if payload.moved_start is None:
            raise api_error(422, ErrorCode.WINDOW_EXCEPTION_INVALID, field="moved_start")
        moved = payload.moved_start if payload.moved_start.tzinfo else payload.moved_start.replace(tzinfo=timezone.utc)
        if moved <= now:
            raise api_error(422, ErrorCode.WINDOW_EXCEPTION_INVALID, field="moved_start")
    if any(e.occurrence_date == payload.occurrence_date for e in window.exceptions):
        raise api_error(409, ErrorCode.WINDOW_EXCEPTION_EXISTS)
    exc = MaintenanceWindowException(
        window_id=window.id, occurrence_date=payload.occurrence_date, kind=payload.kind, moved_start=moved,
        reason=payload.reason.strip(), actor=request.state.user.username, created_at=now,
    )
    db.add(exc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise api_error(409, ErrorCode.WINDOW_EXCEPTION_EXISTS)
    await audit_service.persist(
        kind="maintenance_window", action=f"exception_{payload.kind}", status="saved", entity_id=str(window.id),
        entity_name=window.name, details={"occurrence_date": payload.occurrence_date.isoformat(),
                                          "moved_start": moved.isoformat() if moved else None, "reason": exc.reason},
    )
    db.expire_all()
    return read_window(await _window_or_404(db, window_id))


@router.delete("/{window_id}/exceptions/{exception_id}")
async def delete_exception(window_id: uuid.UUID, exception_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    window = await _window_or_404(db, window_id)
    exc = await db.get(MaintenanceWindowException, exception_id)
    if exc is None or exc.window_id != window.id:
        raise api_error(404, ErrorCode.WINDOW_EXCEPTION_NOT_FOUND)
    await db.delete(exc)
    await db.commit()
    db.expire_all()
    return read_window(await _window_or_404(db, window_id))

