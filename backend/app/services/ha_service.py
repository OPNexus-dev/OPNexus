"""High availability state as the backend sees it (docs/HA-H2-DESIGN.md 3.5, 3.6, 3.9).

The backend only reads: `opnexus ha promote` writes the epoch. Without HA the row stays at epoch 0 and none of this
has any effect."""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import SessionLocal
from app.models.ha_state import HaState

logger = logging.getLogger(__name__)

# After a promote, overdue rollout steps are postponed instead of dropped for this long (see scheduled_maintenance).
PROMOTE_GRACE = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_state(db: AsyncSession) -> HaState | None:
    return await db.get(HaState, 1)


async def own_epoch(db: AsyncSession) -> int:
    row = await get_state(db)
    return row.epoch if row is not None else 0


async def grace_ends_at(db: AsyncSession) -> datetime | None:
    """When the grace period after the last promote ends, or None if it is over (or there never was a promote)."""
    row = await get_state(db)
    if row is None or row.promoted_at is None:
        return None
    end = row.promoted_at + PROMOTE_GRACE
    return end if end > _now() else None


async def last_grace_end(db: AsyncSession) -> datetime | None:
    """End of the grace period of the last promote, past or future (None: never promoted)."""
    row = await get_state(db)
    return row.promoted_at + PROMOTE_GRACE if row is not None and row.promoted_at is not None else None


SLOT_NAME = "opnexus_standby"


def configured() -> bool:
    """This installation is one node of an HA pair (`opnexus ha init` passes HA_NODE and HA_PEER_IP to the backend)."""
    return bool(settings.ha_node.strip() and settings.ha_peer_ip.strip())


@dataclass
class Snapshot:
    """What this node's database says about the pair. Everything not measurable right now is None."""
    db_role: str | None = None  # primary | replica
    epoch: int = 0
    primary_node: str | None = None
    promoted_at: datetime | None = None
    last_sync_at: datetime | None = None
    standby_seen_at: datetime | None = None
    replication_state: str | None = None  # primary: state of the standby's walsender (streaming, catchup, ...); replica: wal receiver status
    replication_lag_bytes: int | None = None
    replication_lag_seconds: float | None = None
    slot_present: bool = False
    slot_active: bool | None = None
    slot_wal_status: str | None = None
    slot_retained_bytes: int | None = None
    slot_limit_bytes: int | None = None  # None: no limit (-1 in PostgreSQL)
    complete: bool = True  # False if a statistics query failed: "missing" values may then be "not measured"


_FAILED = object()


async def _one(db: AsyncSession, sql: str, **params):
    """The first row, None for no row, _FAILED if the query raised (the session stays usable)."""
    try:
        return (await db.execute(text(sql), params)).first()
    except Exception:
        await db.rollback()
        return _FAILED


async def collect(db: AsyncSession) -> Snapshot:
    snap = Snapshot()
    row = await _one(db, "SELECT pg_is_in_recovery()")
    if row is None or row is _FAILED:
        snap.complete = False
        return snap
    snap.db_role = "replica" if row[0] else "primary"
    state = await get_state(db)
    if state is not None:
        snap.epoch, snap.primary_node, snap.promoted_at = state.epoch, state.primary_node, state.promoted_at
        snap.last_sync_at, snap.standby_seen_at = state.last_sync_at, state.standby_seen_at
    if snap.db_role == "replica":
        row = await _one(db, "SELECT status FROM pg_stat_wal_receiver")
        snap.complete = row is not _FAILED
        snap.replication_state = row[0] if row and row is not _FAILED else None
        return snap
    row = await _one(
        db,
        "SELECT state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint, extract(epoch from replay_lag)::float "
        "FROM pg_stat_replication WHERE application_name = 'walreceiver' ORDER BY state = 'streaming' DESC LIMIT 1")
    if row is _FAILED:
        snap.complete = False
    elif row is not None:
        snap.replication_state, snap.replication_lag_bytes, snap.replication_lag_seconds = row[0], row[1], row[2]
    row = await _one(
        db,
        "SELECT active, wal_status, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint "
        "FROM pg_replication_slots WHERE slot_name = :slot", slot=SLOT_NAME)
    if row is _FAILED:
        snap.complete = False
    elif row is not None:
        snap.slot_present, snap.slot_active, snap.slot_wal_status, snap.slot_retained_bytes = True, row[0], row[1], row[2]
    row = await _one(db, "SELECT setting::bigint FROM pg_settings WHERE name = 'max_slot_wal_keep_size'")
    if row is _FAILED:
        snap.complete = False
    elif row is not None and row[0] is not None and row[0] >= 0:
        snap.slot_limit_bytes = int(row[0]) * 1024 * 1024
    return snap


_shared: tuple[float, Snapshot] | None = None
SHARED_TTL_SECONDS = 10.0


async def collect_shared() -> Snapshot:
    """One snapshot for everything that asks within a few seconds (the five alarms of one notification cycle), taken in a
    session of its own: a failing statistics query rolls its session back, and that must not touch the caller's work."""
    global _shared
    now = time.monotonic()
    if _shared is not None and now - _shared[0] < SHARED_TTL_SECONDS:
        return _shared[1]
    async with SessionLocal() as own:
        snap = await collect(own)
    _shared = (now, snap)
    return snap


async def mark_standby_seen(db: AsyncSession) -> None:
    """The leader saw the standby streaming: start the "no standby for N minutes" clock anew."""
    await db.execute(text(
        "INSERT INTO ha_state (id, epoch, standby_seen_at, updated_at) VALUES (1, 0, now(), now()) "
        "ON CONFLICT (id) DO UPDATE SET standby_seen_at = now()"))
    await db.commit()
