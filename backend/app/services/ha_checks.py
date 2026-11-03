"""The six HA alarms (docs/HA-H2-DESIGN.md 3.8, plus the agent alarm of docs/DESIGN-HA-UI.md), checked by the leader. Without HA (no HA_NODE/HA_PEER_IP) all of them
return nothing. They work on a snapshot of the primary's database (``ha_service.collect``); an unmeasurable value
never raises or resolves an alarm."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app import leader
from app.config import settings
from app.database import SessionLocal
from app.services import ha_service, updater_client
from app.services.notification_dedup import Problems

STANDBY_DOWN_AFTER = timedelta(minutes=5)
LAG_BYTES = 64 * 1024 * 1024
LAG_SECONDS = 60.0
SLOT_RETENTION_SHARE = 0.5
SYNC_STALE_AFTER = timedelta(hours=1)
AGENT_DOWN_AFTER = timedelta(minutes=5)
_agent_down_since: dict[str, datetime] = {}   # "own" / "partner" -> first failed look of an unbroken run (in memory, a restart starts the clock anew)
RESYNC_HINT = "Standby neu aufbauen: am Standby 'opnexus ha resync'."


def _mb(value: int | None) -> float | None:
    return None if value is None else round(value / 1048576, 1)


def _age(since: datetime, now: datetime) -> str:
    seconds = int((now - since).total_seconds())
    return f"{seconds // 3600} h {seconds % 3600 // 60} min" if seconds >= 3600 else f"{seconds // 60} min"


async def _primary_snapshot(db: AsyncSession) -> ha_service.Snapshot | None:
    """The shared snapshot of this cycle, if this node is a configured primary (None otherwise)."""
    if not ha_service.configured():
        return None
    snap = await ha_service.collect_shared()
    return snap if snap.db_role == "primary" or not snap.complete else None


def _unmeasurable(prefix: str) -> Problems:
    """A query failed this round: nothing is raised, and an alarm that is already open stays as it is."""
    keep = Problems()
    keep.preserve_prefixes.add(prefix)
    return keep


async def _mark_seen() -> None:
    async with SessionLocal() as own:
        await ha_service.mark_standby_seen(own)


async def check_standby_down(db: AsyncSession) -> dict[str, dict]:
    """The standby has not been streaming for 5 minutes (counted from when it was last seen, or from the last promote)."""
    snap = await _primary_snapshot(db)
    if snap is None:
        return {}
    if not snap.complete:
        return _unmeasurable("standby")
    if snap.replication_state == "streaming":
        await _mark_seen()
        return {}
    now = datetime.now(timezone.utc)
    seen = [t for t in (snap.standby_seen_at, snap.promoted_at) if t is not None]
    if not seen:  # never seen yet: the clock starts now
        await _mark_seen()
        return {}
    since = max(seen)
    if now - since < STANDBY_DOWN_AFTER:
        problems = Problems()
        problems.preserve_prefixes.add("standby")  # inside the grace window: leave an open problem as it is
        return problems
    return {"standby": {
        "knoten": settings.ha_node, "partner": settings.ha_peer_ip, "zustand": snap.replication_state or "kein Walsender",
        "seit": since.isoformat(timespec="seconds"), "dauer": _age(since, now),
        "hinweis": "Der Standby streamt nicht mehr: 'opnexus ha status' zeigt, warum. Nach einem Wechsel den alten Primär "
                   "mit 'opnexus ha resync' zurückholen.",
    }}


async def check_replication_lag(db: AsyncSession) -> dict[str, dict]:
    """The standby streams but is more than 64 MB or 60 s behind."""
    snap = await _primary_snapshot(db)
    if snap is None:
        return {}
    if not snap.complete:
        return _unmeasurable("standby")
    if snap.replication_state not in ("streaming", "catchup"):
        return {}
    late_bytes = (snap.replication_lag_bytes or 0) > LAG_BYTES
    late_seconds = (snap.replication_lag_seconds or 0) > LAG_SECONDS
    if not (late_bytes or late_seconds):
        return {}
    return {"standby": {
        "knoten": settings.ha_node, "partner": settings.ha_peer_ip, "zustand": snap.replication_state,
        "verzug_mb": _mb(snap.replication_lag_bytes), "verzug_s": None if snap.replication_lag_seconds is None else round(snap.replication_lag_seconds, 1),
        "hinweis": "Bei einem Wechsel in dieser Lücke gehen Änderungen verloren. Netz und Platte des Standbys prüfen.",
    }}


async def check_slot_retention(db: AsyncSession) -> dict[str, dict]:
    """The replication slot holds back more than half of max_slot_wal_keep_size, or is gone/lost (the standby must be rebuilt)."""
    snap = await _primary_snapshot(db)
    if snap is None:
        return {}
    if not snap.complete:
        return _unmeasurable("slot")
    if not snap.slot_present:
        return {"slot": {"knoten": settings.ha_node, "zustand": "fehlt", "hinweis": RESYNC_HINT}}
    if snap.slot_wal_status == "lost":
        return {"slot": {"knoten": settings.ha_node, "zustand": "verloren (Obergrenze überschritten)", "hinweis": RESYNC_HINT}}
    if snap.slot_limit_bytes and (snap.slot_retained_bytes or 0) > snap.slot_limit_bytes * SLOT_RETENTION_SHARE:
        return {"slot": {
            "knoten": settings.ha_node, "zustand": snap.slot_wal_status, "zurueckgehalten_mb": _mb(snap.slot_retained_bytes),
            "obergrenze_mb": _mb(snap.slot_limit_bytes),
            "hinweis": "Der Standby holt WAL nicht ab. Bei Überschreiten der Obergrenze geht der Slot verloren und der Standby "
                       "muss neu aufgebaut werden.",
        }}
    return {}


async def check_sync_stale(db: AsyncSession) -> dict[str, dict]:
    """`opnexus ha sync` (the agent's timer or the cron, every 5 minutes) has not succeeded for an hour: the standby's secrets are old."""
    snap = await _primary_snapshot(db)
    if snap is None:
        return {}
    now = datetime.now(timezone.utc)
    if snap.last_sync_at is not None and now - snap.last_sync_at <= SYNC_STALE_AFTER:
        return {}
    return {"sync": {
        "knoten": settings.ha_node, "letzter_abgleich": snap.last_sync_at.isoformat(timespec="seconds") if snap.last_sync_at else "nie",
        "alter": _age(snap.last_sync_at, now) if snap.last_sync_at else None,
        "hinweis": "Firewall-Zugänge und 2FA-Geheimnisse neuer Objekte fehlen am Standby. Agent (Updater-Container, Log ha-timer.log) "
                   "bzw. Cron und SSH prüfen ('opnexus ha sync' von Hand).",
    }}


def _agent_marker() -> Path:
    """The updater (the agent) keeps this file in its state volume for as long as this node is paired with an agent: only then is a silent
    agent a fault (a pair set up over SSH, or a node whose updater was never enabled, has none and never alarms)."""
    return Path(settings.updater_token_file).parent / "ha-paired"


async def check_agent_down(db: AsyncSession) -> dict[str, dict]:
    """The agent of this node or of the partner has not answered for 5 minutes. Without the agent a takeover from the browser, the
    secrets sync (every 5 minutes) and the VIP follow-up do not work, and nothing else would tell."""
    snap = await _primary_snapshot(db)
    if snap is None or not snap.complete:
        return {}
    if not _agent_marker().exists():
        _agent_down_since.clear()
        return {}
    now = datetime.now(timezone.utc)
    try:
        status = await updater_client.ha_local_status()
    except (updater_client.UpdaterUnavailable, updater_client.UpdaterRejected):
        # The partner is asked through this node's own agent: while that is silent nothing is known about the partner's.
        _agent_down_since.setdefault("own", now)
        _agent_down_since.pop("partner", None)   # an earlier look at the partner is stale now
    else:
        _agent_down_since.pop("own", None)
        if status.get("peer_reachable") is False:   # None: not asked (not paired), not a fault
            _agent_down_since.setdefault("partner", now)
        else:
            _agent_down_since.pop("partner", None)
    if not _agent_down_since:
        return {}
    key, since = min(_agent_down_since.items(), key=lambda item: item[1])
    if now - since < AGENT_DOWN_AFTER:
        problems = Problems()
        problems.preserve_prefixes.add("agent")   # inside the grace window: an open alarm stays as it is
        return problems
    who = "dieser Knoten" if key == "own" else "der Partner"
    return {"agent": {
        "knoten": settings.ha_node, "partner": settings.ha_peer_ip, "agent_antwortet_nicht": who,
        "seit": since.isoformat(timespec="seconds"), "dauer": _age(since, now),
        "hinweis": "Ohne den Agenten (Updater-Container) gehen Übernahme im Browser, Geheimnis-Abgleich und VIP-Nachführung nicht. "
                   "'docker compose ps updater ha-gate' prüfen, Port 8445 zwischen den Knoten.",
    }}


async def check_split_brain(db: AsyncSession) -> dict[str, dict]:
    """The partner node is running as a second primary and its guard has fenced it (or both claim leadership)."""
    if not ha_service.configured():
        return {}
    gate = leader.gate
    if gate is not None and gate.partner_probed:
        view = gate.partner_view  # the gate asks the partner every 30 s anyway
    else:
        probe = leader.http_partner(settings.ha_peer_ip)
        if probe is None:
            return {}
        view = await probe()
    if view is None:
        unknown = Problems()
        unknown.preserve_prefixes.add("partner")  # unreachable: say nothing new, keep an open alarm open
        return unknown
    role, epoch = view
    if role not in (leader.FENCED, leader.LEADER):
        return {}
    own = (await ha_service.collect_shared()).epoch
    return {"partner": {
        "partner": settings.ha_peer_ip, "rolle_partner": role, "epoche_partner": epoch, "epoche_eigene": own,
        "hinweis": "Der Partner läuft ohne Fencing als zweiter Primär; sein Wächter hat die Hintergrunddienste gestoppt. "
                   "Partner anhalten und mit 'opnexus ha resync' neu aufbauen, bevor Änderungen verloren gehen.",
    }}
