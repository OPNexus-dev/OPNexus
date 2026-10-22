"""Change history of the rules that are live on a firewall, also those OPNexus did not create.

Every ``rule_observe`` cycle reads ``searchRule`` (and the counters, for the usage sampling) of each enabled
firewall and compares the user rules with the last observed state (``live_rule_states``). A difference becomes
one ``rule_change_events`` row: added, changed, removed, enabled or disabled, with the rule as it was and as it
is. The first observation of a firewall only records the starting point (no flood of "added" events).

Origin: a change is ``opnexus`` when the audit trail has a deployment event for that rule UUID on that host in
the time since the previous observation (the actor is taken from it), else ``external`` (changed in the
firewall's own GUI, by a script, ...). The audit entry is written a moment after the change reaches the firewall,
so an event classified ``external`` is re-checked in the next cycles (``reconcile_origins``).

Two processes may run the loop at once (overlapping web updates, a replica): every change is claimed with an
optimistic ``version`` on the state row and the event carries that version in a unique key, so a change is
recorded exactly once.

Only the rows' own fields are kept; derived display fields (``%…``, ``alias_meta_*``, colours, priorities) are
dropped so that a changed alias or colour does not look like a rule change.
"""
import hashlib
import json
import logging
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.host import OpnsenseHost
from app.models.policy import RuleRevision
from app.models.rule_deployment import RuleDeployment
from app.models.rule_history import LiveRuleState, RuleChangeEvent
from app.services import opnsense_client as client
from app.services import rule_hit_service
from app.services.opnsense_client import OpnsenseAPIError

logger = logging.getLogger(__name__)

BASELINE_KEY = "__baseline__"
RETENTION_DAYS = 365
# Audit kinds whose events carry the UUID of a rule on a firewall in details["remote_uuid"]. "rule_cleanup": the
# cleanup assistant (rule_cleanup_service) switching a hand-made rule off/on, deleting or restoring it.
AUDIT_KINDS = ("rule", "policy_hierarchy", "rule_cleanup")
# Audit statuses that mean the firewall's rule was NOT changed by that event (so it cannot explain a change).
# "confirmed": the cleanup's confirmation only re-reads the firewall.
AUDIT_STATUSES_NO_CHANGE = ("requested", "interrupted", "error", "confirmed")
# The audit entry is written right after the firewall changed; allow for the observer running just before it.
AUDIT_SLACK = timedelta(minutes=2)
RECHECK_WINDOW = timedelta(minutes=45)
_DROP_PREFIXES = ("%", "alias_meta", "#")
_DROP_KEYS = {"category_colors", "categories_colors", "icons", "ref"}


def clean_row(row: dict) -> dict:
    """The part of a ``searchRule`` row that describes the rule itself."""
    return {k: v for k, v in row.items() if not k.startswith(_DROP_PREFIXES) and k not in _DROP_KEYS}


def fingerprint(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _enabled(row: dict) -> bool:
    return str(row.get("enabled", "1")).strip().lower() in ("1", "true", "yes", "on")


# Fields that only say where the rule sits in the order. Another rule being inserted or a group being re-sorted
# changes them for many rules at once; that is a "moved", not an edit of the rule.
ORDER_FIELDS = frozenset({"sort_order", "prio_group", "sequence"})
# Kinds after which what OPNexus learned about the rule's traffic no longer describes it.
RESTARTS_USAGE = frozenset({"added", "changed", "enabled", "disabled"})


def change_kind(before: dict | None, after: dict | None) -> str:
    if before is None:
        return "added"
    if after is None:
        return "removed"
    differing = {k for k in {*before, *after} if before.get(k) != after.get(k)}
    if differing == {"enabled"}:
        return "enabled" if _enabled(after) else "disabled"
    if differing and differing <= ORDER_FIELDS:
        return "moved"
    return "changed"


def diff_fields(before: dict | None, after: dict | None, limit: int = 12) -> list[dict]:
    """Short field-by-field difference: [{field, before, after}] (a missing side is None)."""
    before, after = before or {}, after or {}
    changes = [{"field": key, "before": before.get(key), "after": after.get(key)}
               for key in sorted({*before, *after}) if before.get(key) != after.get(key)]
    return changes[:limit]


def _tracked(search: dict) -> dict[str, dict] | None:
    """{rule label: cleaned row} of the user rules; None when the answer is not a rule list at all (an error
    page must never look like 'every rule was removed')."""
    rows = search.get("rows") if isinstance(search, dict) else None
    if not isinstance(rows, list):
        return None
    result: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("uuid") or row.get("is_automatic") in (True, 1, "1"):
            continue
        result[str(row["uuid"]).lower()[:64]] = clean_row(row)
    return result


async def _find_audit(db: AsyncSession, host_id, rule_uuid: str, start: datetime, end: datetime) -> AuditEvent | None:
    """The newest deployment audit entry of this rule after ``start`` that no other event has claimed yet. The
    audit entry is written after the firewall changed, so it can only lie after the previous observation."""
    claimed = select(RuleChangeEvent.audit_id).where(RuleChangeEvent.audit_id.is_not(None))
    return await db.scalar(
        select(AuditEvent).where(
            AuditEvent.host_id == str(host_id), AuditEvent.kind.in_(AUDIT_KINDS),
            AuditEvent.created_at >= start, AuditEvent.created_at <= end + AUDIT_SLACK,
            AuditEvent.id.notin_(claimed),
            AuditEvent.status.notin_(AUDIT_STATUSES_NO_CHANGE),
            AuditEvent.details["remote_uuid"].as_string() == rule_uuid,
        ).order_by(AuditEvent.created_at.desc()).limit(1))


async def observe_rows(db: AsyncSession, host: OpnsenseHost, search: dict, now: datetime | None = None) -> dict:
    """Compare a ``searchRule`` answer with the stored state of the host. Returns {"baseline": bool, "events": n}."""
    now = now or datetime.now(timezone.utc)
    current = _tracked(search)
    if current is None or not rule_hit_service.looks_complete(search):
        # Not a full rule list (no automatic rules in it): never read an empty or partial answer as "all removed".
        return {"baseline": False, "events": 0, "skipped": True, "touched": []}
    states = {s.rule_uuid: s for s in (await db.scalars(select(LiveRuleState).where(LiveRuleState.host_id == host.id))).all()}
    baseline = states.get(BASELINE_KEY)
    since = baseline.last_seen_at if baseline else now
    events = 0
    touched: list[str] = []
    if baseline is not None:
        known = [u for u, st in states.items() if u != BASELINE_KEY and st.present]
        missing = sorted(u for u in known if u not in current)
        if known and len(missing) * 2 > len(known):
            # More than half of the known rules vanished at once: possible, but far more often a broken answer.
            # Only the same picture on two observations in a row counts.
            marker = hashlib.sha256("\n".join(missing).encode()).hexdigest()
            if (baseline.row or {}).get("pending") != marker:
                await db.execute(update(LiveRuleState).where(
                    LiveRuleState.host_id == host.id, LiveRuleState.rule_uuid == BASELINE_KEY)
                    .values(row={"pending": marker}).execution_options(synchronize_session=False))
                await db.commit()
                return {"baseline": False, "events": 0, "skipped": True, "touched": [], "unconfirmed": len(missing)}

    async def claim(rule_uuid: str, state: LiveRuleState, new_row: dict | None) -> int | None:
        """Move the state to its new version; None when another process already did."""
        values = {"version": LiveRuleState.version + 1, "changed_at": now, "last_seen_at": now,
                  "present": new_row is not None}
        if new_row is not None:
            values.update(fingerprint=fingerprint(new_row), row=new_row)
        else:
            values.update(fingerprint="")
        return await db.scalar(update(LiveRuleState).where(
            LiveRuleState.host_id == host.id, LiveRuleState.rule_uuid == rule_uuid, LiveRuleState.version == state.version,
        ).values(**values).returning(LiveRuleState.version).execution_options(synchronize_session=False))

    async def record(rule_uuid: str, seq: int, before: dict | None, after: dict | None) -> None:
        nonlocal events
        kind = change_kind(before, after)
        if kind in RESTARTS_USAGE:
            touched.append(rule_uuid)
        audit = await _find_audit(db, host.id, rule_uuid, since, now)
        stmt = pg_insert(RuleChangeEvent).values(
            id=uuid_lib.uuid4(), host_id=host.id, rule_uuid=rule_uuid, seq=seq, observed_at=now,
            kind=kind, before=before, after=after,
            origin="opnexus" if audit else "external", actor=audit.actor if audit else None,
            audit_id=audit.id if audit else None, window_start=since,
        ).on_conflict_do_nothing(constraint="uq_rule_change_event")
        events += 1 if (await db.execute(stmt)).rowcount else 0

    for rule_uuid, row in current.items():
        state = states.get(rule_uuid)
        if state is None:
            stmt = pg_insert(LiveRuleState).values(
                host_id=host.id, rule_uuid=rule_uuid, fingerprint=fingerprint(row), row=row, present=True, version=1,
                first_seen_at=now, changed_at=now, last_seen_at=now).on_conflict_do_nothing()
            inserted = (await db.execute(stmt)).rowcount
            if inserted and baseline is not None:
                await record(rule_uuid, 1, None, row)
        elif not state.present:
            seq = await claim(rule_uuid, state, row)
            if seq is not None:
                await record(rule_uuid, seq, None, row)
        elif state.fingerprint != fingerprint(row):
            seq = await claim(rule_uuid, state, row)
            if seq is not None:
                await record(rule_uuid, seq, state.row, row)
    if baseline is not None:
        for rule_uuid, state in states.items():
            if rule_uuid == BASELINE_KEY or rule_uuid in current or not state.present:
                continue
            seq = await claim(rule_uuid, state, None)
            if seq is not None:
                await record(rule_uuid, seq, state.row, None)
    unchanged = [u for u in current if u in states and states[u].present]
    if unchanged:
        await db.execute(update(LiveRuleState).where(
            LiveRuleState.host_id == host.id, LiveRuleState.rule_uuid.in_(unchanged)).values(last_seen_at=now)
            .execution_options(synchronize_session=False))
    if baseline is None:
        await db.execute(pg_insert(LiveRuleState).values(
            host_id=host.id, rule_uuid=BASELINE_KEY, fingerprint="", row={}, present=False, version=1,
            first_seen_at=now, changed_at=now, last_seen_at=now).on_conflict_do_nothing())
    else:
        await db.execute(update(LiveRuleState).where(
            LiveRuleState.host_id == host.id, LiveRuleState.rule_uuid == BASELINE_KEY).values(last_seen_at=now, row={})
            .execution_options(synchronize_session=False))
    await db.commit()
    return {"baseline": baseline is None, "events": events, "touched": touched}


async def reconcile_origins(db: AsyncSession, host: OpnsenseHost, now: datetime | None = None) -> int:
    """Recent 'external' events may only look so because the audit entry was written after the observation."""
    now = now or datetime.now(timezone.utc)
    recent = (await db.scalars(select(RuleChangeEvent).where(
        RuleChangeEvent.host_id == host.id, RuleChangeEvent.origin == "external",
        RuleChangeEvent.observed_at >= now - RECHECK_WINDOW).order_by(RuleChangeEvent.observed_at, RuleChangeEvent.seq))).all()
    fixed = 0
    for event in recent:  # oldest first: an audit entry belongs to the earliest change it can explain
        start = event.window_start or (event.observed_at - RECHECK_WINDOW)
        audit = await _find_audit(db, host.id, event.rule_uuid, start, event.observed_at)
        if audit is not None:
            event.origin, event.actor, event.audit_id = "opnexus", audit.actor, audit.id
            # (autoflush makes the claim visible to the next event's query)
            fixed += 1
    if fixed:
        await db.commit()
    return fixed


async def observe_host(db: AsyncSession, host: OpnsenseHost, now: datetime | None = None) -> dict:
    """Two GETs: the rules and their counters. One read serves the usage sampling and the history."""
    # First settle the origin of earlier events (their audit entries may have arrived late), so that an entry is
    # not taken by a newer change of the same rule that happens to fall in its window.
    await reconcile_origins(db, host, now)
    search = await client.search_rules(host, all_rows=True)
    try:
        counters = await rule_hit_service.read_counters(host)
    except OpnsenseAPIError as exc:
        counters = None
        logger.info("Regelzähler von %s nicht lesbar: %s", host.name, exc)
    result = await observe_rows(db, host, search, now)
    if not result.get("skipped"):  # a rejected or unconfirmed answer must not touch the usage data either
        if counters is not None:
            await rule_hit_service.apply_sample(db, host, counters, search, now)
        await rule_hit_service.restart_window(db, host.id, result["touched"], now)
    return result


async def observe_all(db: AsyncSession, now: datetime | None = None) -> dict:
    hosts = (await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)).order_by(OpnsenseHost.name))).all()
    summary = {"hosts": 0, "failed": 0, "events": 0}
    for host in hosts:
        try:
            result = await observe_host(db, host, now)
            summary["hosts"] += 1
            summary["events"] += result["events"]
        except OpnsenseAPIError as exc:
            summary["failed"] += 1
            logger.warning("Regelverlauf von %s nicht lesbar: %s", host.name, exc)
        except Exception:  # noqa: BLE001 - one host must never stop the cycle for the others
            summary["failed"] += 1
            await db.rollback()
            logger.exception("Regelverlauf von %s fehlgeschlagen", host.name)
    return summary


async def prune(db: AsyncSession, retention_days: int = RETENTION_DAYS) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    events = await db.execute(delete(RuleChangeEvent).where(RuleChangeEvent.observed_at < cutoff))
    gone = await db.execute(delete(LiveRuleState).where(
        LiveRuleState.present.is_(False), LiveRuleState.rule_uuid != BASELINE_KEY, LiveRuleState.changed_at < cutoff))
    # Finished cleanup operations (with the snapshots of deleted rules) are kept as long as the history.
    from app.services.rule_cleanup_service import prune_operations
    cleanups = await prune_operations(db, cutoff)
    await db.commit()
    return (events.rowcount or 0) + (gone.rowcount or 0) + cleanups


# ── Reading the history of one rule ───────────────────────────────────────

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def rule_timeline(db: AsyncSession, host: OpnsenseHost, rule_uuid: str, limit: int = 200) -> list[dict]:
    """Observed changes, saved revisions (OPNexus-managed rules) and audit entries of one rule, newest first."""
    rule_uuid = rule_uuid.lower()[:64]
    items: list[dict] = []
    for event in (await db.scalars(select(RuleChangeEvent).where(
            RuleChangeEvent.host_id == host.id, RuleChangeEvent.rule_uuid == rule_uuid)
            .order_by(RuleChangeEvent.observed_at.desc()).limit(limit))).all():
        items.append({
            "source": "observed", "at": event.observed_at, "kind": event.kind, "origin": event.origin, "actor": event.actor,
            "changes": diff_fields(event.before, event.after), "audit_id": event.audit_id,
        })
    audits = (await db.scalars(select(AuditEvent).where(
        AuditEvent.host_id == str(host.id), AuditEvent.kind.in_(AUDIT_KINDS),
        AuditEvent.details["remote_uuid"].as_string() == rule_uuid,
        AuditEvent.status.notin_(("requested",))).order_by(AuditEvent.created_at.desc()).limit(limit))).all()
    explained = {item["audit_id"] for item in items if item["audit_id"]}  # already shown as the observed change
    for audit in audits:
        if audit.id in explained:
            continue
        items.append({
            "source": "audit", "at": audit.created_at, "kind": audit.action, "status": audit.status, "actor": audit.actor,
            "origin": "opnexus", "message": audit.message, "audit_id": audit.id, "changes": [],
        })
    deployment = await db.scalar(select(RuleDeployment).where(
        RuleDeployment.host_id == host.id, RuleDeployment.opnsense_rule_uuid == rule_uuid))
    if deployment is not None:
        revisions = (await db.scalars(select(RuleRevision).where(RuleRevision.rule_id == deployment.rule_id)
                                      .order_by(RuleRevision.created_at.desc()).limit(limit))).all()
        ordered = sorted(revisions, key=lambda r: r.created_at)
        previous = None
        for revision in ordered:
            items.append({
                "source": "revision", "at": revision.created_at, "kind": "revision", "origin": "opnexus", "actor": None,
                "note": revision.note, "changes": diff_fields(previous, revision.snapshot) if previous is not None else [],
                "audit_id": None, "managed_rule_id": deployment.rule_id,
            })
            previous = revision.snapshot
    items.sort(key=lambda item: item["at"], reverse=True)
    return items[:limit]
