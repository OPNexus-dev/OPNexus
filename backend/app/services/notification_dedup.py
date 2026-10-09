"""Diffs a check's current-problem snapshot against previously open
NotificationProblem rows, so a still-open problem is never re-notified
every poll cycle — only transitions matter."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationProblem


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Problems(dict):
    """The result of a check ({dedup_key: context}) plus the key prefixes the
    check could NOT evaluate this round (e.g. a cluster member is offline, so
    drift is not computable). Open problems under such a prefix are left alone
    instead of being resolved and re-opened on the next round."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.preserve_prefixes: set[str] = set()


async def diff_and_update(
    db: AsyncSession, problem_type: str, current: dict[str, dict]
) -> tuple[list[NotificationProblem], list[NotificationProblem]]:
    """current: {dedup_key: context}. Returns (opened, resolved) — the rows
    that just transitioned, not the full open set. A key present in both
    the last run and this one has its last_seen_at/context refreshed but is
    NOT returned (that's the dedup guarantee)."""
    result = await db.execute(
        select(NotificationProblem).where(
            NotificationProblem.problem_type == problem_type,
            NotificationProblem.status == "open",
        )
    )
    open_rows = {row.dedup_key: row for row in result.scalars().all()}

    # (problem_type, dedup_key) is unique across *all* rows, so a problem that
    # was resolved earlier and now comes back must reuse its row instead of
    # inserting a second one (that raised IntegrityError and aborted the whole
    # check, silently dropping e.g. "rollback failed" alerts).
    returning = [k for k in current if k not in open_rows]
    resolved_rows = {}
    if returning:
        again = await db.execute(
            select(NotificationProblem).where(
                NotificationProblem.problem_type == problem_type,
                NotificationProblem.dedup_key.in_(returning),
            )
        )
        resolved_rows = {row.dedup_key: row for row in again.scalars().all()}

    opened: list[NotificationProblem] = []
    resolved: list[NotificationProblem] = []
    now = _now()

    for key, context in current.items():
        existing = open_rows.get(key)
        if existing is None and key in resolved_rows:
            row = resolved_rows[key]
            row.status = "open"
            row.context = context
            row.first_detected_at = now
            row.last_seen_at = now
            row.resolved_at = None
            row.last_notified_at = None
            row.resolved_notified_at = None
            opened.append(row)
        elif existing is None:
            row = NotificationProblem(
                problem_type=problem_type,
                dedup_key=key,
                status="open",
                context=context,
                first_detected_at=now,
                last_seen_at=now,
            )
            db.add(row)
            opened.append(row)
        else:
            existing.last_seen_at = now
            existing.context = context

    preserve = tuple(getattr(current, "preserve_prefixes", ()))
    for key, row in open_rows.items():
        if key not in current and not (preserve and key.startswith(preserve)):
            row.status = "resolved"
            row.resolved_at = now
            resolved.append(row)

    return opened, resolved
