"""Recurring maintenance windows: occurrence maths (weekly, monthly, DST, exceptions, cut-off)."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.models.maintenance_window import MaintenanceWindow, MaintenanceWindowException
from app.services import window_service as ws

UTC = timezone.utc


def _w(**kw) -> MaintenanceWindow:
    base = dict(name="w", timezone="Europe/Berlin", recurrence="weekly", weekdays=[6], start_time=time(2, 0),
                duration_minutes=180, start_cutoff_minutes=60, enabled=True, created_by="admin")
    base.update(kw)
    w = MaintenanceWindow(**base)
    w.exceptions = kw.get("exceptions", [])
    return w


def _starts(w, after, n=4, **kw):
    return [o.start for o in ws.occurrences(w, after, n, **kw)]


def test_weekly_sunday_in_summer_and_winter_keeps_local_time():
    w = _w()
    # 2026-10-18 is a Sunday (CEST, +2), 2026-11-01 a Sunday (CET, +1)
    starts = _starts(w, datetime(2026, 10, 12, tzinfo=UTC), 4)
    assert starts[0] == datetime(2026, 10, 18, 0, 0, tzinfo=UTC)
    assert starts[2] == datetime(2026, 11, 1, 1, 0, tzinfo=UTC)
    assert all(s.astimezone(ws.ZoneInfo("Europe/Berlin")).hour == 2 for s in starts)


def test_weekly_several_days_in_order():
    w = _w(weekdays=[1, 3], timezone="UTC", start_time=time(22, 0), duration_minutes=60)
    starts = _starts(w, datetime(2026, 10, 19, tzinfo=UTC), 3)  # Monday
    assert starts == [datetime(2026, 10, 20, 22, tzinfo=UTC), datetime(2026, 10, 22, 22, tzinfo=UTC),
                      datetime(2026, 10, 27, 22, tzinfo=UTC)]


def test_monthly_nth_weekday_first_and_last():
    first_tue = _w(recurrence="monthly", weekdays=[], month_mode="nth_weekday", month_nth=1, month_weekday=1, timezone="UTC")
    assert [s.date() for s in _starts(first_tue, datetime(2026, 10, 1, tzinfo=UTC), 3)] == [
        date(2026, 10, 6), date(2026, 11, 3), date(2026, 12, 1)]
    last_sun = _w(recurrence="monthly", weekdays=[], month_mode="nth_weekday", month_nth=-1, month_weekday=6, timezone="UTC")
    assert [s.date() for s in _starts(last_sun, datetime(2026, 10, 1, tzinfo=UTC), 3)] == [
        date(2026, 10, 25), date(2026, 11, 29), date(2026, 12, 27)]


def test_monthly_fixed_day():
    w = _w(recurrence="monthly", weekdays=[], month_mode="day", month_day=15, timezone="UTC")
    assert [s.date() for s in _starts(w, datetime(2026, 10, 16, tzinfo=UTC), 2)] == [date(2026, 11, 15), date(2026, 12, 15)]


def test_spring_forward_missing_time_moves_forward():
    # 2026-03-29 (last Sunday of March): 02:00-03:00 local does not exist in Europe/Berlin.
    w = _w(start_time=time(2, 30))
    occ = ws.occurrences(w, datetime(2026, 3, 28, tzinfo=UTC), 1)[0]
    assert occ.local_date == date(2026, 3, 29)
    assert occ.start == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # = 03:30 CEST
    assert occ.start.astimezone(ws.ZoneInfo("Europe/Berlin")).strftime("%H:%M") == "03:30"


def test_fall_back_ambiguous_time_uses_the_first_and_duration_is_real_time():
    # 2026-10-25 (last Sunday of October): 02:00-03:00 local happens twice.
    w = _w(start_time=time(2, 30), duration_minutes=120)
    occ = ws.occurrences(w, datetime(2026, 10, 24, tzinfo=UTC), 1)[0]
    assert occ.start == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST (first pass)
    assert occ.end - occ.start == timedelta(hours=2)


def test_skip_and_move_exceptions():
    moved = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)  # Saturday evening instead of Sunday night
    w = _w(exceptions=[
        MaintenanceWindowException(occurrence_date=date(2026, 10, 25), kind="skip", reason="x", actor="a"),
        MaintenanceWindowException(occurrence_date=date(2026, 11, 1), kind="move", moved_start=moved, reason="x", actor="a"),
    ])
    occs = ws.occurrences(w, datetime(2026, 10, 19, tzinfo=UTC), 3)
    assert [o.local_date for o in occs] == [date(2026, 11, 1), date(2026, 11, 8), date(2026, 11, 15)]
    assert occs[0].moved and occs[0].start == moved and occs[0].end == moved + timedelta(minutes=180)
    listed = ws.occurrences(w, datetime(2026, 10, 19, tzinfo=UTC), 3, with_skipped=True)
    assert [(o.local_date, o.skipped, o.moved) for o in listed] == [
        (date(2026, 10, 25), True, False), (date(2026, 11, 1), False, True), (date(2026, 11, 8), False, False)]


def test_open_window_is_current_and_cutoff_limits_new_starts():
    w = _w(timezone="UTC", weekdays=[6], start_time=time(2, 0), duration_minutes=180, start_cutoff_minutes=60)
    start = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)
    assert ws.current(w, start - timedelta(seconds=1)) is None
    assert ws.current(w, start).start == start
    assert ws.startable(w, start + timedelta(minutes=59)) is not None
    assert ws.startable(w, start + timedelta(minutes=60)) is None  # past the cut-off: nothing new starts
    assert ws.current(w, start + timedelta(minutes=179)) is not None  # still open, running work may finish
    assert ws.current(w, start + timedelta(minutes=180)) is None
    # an occurrence that is open right now is the first one listed
    assert ws.occurrences(w, start + timedelta(minutes=30), 1)[0].start == start


def test_cutoff_never_exceeds_duration_and_disabled_window_is_never_open():
    w = _w(timezone="UTC", duration_minutes=30, start_cutoff_minutes=90)
    start = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)
    assert ws.occurrences(w, start, 1)[0].cutoff == start + timedelta(minutes=30)
    w.enabled = False
    assert ws.current(w, start) is None and ws.startable(w, start) is None


@pytest.mark.parametrize("name,ok", [("Europe/Berlin", True), ("UTC", True), ("Mars/Olympus", False), ("", False), ("../etc", False)])
def test_timezone_validation(name, ok):
    assert ws.valid_timezone(name) is ok


# ── API ─────────────────────────────────────────────────────────────────────

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.audit import AuditEvent  # noqa: E402

_WEEKLY = {"name": "Sonntagnacht", "timezone": "Europe/Berlin", "recurrence": "weekly", "weekdays": [6],
           "start_time": "02:00", "duration_minutes": 180, "start_cutoff_minutes": 60}


@pytest.mark.asyncio
async def test_window_crud_and_next_occurrences(client):
    resp = await client.post("/api/maintenance-windows", json=_WEEKLY)
    assert resp.status_code == 201, resp.text
    w = resp.json()
    assert w["start_time"] == "02:00" and len(w["next"]) == 5 and w["exceptions"] == [] and w["used_by"] == []
    assert (await client.post("/api/maintenance-windows", json=_WEEKLY)).json()["detail"]["code"] == "WINDOW_NAME_TAKEN"
    upd = await client.put(f"/api/maintenance-windows/{w['id']}", json={**_WEEKLY, "start_time": "03:15", "start_cutoff_minutes": 999})
    assert upd.status_code == 200 and upd.json()["start_time"] == "03:15"
    assert upd.json()["start_cutoff_minutes"] == 180  # never longer than the window itself
    occ = await client.get(f"/api/maintenance-windows/{w['id']}/occurrences?count=12")
    assert occ.status_code == 200 and len(occ.json()) == 12
    assert (await client.delete(f"/api/maintenance-windows/{w['id']}")).status_code == 204
    assert (await client.get(f"/api/maintenance-windows/{w['id']}")).status_code == 404


@pytest.mark.parametrize("change,field", [
    ({"timezone": "Mars/Base"}, "timezone"),
    ({"start_time": "25:00"}, "start_time"),
    ({"weekdays": []}, "weekdays"),
    ({"weekdays": [7]}, "weekdays"),
    ({"recurrence": "monthly", "month_mode": "day", "month_day": 31}, "month_day"),
    ({"recurrence": "monthly", "month_mode": "nth_weekday", "month_nth": 5, "month_weekday": 1}, "month_nth"),
    ({"recurrence": "monthly"}, "month_mode"),
])
@pytest.mark.asyncio
async def test_window_validation(client, change, field):
    resp = await client.post("/api/maintenance-windows", json={**_WEEKLY, **change})
    assert resp.status_code == 422 and resp.json()["detail"] == {"code": "WINDOW_INVALID", "params": {"field": field}}


@pytest.mark.asyncio
async def test_exceptions_skip_move_and_audit(client):
    w = (await client.post("/api/maintenance-windows", json={**_WEEKLY, "timezone": "UTC"})).json()
    first = date.fromisoformat(w["next"][0]["local_date"])
    second = date.fromisoformat(w["next"][1]["local_date"])
    bad = await client.post(f"/api/maintenance-windows/{w['id']}/exceptions", json={"occurrence_date": (first + timedelta(days=1)).isoformat(), "kind": "skip"})
    assert bad.status_code == 422 and bad.json()["detail"]["params"]["field"] == "occurrence_date"
    past = await client.post(f"/api/maintenance-windows/{w['id']}/exceptions", json={"occurrence_date": (first - timedelta(days=14)).isoformat(), "kind": "skip"})
    assert past.status_code == 422
    skip = await client.post(f"/api/maintenance-windows/{w['id']}/exceptions", json={"occurrence_date": first.isoformat(), "kind": "skip", "reason": "Feiertag"})
    assert skip.status_code == 201, skip.text
    assert skip.json()["next"][0]["skipped"] is True
    dup = await client.post(f"/api/maintenance-windows/{w['id']}/exceptions", json={"occurrence_date": first.isoformat(), "kind": "skip"})
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "WINDOW_EXCEPTION_EXISTS"
    no_target = await client.post(f"/api/maintenance-windows/{w['id']}/exceptions", json={"occurrence_date": second.isoformat(), "kind": "move"})
    assert no_target.status_code == 422 and no_target.json()["detail"]["params"]["field"] == "moved_start"
    target = datetime.combine(second, time(20, 0), tzinfo=UTC) - timedelta(days=1)
    moved = await client.post(f"/api/maintenance-windows/{w['id']}/exceptions", json={"occurrence_date": second.isoformat(), "kind": "move", "moved_start": target.isoformat()})
    assert moved.status_code == 201
    regular = [o for o in moved.json()["next"] if not o["skipped"]]
    assert regular[0]["moved"] is True and datetime.fromisoformat(regular[0]["start"]) == target
    eid = moved.json()["exceptions"][1]["id"]
    after = await client.delete(f"/api/maintenance-windows/{w['id']}/exceptions/{eid}")
    assert after.status_code == 200 and len(after.json()["exceptions"]) == 1
    async with SessionLocal() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "maintenance_window"))).all()
    assert any(e.action == "exception_skip" and e.details["reason"] == "Feiertag" for e in events)
    assert any(e.status == "accepted" and e.action == "delete" for e in events)


@pytest.mark.asyncio
async def test_window_writes_follow_the_deploy_right(client):
    from tests.test_operator_role import _operator
    op = await _operator(client, "winop")
    try:
        made = await op.post("/api/maintenance-windows", json={**_WEEKLY, "name": "op-window"})
        assert made.status_code == 201  # operators may prepare windows while they may deploy
        await client.put("/api/settings/governance", json={"operators_may_deploy": False})
        for method, path, body in [
            ("POST", "/api/maintenance-windows", {**_WEEKLY, "name": "x"}),
            ("PUT", f"/api/maintenance-windows/{made.json()['id']}", _WEEKLY),
            ("DELETE", f"/api/maintenance-windows/{made.json()['id']}", None),
            ("POST", f"/api/maintenance-windows/{made.json()['id']}/exceptions", {"occurrence_date": "2030-01-06", "kind": "skip"}),
        ]:
            resp = await op.request(method, path, json=body)
            assert resp.status_code == 403 and resp.json()["detail"]["code"] == "DEPLOY_NEEDS_ADMIN", path
        assert (await op.get("/api/maintenance-windows")).status_code == 200
    finally:
        await op.aclose()
        await client.put("/api/settings/governance", json={"operators_may_deploy": True})
