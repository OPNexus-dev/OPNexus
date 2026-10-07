"""Digest-based incremental firewall-log ingest. The single testable entry
point (poll_all_hosts) is called directly by main.py's background loop AND
directly by tests — same pattern as notification_service.run_notification_checks
and rule_deploy_service.sweep_expired_deployments.

Incremental fetch relies on a verified OPNsense core behaviour (see
opnsense_client.get_log / scripts/filter/read_log.py): passing the
previous poll's newest `__digest__` back as the `digest` query param makes
OPNsense return only strictly-newer lines, read in reverse-chronological
order. That means the first poll of a host (no cursor yet) is a plain
"latest N lines" fetch, and every poll after that only pulls what's new.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import math

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.log_cursor import HostLogCursor
from app.models.log_entry import FirewallLogEntry
from app.services import opnsense_client
from app.services.opnsense_client import OpnsenseAPIError

logger = logging.getLogger(__name__)

# Generous enough to cover a burst since the last poll at the default
# 30s interval without truncating, while still bounding a first-ever
# poll's backlog fetch (see docstring above) to something reasonable.
_FETCH_LIMIT = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _extract_rows(data: Any) -> list[dict]:
    """Same envelope handling the old live router used: OPNsense has been
    observed to return either a flat JSON array or a {"rows": [...]}
    envelope depending on version/config."""
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return data["rows"]
    if isinstance(data, list):
        return data
    return []


def _parse_timestamp(value: Any) -> datetime:
    """OPNsense's exact `__timestamp__` format was not part of this
    feature's core-source-verified fact set (only the digest-cursor
    semantics were) — since implemented, live-checked against both real
    hosts: it comes back as a bare "YYYY-MM-DDTHH:MM:SS" with NO timezone
    offset (e.g. "2026-09-08T12:45:05"), which `fromisoformat` parses fine
    but leaves naive. That naive value is stored as-is (Postgres then
    treats it as the session timezone, effectively "as if UTC").

    KNOWN LIMITATION: this is the firewall's own local clock as OPNsense
    reports it, not necessarily UTC — if the firewall isn't configured for
    UTC, every stored timestamp (and therefore since/until filtering) is
    off by that fixed offset. The API gives no separate offset field to
    correct for this. Documented in README rather than guessed at, since
    guessing a specific offset without confirming the host's configured
    timezone would just trade one unverified assumption for another.

    Also defensively accepts a numeric epoch and falls back to "now" (poll
    time) for anything unparseable, so one oddly-formatted line never
    aborts an entire ingest cycle."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return _now()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except ValueError:
                pass
    return _now()


def _map_row(host_id: uuid.UUID, row: dict) -> dict | None:
    digest = row.get("__digest__")
    if not digest:
        logger.warning("Log-Zeile ohne __digest__ übersprungen (host_id=%s)", host_id)
        return None
    return {
        "id": uuid.uuid4(),
        "host_id": host_id,
        "timestamp": _parse_timestamp(row.get("__timestamp__")),
        "action": row.get("action"),
        "interface": row.get("interface"),
        "direction": row.get("dir"),
        "protocol": row.get("protoname"),
        "source_net": row.get("src"),
        "source_port": row.get("srcport"),
        "destination_net": row.get("dst"),
        "destination_port": row.get("dstport"),
        "label": row.get("label"),
        "raw": row,
        "digest": str(digest),
    }


async def poll_host(db: AsyncSession, host: OpnsenseHost) -> int:
    """Fetch new log lines since the host's stored cursor, insert them
    (deduped via ON CONFLICT DO NOTHING on the (host_id, digest) unique
    constraint — the same postgresql insert().on_conflict_do_nothing()
    upsert pattern already used in routers/settings.py for the
    preferences upsert, just bulk), and advance the cursor to the newest
    fetched row's digest. Returns the number of newly inserted rows (0 is
    the normal/expected result on a quiet host with nothing new).

    Raises OpnsenseAPIError on request failure — callers (poll_all_hosts,
    tests) decide how to handle a single host's failure without aborting
    the whole cycle."""
    cursor = await db.get(HostLogCursor, host.id)
    last_digest = cursor.last_digest if cursor else None

    data = await opnsense_client.get_log(host, limit=_FETCH_LIMIT, digest=last_digest)
    rows = _extract_rows(data)

    if cursor is None:
        cursor = HostLogCursor(host_id=host.id)
        db.add(cursor)

    if not rows:
        cursor.last_polled_at = _now()
        return 0

    mapped = [m for m in (_map_row(host.id, row) for row in rows) if m is not None]
    actually_inserted = 0
    if mapped:
        stmt = insert(FirewallLogEntry).values(mapped).on_conflict_do_nothing(
            index_elements=["host_id", "digest"]
        )
        result = await db.execute(stmt)
        # rowcount reflects rows the ON CONFLICT DO NOTHING clause actually
        # inserted, not the number attempted — an overlapping re-fetch of
        # an already-ingested digest correctly counts as 0 here.
        actually_inserted = result.rowcount or 0
        # Newest by parsed timestamp rather than "first in the response" —
        # a defensive choice independent of trusting the API's ordering,
        # in case it's ever not strictly reverse-chronological.
        newest = max(mapped, key=lambda m: m["timestamp"])
        cursor.last_digest = newest["digest"]

    cursor.last_polled_at = _now()
    return actually_inserted


async def poll_all_hosts(db: AsyncSession) -> dict:
    """Polls every enabled host independently — one host's failure (e.g.
    unreachable firewall) must not block ingest for the others, same
    principle as host_status_service.get_statuses."""
    hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)))).all())
    inserted_total = 0
    failed = 0
    for host in hosts:
        try:
            inserted_total += await poll_host(db, host)
        except OpnsenseAPIError as exc:
            failed += 1
            logger.warning("Log-Ingest für Host %s fehlgeschlagen: %s", host.name, exc)
        except Exception:
            failed += 1
            logger.exception("Log-Ingest für Host %s fehlgeschlagen", host.name)
    await db.commit()
    return {"hosts_polled": len(hosts), "hosts_failed": failed, "inserted": inserted_total}


async def prune_old_entries(db: AsyncSession, retention_days: int) -> int:
    """Deletes log entries older than the retention window. Unlike
    audit_events (models/audit.py), firewall_log_entries carries no
    immutability trigger — pruning is a core requirement here, not
    something to guard against."""
    cutoff = _now() - timedelta(days=retention_days)
    result = await db.execute(delete(FirewallLogEntry).where(FirewallLogEntry.timestamp < cutoff))
    await db.commit()
    return result.rowcount or 0


# After a size-based prune the table is brought to this fraction of the cap,
# so the next cycles do not delete a handful of rows each time.
_SIZE_PRUNE_TARGET = 0.95
# One pass never deletes more than this; the next prune cycle continues.
_SIZE_PRUNE_MAX_ROWS = 500_000


async def storage_usage(db: AsyncSession) -> dict[str, int]:
    """Disk footprint of the log table (incl. indexes) from Postgres statistics.
    `live_bytes` scales that by the share of live tuples: Postgres keeps the
    file size after DELETE (space is reused, not returned), so the raw size
    alone would never fall below the cap again."""
    row = (await db.execute(text(
        "SELECT pg_total_relation_size('firewall_log_entries') AS total, "
        "COALESCE(s.n_live_tup, 0) AS live, COALESCE(s.n_dead_tup, 0) AS dead "
        "FROM (SELECT 1) AS one "
        "LEFT JOIN pg_stat_user_tables s ON s.relname = 'firewall_log_entries'"
    ))).one()
    total, live, dead = int(row.total), int(row.live), int(row.dead)
    tuples = live + dead
    live_bytes = int(total * live / tuples) if tuples else 0
    per_entry = int(total / tuples) if tuples else 0
    return {"used_bytes": total, "live_bytes": live_bytes, "entries": live, "bytes_per_entry": per_entry}


async def enforce_size_limit(db: AsyncSession, max_size_gb: float) -> int:
    """Deletes the oldest entries while the estimated live data exceeds the cap.
    max_size_gb <= 0 means unlimited."""
    if max_size_gb <= 0:
        return 0
    limit = int(max_size_gb * 1024**3)
    usage = await storage_usage(db)
    if usage["live_bytes"] <= limit or usage["bytes_per_entry"] <= 0:
        return 0
    excess = usage["live_bytes"] - int(limit * _SIZE_PRUNE_TARGET)
    to_delete = min(math.ceil(excess / usage["bytes_per_entry"]), _SIZE_PRUNE_MAX_ROWS)
    oldest = select(FirewallLogEntry.id).order_by(FirewallLogEntry.timestamp.asc()).limit(to_delete)
    result = await db.execute(delete(FirewallLogEntry).where(FirewallLogEntry.id.in_(oldest)))
    await db.commit()
    return result.rowcount or 0
