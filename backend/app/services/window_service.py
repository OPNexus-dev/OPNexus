"""Occurrences of recurring maintenance windows.

Rules (deliberately simple and written down, because a firmware rollout depends on them):

* A window is a wall-clock start time in its own IANA time zone plus a duration. "Sunday 02:00
  Europe/Berlin" stays 02:00 local time in summer and in winter.
* Daylight saving: a local start time that does not exist on that day (spring forward, e.g. 02:30 on
  the last Sunday of March in Europe/Berlin) moves FORWARD by the gap (03:30). A local time that
  exists twice (fall back) means the FIRST of the two (fold=0, still summer time). Both follow from
  Python's zoneinfo with fold=0 and are covered by tests.
* The duration is real elapsed time (a 3 h window over the fall-back night still lasts 3 h).
* Exceptions are keyed by the occurrence's local date: "skip" drops it, "move" replaces its start
  with an absolute instant (same duration and cut-off).
* New host actions may only START within the first ``start_cutoff_minutes`` of an occurrence
  (``startable``); actions already running finish even after the window has closed.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from app.models.maintenance_window import MaintenanceWindow

# How far ahead occurrences are searched. A monthly "5th ..." does not exist; the longest real gap
# between two occurrences is a month, so two years is plenty and bounds the work per call.
_SEARCH_DAYS = 2 * 366
MAX_DURATION_MINUTES = 24 * 60


@dataclass(frozen=True)
class Occurrence:
    local_date: date
    start: datetime  # UTC
    end: datetime  # UTC
    cutoff: datetime  # UTC: no new host action after this instant
    moved: bool = False
    skipped: bool = False

    def as_dict(self) -> dict:
        return {"local_date": self.local_date.isoformat(), "start": self.start.isoformat(), "end": self.end.isoformat(),
                "cutoff": self.cutoff.isoformat(), "moved": self.moved, "skipped": self.skipped}


def valid_timezone(name: str) -> bool:
    if not name or name not in available_timezones():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _matches(window: MaintenanceWindow, day: date) -> bool:
    if window.recurrence == "weekly":
        return day.weekday() in (window.weekdays or [])
    if window.recurrence == "monthly":
        if window.month_mode == "day":
            return day.day == window.month_day
        if window.month_mode == "nth_weekday":
            if day.weekday() != window.month_weekday:
                return False
            if window.month_nth == -1:
                return (day + timedelta(days=7)).month != day.month
            return (day.day - 1) // 7 + 1 == window.month_nth
    return False


def local_start(window: MaintenanceWindow, day: date) -> datetime:
    """The UTC instant of the window's start on local date ``day`` (DST rules see module docstring)."""
    tz = ZoneInfo(window.timezone)
    wall = datetime.combine(day, window.start_time, tzinfo=tz).replace(fold=0)
    return wall.astimezone(timezone.utc)


def _occurrence(window: MaintenanceWindow, day: date, start: datetime, *, moved: bool = False, skipped: bool = False) -> Occurrence:
    duration = timedelta(minutes=window.duration_minutes)
    cutoff = timedelta(minutes=min(window.start_cutoff_minutes, window.duration_minutes))
    return Occurrence(local_date=day, start=start, end=start + duration, cutoff=start + cutoff, moved=moved, skipped=skipped)


def occurrences(window: MaintenanceWindow, after: datetime, n: int = 10, *, with_skipped: bool = False) -> list[Occurrence]:
    """The next ``n`` occurrences whose END lies after ``after`` (so one that is open right now is first).
    ``with_skipped`` lists skipped ones too (flagged, for the UI); the engine never sees them."""
    if n <= 0:
        return []
    after = after.astimezone(timezone.utc)
    tz = ZoneInfo(window.timezone)
    exceptions = {e.occurrence_date: e for e in (window.exceptions or [])}
    duration = timedelta(minutes=window.duration_minutes)
    # Start one day early: an occurrence that began yesterday (local) may still be open.
    day = (after - duration).astimezone(tz).date() - timedelta(days=1)
    found: list[Occurrence] = []
    # Moved occurrences can land anywhere, so they are collected separately and merged at the end.
    for exc in exceptions.values():
        if exc.kind == "move" and exc.moved_start is not None and _matches(window, exc.occurrence_date):
            occ = _occurrence(window, exc.occurrence_date, exc.moved_start.astimezone(timezone.utc), moved=True)
            if occ.end > after:
                found.append(occ)
    regular = 0
    for _ in range(_SEARCH_DAYS):
        if regular >= n:
            break
        if _matches(window, day):
            exc = exceptions.get(day)
            if exc is None or (with_skipped and exc.kind == "skip"):
                occ = _occurrence(window, day, local_start(window, day), skipped=exc is not None)
                if occ.end > after:
                    found.append(occ)
                    regular += 1
        day += timedelta(days=1)
    found.sort(key=lambda o: o.start)
    return found[:n]


def current(window: MaintenanceWindow, now: datetime) -> Occurrence | None:
    """The occurrence open at ``now`` (start <= now < end), if any.

    Occurrences can overlap (a moved one reaching into the next regular one, a 24 h window on a 25 h day). Then the
    one that started LAST counts: its start cut-off lies latest, so "may a new host action start now" is not
    decided by an older occurrence whose cut-off has long passed (1.29; before, the earliest one was taken)."""
    if not window.enabled:
        return None
    open_now = [occ for occ in occurrences(window, now, 5) if occ.start <= now < occ.end]
    return max(open_now, key=lambda occ: occ.start) if open_now else None


def startable(window: MaintenanceWindow | None, now: datetime) -> Occurrence | None:
    """The occurrence in which a NEW host action may start right now (start <= now < cutoff)."""
    if window is None or not window.enabled:
        return None
    occ = current(window, now)
    if occ is not None and occ.start <= now < occ.cutoff:
        return occ
    return None


def is_occurrence_date(window: MaintenanceWindow, day: date) -> bool:
    return _matches(window, day)


def definition(window: MaintenanceWindow) -> dict:
    """What a rollout plan's fingerprint covers: changing any of this re-requires activation. Skipped and moved
    occurrences count too (1.29; keyed by the occurrence's local date, the reason text and the actor do not matter).
    Without any exception the result is the same as before 1.29, so existing activations stay valid."""
    result = {
        "id": str(window.id), "timezone": window.timezone, "recurrence": window.recurrence,
        "weekdays": sorted(window.weekdays or []), "month_mode": window.month_mode, "month_nth": window.month_nth,
        "month_weekday": window.month_weekday, "month_day": window.month_day,
        "start_time": window.start_time.strftime("%H:%M"), "duration_minutes": window.duration_minutes,
        "start_cutoff_minutes": window.start_cutoff_minutes, "enabled": window.enabled,
    }
    if window.exceptions:
        result["exceptions"] = sorted(
            [e.occurrence_date.isoformat(), e.kind, e.moved_start.astimezone(timezone.utc).isoformat() if e.moved_start else None]
            for e in window.exceptions)
    return result
