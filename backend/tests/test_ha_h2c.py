"""H2c (docs/HA-H2-DESIGN.md 3.8, 3.10): the five HA alarms, metrics, the read-only API behind the card in Settings › System.

The alarms work on a snapshot of the primary's database. A test database has no standby, so the decisions are tested with
crafted snapshots and the SQL itself (`ha_service.collect`) against the real test database."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app import leader
from app.config import settings
from app.database import SessionLocal, engine
from app.models.ha_state import HaState
from app.models.notification import NotificationProblem
from app.services import ha_checks, ha_service, notification_service
from app.services.notification_dedup import Problems
from tests.test_updater import _bump, _feed, _setup, _updater_status  # noqa: F401 (_setup: autouse fixture of the updater tests)
from tests.test_users import _login_as

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _fresh_snapshot_cache(monkeypatch):
    monkeypatch.setattr(ha_service, "_shared", None)
    monkeypatch.setattr(leader, "gate", None)


@pytest.fixture
def ha(monkeypatch):
    """This installation is node "a" of a pair with the partner at 192.168.1.21."""
    monkeypatch.setattr(settings, "ha_node", "a")
    monkeypatch.setattr(settings, "ha_peer_ip", "192.168.1.21")
    monkeypatch.setattr(settings, "ha_vip", "192.168.1.22/24")


def _snap(monkeypatch, **fields):
    snap = ha_service.Snapshot(db_role=fields.pop("db_role", "primary"), **fields)

    async def collect(db):
        return snap

    monkeypatch.setattr(ha_service, "collect", collect)
    monkeypatch.setattr(ha_service, "_shared", None)  # a new snapshot replaces the shared one of the previous step
    return snap


async def _run(check):
    async with SessionLocal() as db:
        return await check(db)


ALL_CHECKS = [ha_checks.check_standby_down, ha_checks.check_replication_lag, ha_checks.check_slot_retention,
              ha_checks.check_sync_stale, ha_checks.check_split_brain]


@pytest.mark.asyncio
@pytest.mark.parametrize("check", ALL_CHECKS)
async def test_without_ha_every_alarm_is_silent(check, monkeypatch):
    monkeypatch.setattr(settings, "ha_node", "")
    monkeypatch.setattr(settings, "ha_peer_ip", "")
    assert not ha_service.configured()
    assert await _run(check) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("check", [c for c in ALL_CHECKS if c is not ha_checks.check_split_brain])
async def test_a_replica_raises_no_database_alarm(check, ha, monkeypatch):
    _snap(monkeypatch, db_role="replica", replication_state=None)
    assert await _run(check) == {}


# ── standby down ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_streaming_standby_is_no_problem_and_restarts_the_clock(ha, monkeypatch):
    _snap(monkeypatch, replication_state="streaming")
    assert await _run(ha_checks.check_standby_down) == {}
    async with SessionLocal() as db:
        assert (await db.get(HaState, 1)).standby_seen_at is not None


@pytest.mark.asyncio
async def test_no_standby_for_five_minutes_is_a_problem_with_the_duration(ha, monkeypatch):
    seen = datetime.now(timezone.utc) - timedelta(minutes=7)
    _snap(monkeypatch, replication_state=None, standby_seen_at=seen)
    problems = await _run(ha_checks.check_standby_down)
    assert set(problems) == {"standby"}
    ctx = problems["standby"]
    assert ctx["knoten"] == "a" and ctx["partner"] == "192.168.1.21" and ctx["zustand"] == "kein Walsender"
    assert "7 min" in ctx["dauer"] and "ha resync" in ctx["hinweis"]


@pytest.mark.asyncio
async def test_a_short_gap_keeps_an_open_alarm_open_but_raises_none(ha, monkeypatch):
    _snap(monkeypatch, replication_state=None, standby_seen_at=datetime.now(timezone.utc) - timedelta(minutes=2))
    problems = await _run(ha_checks.check_standby_down)
    assert dict(problems) == {} and isinstance(problems, Problems) and "standby" in problems.preserve_prefixes


@pytest.mark.asyncio
async def test_a_fresh_promote_counts_from_the_promote_not_from_the_old_sighting(ha, monkeypatch):
    now = datetime.now(timezone.utc)
    _snap(monkeypatch, replication_state=None, standby_seen_at=now - timedelta(days=3), promoted_at=now - timedelta(minutes=1))
    assert dict(await _run(ha_checks.check_standby_down)) == {}
    _snap(monkeypatch, replication_state=None, standby_seen_at=now - timedelta(days=3), promoted_at=now - timedelta(minutes=9))
    assert set(await _run(ha_checks.check_standby_down)) == {"standby"}


@pytest.mark.asyncio
async def test_a_pair_never_seen_streaming_starts_its_clock_now(ha, monkeypatch):
    _snap(monkeypatch, replication_state=None)
    assert await _run(ha_checks.check_standby_down) == {}
    async with SessionLocal() as db:
        assert (await db.get(HaState, 1)).standby_seen_at is not None


# ── lag ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("lag_bytes,lag_seconds,raised", [
    (65 * MB, 0.1, True), (1 * MB, 61.0, True), (64 * MB, 60.0, False), (0, 0.0, False), (None, None, False),
])
async def test_replication_lag_over_64_mb_or_60_s_is_a_problem(lag_bytes, lag_seconds, raised, ha, monkeypatch):
    _snap(monkeypatch, replication_state="streaming", replication_lag_bytes=lag_bytes, replication_lag_seconds=lag_seconds)
    problems = await _run(ha_checks.check_replication_lag)
    assert (set(problems) == {"standby"}) is raised
    if raised:
        assert problems["standby"]["zustand"] == "streaming"


@pytest.mark.asyncio
async def test_a_standby_that_does_not_stream_is_the_other_alarms_business(ha, monkeypatch):
    _snap(monkeypatch, replication_state=None, replication_lag_bytes=999 * MB)
    assert await _run(ha_checks.check_replication_lag) == {}


# ── slot ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_missing_or_lost_slot_means_rebuild_the_standby(ha, monkeypatch):
    _snap(monkeypatch, slot_present=False)
    problems = await _run(ha_checks.check_slot_retention)
    assert problems["slot"]["zustand"] == "fehlt" and "ha resync" in problems["slot"]["hinweis"]
    _snap(monkeypatch, slot_present=True, slot_wal_status="lost", slot_retained_bytes=0, slot_limit_bytes=4096 * MB)
    problems = await _run(ha_checks.check_slot_retention)
    assert "verloren" in problems["slot"]["zustand"] and "ha resync" in problems["slot"]["hinweis"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retained,limit,raised", [
    (2049 * MB, 4096 * MB, True), (2048 * MB, 4096 * MB, False), (10 * MB, 4096 * MB, False), (9999 * MB, None, False),
])
async def test_a_slot_holding_back_more_than_half_the_limit_is_a_problem(retained, limit, raised, ha, monkeypatch):
    _snap(monkeypatch, slot_present=True, slot_wal_status="reserved", slot_retained_bytes=retained, slot_limit_bytes=limit)
    problems = await _run(ha_checks.check_slot_retention)
    assert (set(problems) == {"slot"}) is raised
    if raised:
        assert problems["slot"]["zurueckgehalten_mb"] == 2049.0 and problems["slot"]["obergrenze_mb"] == 4096.0


# ── sync ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_sync_older_than_an_hour_or_never_is_a_problem(ha, monkeypatch):
    now = datetime.now(timezone.utc)
    _snap(monkeypatch, last_sync_at=now - timedelta(minutes=59))
    assert await _run(ha_checks.check_sync_stale) == {}
    _snap(monkeypatch, last_sync_at=now - timedelta(hours=2, minutes=5))
    ctx = (await _run(ha_checks.check_sync_stale))["sync"]
    assert ctx["alter"] == "2 h 5 min" and "ha sync" in ctx["hinweis"]
    _snap(monkeypatch, last_sync_at=None)
    ctx = (await _run(ha_checks.check_sync_stale))["sync"]
    assert ctx["letzter_abgleich"] == "nie" and ctx["alter"] is None


# ── split brain ───────────────────────────────────────────────────────────────

def _partner(monkeypatch, view):
    async def probe():
        return view

    monkeypatch.setattr(leader, "http_partner", lambda ip: probe)


@pytest.mark.asyncio
@pytest.mark.parametrize("view,raised", [
    (("fenced", 2), True), (("leader", 2), True), (("replica", 2), False), (("standby", 1), False), (("takeover", 1), False),
])
async def test_a_fenced_or_second_leading_partner_is_split_brain(view, raised, ha, monkeypatch):
    _snap(monkeypatch, epoch=3)
    _partner(monkeypatch, view)
    problems = await _run(ha_checks.check_split_brain)
    assert (set(problems) == {"partner"}) is raised
    if raised:
        assert problems["partner"]["rolle_partner"] == view[0] and problems["partner"]["epoche_eigene"] == 3
        assert "resync" in problems["partner"]["hinweis"]


@pytest.mark.asyncio
async def test_an_unreachable_partner_keeps_an_open_split_brain_alarm_open(ha, monkeypatch):
    _snap(monkeypatch)
    _partner(monkeypatch, None)
    problems = await _run(ha_checks.check_split_brain)
    assert dict(problems) == {} and "partner" in problems.preserve_prefixes


@pytest.mark.asyncio
async def test_split_brain_check_is_silent_when_the_peer_address_is_unusable(ha, monkeypatch):
    monkeypatch.setattr(settings, "ha_peer_ip", "not-an-ip")
    _snap(monkeypatch)
    assert await _run(ha_checks.check_split_brain) == {}


# ── the SQL against a real (standby-less) database ───────────────────────────

@pytest.mark.asyncio
async def test_collect_on_a_primary_without_a_standby():
    async with SessionLocal() as db:
        snap = await ha_service.collect(db)
    assert snap.db_role == "primary" and snap.epoch == 0
    assert snap.replication_state is None and snap.replication_lag_bytes is None and snap.slot_present is False


@pytest.mark.asyncio
async def test_collect_reads_epoch_timestamps_and_the_slot():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    async with SessionLocal() as db:
        db.add(HaState(id=1, epoch=4, primary_node="b", promoted_at=now, last_sync_at=now, standby_seen_at=now))
        await db.commit()
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("SELECT pg_create_physical_replication_slot(:s, true)"), {"s": ha_service.SLOT_NAME})
    try:
        async with SessionLocal() as db:
            snap = await ha_service.collect(db)
    finally:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("SELECT pg_drop_replication_slot(:s)"), {"s": ha_service.SLOT_NAME})
    assert (snap.epoch, snap.primary_node, snap.promoted_at, snap.last_sync_at, snap.standby_seen_at) == (4, "b", now, now, now)
    assert snap.slot_present is True and snap.slot_active is False and snap.slot_wal_status in ("reserved", "extended")
    assert snap.slot_retained_bytes is not None and snap.slot_retained_bytes >= 0


@pytest.mark.asyncio
async def test_a_failing_query_is_marked_as_failed_and_leaves_the_session_usable(monkeypatch):
    async with SessionLocal() as db:
        assert await ha_service._one(db, "SELECT * FROM table_that_does_not_exist") is ha_service._FAILED
        assert (await db.execute(text("SELECT 1"))).scalar() == 1  # the session is usable afterwards


# ── through the notification engine ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_engine_opens_and_resolves_an_ha_problem_and_the_switch_stops_new_ones(client, ha, monkeypatch):
    snap = _snap(monkeypatch, replication_state="streaming", replication_lag_bytes=200 * MB)
    _partner(monkeypatch, None)
    async with SessionLocal() as db:
        result = await notification_service.run_notification_checks(db)
    assert result["opened"] >= 1
    async with SessionLocal() as db:
        rows = [p for p in (await db.scalars(__import__("sqlalchemy").select(NotificationProblem))).all() if p.problem_type == "ha_replication_lag"]
    assert len(rows) == 1 and rows[0].status == "open" and rows[0].context["verzug_mb"] == 200.0
    from app.services import notification_delivery
    subject, body = notification_delivery.render_message(rows[0], "opened")
    assert subject == "OPNexus: Hochverfügbarkeit: Replikationsverzug" and "verzug_mb: 200.0" in body
    snap.replication_lag_bytes = 0
    async with SessionLocal() as db:
        await notification_service.run_notification_checks(db)
    async with SessionLocal() as db:
        assert all(p.status == "resolved" for p in (await db.scalars(__import__("sqlalchemy").select(NotificationProblem))).all()
                   if p.problem_type == "ha_replication_lag")
    # switched off: nothing new
    assert (await client.put("/api/notifications/settings", json={"ha_alerts_enabled": False})).json()["ha_alerts_enabled"] is False
    snap.replication_lag_bytes = 500 * MB
    async with SessionLocal() as db:
        await notification_service.run_notification_checks(db)
        opened = [p for p in (await db.scalars(__import__("sqlalchemy").select(NotificationProblem))).all()
                  if p.problem_type == "ha_replication_lag" and p.status == "open"]
    assert opened == []


@pytest.mark.asyncio
async def test_the_ha_alert_switch_is_on_by_default(client):
    assert (await client.get("/api/notifications/settings")).json()["ha_alerts_enabled"] is True


# ── metrics ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_carry_the_ha_family_only_with_ha(client, ha, monkeypatch):
    leader.gate = None
    now = datetime.now(timezone.utc)
    _snap(monkeypatch, epoch=5, replication_state="streaming", replication_lag_bytes=1234, slot_present=True,
          slot_retained_bytes=4096, last_sync_at=now - timedelta(seconds=90))
    text_ = (await client.get("/api/metrics")).text
    assert 'opnexus_ha_role{node="a",role="primary"} 1' in text_
    assert "opnexus_ha_epoch 5" in text_
    assert "opnexus_ha_replication_lag_bytes 1234" in text_
    assert "opnexus_ha_slot_retained_bytes 4096" in text_
    assert "opnexus_ha_split_brain 0" in text_
    sync_line = next(ln for ln in text_.splitlines() if ln.startswith("opnexus_ha_sync_age_seconds "))
    assert 89 <= int(sync_line.split()[1]) <= 100
    monkeypatch.setattr(settings, "ha_node", "")
    assert "opnexus_ha" not in (await client.get("/api/metrics")).text


@pytest.mark.asyncio
async def test_metrics_report_a_fenced_node(client, ha, monkeypatch):
    _snap(monkeypatch, epoch=1)
    gate = leader.LeaderGate({})
    gate.split_brain = True
    monkeypatch.setattr(leader, "gate", gate)
    assert "opnexus_ha_split_brain 1" in (await client.get("/api/metrics")).text


# ── API behind the card ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_ha_card_endpoint_is_admin_only_and_empty_without_ha(client, monkeypatch):
    monkeypatch.setattr(settings, "ha_node", "")
    assert (await client.get("/api/system/ha")).json() == {"configured": False}
    name = f"viewer-{uuid.uuid4().hex[:6]}"
    assert (await client.post("/api/users", json={"username": name, "password": "viewer-password-123", "role": "read_only"})).status_code == 201
    viewer = await _login_as(name, "viewer-password-123")
    try:
        assert (await viewer.get("/api/system/ha")).status_code == 403
    finally:
        await viewer.aclose()


@pytest.mark.asyncio
async def test_the_ha_card_endpoint_shows_the_pair(client, ha, monkeypatch):
    now = datetime.now(timezone.utc)
    _snap(monkeypatch, epoch=2, primary_node="a", promoted_at=now, last_sync_at=now - timedelta(seconds=40),
          replication_state="streaming", replication_lag_bytes=0, replication_lag_seconds=0.2,
          slot_present=True, slot_active=True, slot_wal_status="reserved", slot_retained_bytes=2048, slot_limit_bytes=4096 * MB)
    _partner(monkeypatch, None)
    body = (await client.get("/api/system/ha")).json()
    assert body["configured"] is True and body["node"] == "a" and body["peer_ip"] == "192.168.1.21" and body["vip"] == "192.168.1.22"
    assert body["db_role"] == "primary" and body["epoch"] == 2 and body["primary_node"] == "a" and body["split_brain"] is False
    assert body["replication"] == {"state": "streaming", "lag_bytes": 0, "lag_seconds": 0.2}
    assert body["slot"]["limit_bytes"] == 4096 * MB and body["slot"]["active"] is True
    assert 39 <= body["sync_age_seconds"] <= 50
    assert body["peer"] == {"reachable": False, "role": None, "epoch": None}
    _partner(monkeypatch, ("fenced", 1))
    assert (await client.get("/api/system/ha")).json()["peer"] == {"reachable": True, "role": "fenced", "epoch": 1}


# ── click updates ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_node_of_a_pair_cannot_click_update(client, respx_mock, ha):
    _feed(respx_mock, _bump(minor=1))
    _updater_status(respx_mock)
    body = (await client.get("/api/system/updater")).json()
    assert body["ha"] is True and body["can_update"] is False


@pytest.mark.asyncio
async def test_the_updater_reporting_ha_also_blocks_the_button(client, respx_mock, monkeypatch):
    monkeypatch.setattr(settings, "ha_node", "")
    _feed(respx_mock, _bump(minor=1))
    _updater_status(respx_mock, ha_role="primary")
    body = (await client.get("/api/system/updater")).json()
    assert body["ha"] is False and body["can_update"] is False


@pytest.mark.asyncio
async def test_without_ha_the_same_status_offers_the_update(client, respx_mock, monkeypatch):
    monkeypatch.setattr(settings, "ha_node", "")
    _feed(respx_mock, _bump(minor=1))
    _updater_status(respx_mock)
    assert (await client.get("/api/system/updater")).json()["can_update"] is True


# ── review fixes ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("check,prefix", [
    (ha_checks.check_standby_down, "standby"), (ha_checks.check_replication_lag, "standby"), (ha_checks.check_slot_retention, "slot"),
])
async def test_a_snapshot_with_a_failed_query_raises_nothing_and_keeps_open_alarms_open(check, prefix, ha, monkeypatch):
    _snap(monkeypatch, replication_state=None, slot_present=False, complete=False,
          standby_seen_at=datetime.now(timezone.utc) - timedelta(hours=1))
    problems = await _run(check)
    assert dict(problems) == {} and isinstance(problems, Problems) and prefix in problems.preserve_prefixes


@pytest.mark.asyncio
async def test_collect_marks_the_snapshot_incomplete_when_a_statistics_query_fails(monkeypatch):
    real = ha_service._one

    async def flaky(db, sql, **params):
        if "pg_replication_slots" in sql:
            return ha_service._FAILED
        return await real(db, sql, **params)

    monkeypatch.setattr(ha_service, "_one", flaky)
    async with SessionLocal() as db:
        snap = await ha_service.collect(db)
    assert snap.db_role == "primary" and snap.complete is False and snap.slot_present is False


@pytest.mark.asyncio
async def test_a_complete_collect_says_so():
    async with SessionLocal() as db:
        assert (await ha_service.collect(db)).complete is True


@pytest.mark.asyncio
async def test_the_alarms_of_one_cycle_share_one_snapshot(ha, monkeypatch):
    calls = []
    snap = ha_service.Snapshot(db_role="primary", replication_state="streaming")

    async def collect(db):
        calls.append(1)
        return snap

    monkeypatch.setattr(ha_service, "collect", collect)
    _partner(monkeypatch, None)
    for check in ALL_CHECKS:
        await _run(check)
    assert len(calls) == 1
    monkeypatch.setattr(ha_service, "SHARED_TTL_SECONDS", 0.0)
    await _run(ha_checks.check_replication_lag)
    assert len(calls) == 2  # an expired snapshot is taken anew


@pytest.mark.asyncio
async def test_split_brain_check_reuses_the_gates_probe_instead_of_asking_again(ha, monkeypatch):
    _snap(monkeypatch, epoch=3)
    asked = []

    async def probe():
        asked.append(1)
        return ("replica", 0)

    monkeypatch.setattr(leader, "http_partner", lambda ip: probe)
    gate = leader.LeaderGate({})
    gate.partner_view, gate.partner_probed = ("fenced", 2), True
    monkeypatch.setattr(leader, "gate", gate)
    assert set(await _run(ha_checks.check_split_brain)) == {"partner"} and asked == []
    gate.partner_probed = False  # the gate has not asked yet: ask ourselves
    assert await _run(ha_checks.check_split_brain) == {} and asked == [1]
    gate.partner_view, gate.partner_probed = None, True  # the gate asked and got no answer: keep an open alarm open
    problems = await _run(ha_checks.check_split_brain)
    assert dict(problems) == {} and "partner" in problems.preserve_prefixes and asked == [1]
