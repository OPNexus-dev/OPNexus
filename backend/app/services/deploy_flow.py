"""The shared write path of every "push to a firewall, then confirm or auto-revert"
feature (rules, aliases, port forwards, 1:1 NAT, outbound NAT, hierarchy rules).

Safety property: from the moment OPNexus may have changed something on a
firewall, a durable record exists that lets the revert sweeper (or an operator)
find and undo it — even if the reply is lost, the apply fails or the process
dies in between.

1. *Intent* is committed BEFORE the first remote call:
   - existing deployment: status "pending" with the revert deadline armed and
     the previous payload as rollback target (a crash leaves a normal,
     sweepable pending change);
   - first deployment (no remote UUID known yet): status "revert_failed" =
     "outcome unknown, check the firewall" — the state that already blocks
     retries/edits and raises a notification; "Rollback wiederholen" just
     forgets the row when no UUID was ever recorded.
2. The write. A refusal the firewall answered restores the previous state; a
   lost/ambiguous reply keeps the intent record (existing: deadline stays armed,
   new: stays "unknown").
3. After a confirmed write the UUID is committed at once and the row becomes a
   normal armed "pending" change. If applying it fails the row is "error" but
   keeps its deadline, so the sweeper still reverts a possibly live change.
4. Only after a successful apply the confirmation window starts.

policy_lock (a transaction-scoped advisory lock) is released by the intermediate
commits and taken again right away; the intent row itself keeps other deploys
and the sweeper away in the gap (pending/unknown rows are never re-deployed).
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.opnsense_client import OpnsenseAmbiguousError, OpnsenseAPIError

UNKNOWN_OUTCOME = (
    "Ausgang unklar: Die Firewall hat die Anfrage möglicherweise ausgeführt. Stand dort prüfen; "
    "„Rollback wiederholen“ entfernt diesen Eintrag, ohne die Firewall anzusprechen."
)

_FIELDS = ("status", "message", "last_payload", "previous_payload", "revert_deadline", "deployed_at")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_unsettled(row) -> bool:
    """A change that may still be live and whose rollback is not yet done: pending, outcome unknown, or an
    "error" row whose revert deadline is still armed (written but apply/ordering not confirmed)."""
    return row.status in ("pending", "revert_failed") or (row.status == "error" and row.revert_deadline is not None)


def unsettled(model):
    """``is_unsettled`` as a SQL condition for the deployment model."""
    return or_(
        model.status.in_(("pending", "revert_failed")),
        and_(model.status == "error", model.revert_deadline.isnot(None)),
    )


async def checkpoint(db: AsyncSession) -> None:
    from app.services.policy_service import policy_lock

    await db.commit()
    await policy_lock(db)


async def push_to_host(
    db: AsyncSession,
    *,
    existing: Any | None,
    make: Callable[..., Any],
    uuid_attr: str,
    prior_uuid: str | None,
    prior_live_payload: dict | None,
    payload: dict,
    write: Callable[[str | None], Awaitable[str]],
    apply: Callable[[], Awaitable[Any]],
) -> Any:
    window = timedelta(seconds=settings.deploy_confirm_window_seconds)
    before = {name: getattr(existing, name) for name in _FIELDS} if existing else None
    # An armed "error" row (written, apply not confirmed) holds the UNCONFIRMED payload as last_payload: a retry must
    # keep the original rollback target instead of promoting that payload to it.
    armed = existing is not None and existing.status == "error" and existing.revert_deadline is not None
    rollback_target = existing.previous_payload if armed else prior_live_payload

    # 1. durable intent
    if existing is not None:
        row = existing
        row.status, row.message = "pending", None
        row.previous_payload, row.last_payload = rollback_target, payload
        row.revert_deadline, row.deployed_at = _now() + window, _now()
    else:
        row = make(
            status="revert_failed", message=UNKNOWN_OUTCOME, last_payload=payload, previous_payload=None,
            revert_deadline=None, deployed_at=_now(), **{uuid_attr: None},
        )
        db.add(row)
    # The intent is a technical safety record, not a state change worth a history
    # entry of its own (the request itself is already audited as "requested").
    row._skip_audit = True
    await checkpoint(db)
    row._skip_audit = False

    wrote = False
    try:
        new_uuid = await write(prior_uuid)
        wrote = True
        # 3. recoverable by UUID from here on
        setattr(row, uuid_attr, new_uuid)
        row.status, row.message = "pending", None
        row.previous_payload = rollback_target if existing is not None else None
        row.last_payload, row.revert_deadline, row.deployed_at = payload, _now() + window, _now()
        await checkpoint(db)
        await apply()
    except OpnsenseAPIError as exc:
        ambiguous = isinstance(exc, OpnsenseAmbiguousError)
        if wrote:
            # written, apply unconfirmed: possibly live — status "error" but the deadline stays armed
            row.status, row.message = "error", str(exc)
            row.revert_deadline, row.deployed_at = _now() + window, _now()
        elif existing is not None and ambiguous:
            # the update may or may not have been applied: keep the armed rollback
            row.status, row.message = "error", f"{exc} (Ausgang unklar, wird zurückgesetzt)"
            row.deployed_at = _now()
        elif existing is not None:
            # a refusal the firewall answered: nothing changed, restore what was there
            for name, value in before.items():
                setattr(row, name, value)
            row.status, row.message, row.deployed_at = "error", str(exc), _now()
        elif ambiguous:
            row.status, row.message = "revert_failed", f"{exc} — {UNKNOWN_OUTCOME}"
        else:
            row.status, row.message = "error", str(exc)
            row.last_payload, row.revert_deadline = None, None
        return row

    # 4. confirmed: the confirmation window starts now
    row.status, row.message = "pending", None
    row.revert_deadline, row.deployed_at = _now() + window, _now()
    return row
