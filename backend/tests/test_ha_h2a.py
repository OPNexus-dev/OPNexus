"""H2a (docs/HA-H2-DESIGN.md 3.6, 3.9, 4): what the backend does for a warm standby.

Partner guard (role fenced), health with epoch/node, settings rows on a replica, grace period after a promote for
overdue rollout steps, interrupted maintenance runs."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app import leader
from app.config import settings
from app.database import SessionLocal
from app.models.ha_state import HaState
from app.models.host_operation import HostOperation
from app.models.notification import NotificationSettings
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services import ha_service
from app.services import rollout_engine as engine
from app.services import scheduled_maintenance_service as svc
from app.singleton import get_or_create, with_defaults
from tests.test_leader import FAST, Tracker, _gate, _stop, _until
from tests.test_rollout_engine import (_host, _run, _scheds, _setup, _start, _tick, _until as _until_run,
                                       _until_step_pending, clock)  # noqa: F401 (clock is a fixture)
from tests.test_rules import _create_host


class Partner:
    """Stands in for the HTTP probe of the other node."""

    def __init__(self, view):
        self.view, self.calls = view, 0

    async def __call__(self):
        self.calls += 1
        return self.view


async def _set_state(epoch, *, node="b", promoted_at=None):
    async with SessionLocal() as db:
        db.add(HaState(id=1, epoch=epoch, primary_node=node, promoted_at=promoted_at))
        await db.commit()


# ── partner guard ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_partner_leader_with_an_equal_or_higher_epoch_fences_this_node():
    await _set_state(2)
    for partner_epoch in (2, 3):  # equal fences both sides: the safe way to fail
        tracker = Tracker()
        gate = _gate(tracker, "a", partner=Partner(("leader", partner_epoch)), partner_interval=0.2)
        task = asyncio.create_task(gate.run())
        try:
            await _until(lambda: gate.role == leader.FENCED)
            await asyncio.sleep(0.8)  # longer than the takeover delay
            assert gate.role == leader.FENCED and gate.split_brain and tracker.iterations == {}
        finally:
            await _stop((gate, task))


@pytest.mark.asyncio
async def test_a_partner_with_a_lower_epoch_or_not_leader_is_no_objection():
    await _set_state(5)
    for view in (("leader", 4), ("replica", 99), ("standby", 99), ("takeover", 99), ("fenced", 99), None):
        tracker = Tracker()
        gate = _gate(tracker, "a", partner=Partner(view), partner_interval=0.2)
        task = asyncio.create_task(gate.run())
        try:
            await _until(lambda: gate.role == leader.LEADER)
            await _until(lambda: tracker.iterations.get("a", 0) >= 2)
            assert not gate.split_brain
        finally:
            await _stop((gate, task))


@pytest.mark.asyncio
async def test_a_partner_that_stops_being_leader_releases_the_fence():
    tracker = Tracker()
    partner = Partner(("leader", 1))
    gate = _gate(tracker, "a", partner=partner, partner_interval=0.2)
    task = asyncio.create_task(gate.run())
    try:
        await _until(lambda: gate.role == leader.FENCED)
        partner.view = None  # the partner was stopped (fencing done properly)
        await _until(lambda: gate.role == leader.LEADER)
        assert not gate.split_brain
    finally:
        await _stop((gate, task))


@pytest.mark.asyncio
async def test_a_leader_is_fenced_when_the_partner_appears_and_resumes_when_it_goes():
    tracker = Tracker()
    partner = Partner(None)
    gate = _gate(tracker, "a", partner=partner, partner_interval=0.2)
    task = asyncio.create_task(gate.run())
    try:
        await _until(lambda: gate.role == leader.LEADER)
        before = tracker.iterations["a"]
        partner.view = ("leader", 1)  # the old primary came back
        await _until(lambda: gate.role == leader.FENCED)
        assert not tracker.running  # no loop iteration is in flight any more
        frozen = tracker.iterations["a"]
        await asyncio.sleep(0.6)
        assert tracker.iterations["a"] == frozen >= before
        partner.view = None
        await _until(lambda: gate.role == leader.LEADER)
        await _until(lambda: tracker.iterations["a"] > frozen)
    finally:
        await _stop((gate, task))


@pytest.mark.asyncio
async def test_the_last_look_before_the_loops_start_catches_a_partner_that_became_leader_meanwhile():
    """Both nodes in takeover at the same time: the one that finishes second must not start. The throttled check
    would not ask again within 30 s, so the final look has to be a fresh one."""
    class Late(Partner):
        async def __call__(self):
            self.calls += 1
            return ("standby", 0) if self.calls == 1 else ("leader", 0)

    tracker = Tracker()
    partner = Late(None)
    gate = _gate(tracker, "a", partner=partner, partner_interval=60.0)
    task = asyncio.create_task(gate.run())
    try:
        await _until(lambda: gate.role == leader.FENCED)
        assert tracker.iterations == {} and partner.calls >= 2
    finally:
        await _stop((gate, task))


@pytest.mark.asyncio
async def test_a_replica_asks_nobody():
    from tests.test_leader import _ReplicaConn
    partner = Partner(("leader", 9))

    async def connect():
        return _ReplicaConn()

    gate = leader.LeaderGate({}, connect=connect, partner=partner, **FAST)
    task = asyncio.create_task(gate.run())
    try:
        await _until(lambda: gate.role == leader.REPLICA)
        await asyncio.sleep(0.5)
        assert gate.role == leader.REPLICA and partner.calls == 0 and not gate.split_brain
    finally:
        await _stop((gate, task))


@pytest.mark.asyncio
async def test_the_guard_is_off_without_a_peer_address(monkeypatch):
    monkeypatch.setattr(settings, "ha_peer_ip", "")
    assert leader.LeaderGate({})._partner is None
    monkeypatch.setattr(settings, "ha_peer_ip", "not-an-ip")
    assert leader.LeaderGate({})._partner is None
    monkeypatch.setattr(settings, "ha_peer_ip", "192.168.1.21")
    assert leader.LeaderGate({})._partner is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,expected", [
    (httpx.Response(200, json={"status": "ok", "role": "leader", "epoch": 3, "node": "b"}), ("leader", 3)),
    (httpx.Response(200, json={"status": "ok", "role": "leader"}), ("leader", 0)),  # an older partner has no epoch
    (httpx.Response(200, json={"status": "ok", "role": "replica", "epoch": None}), ("replica", 0)),
    (httpx.Response(502, text="bad gateway"), None),
    (httpx.Response(200, json=["not", "an", "object"]), None),
    (httpx.ConnectError("refused"), None),
    (httpx.ReadTimeout("slow"), None),
])
async def test_the_http_probe_reads_role_and_epoch_and_never_raises(respx_mock, answer, expected):
    route = respx_mock.get("https://192.168.1.21/api/health")
    route.mock(side_effect=answer) if isinstance(answer, Exception) else route.mock(return_value=answer)
    assert await leader.http_partner("192.168.1.21")() == expected


@pytest.mark.asyncio
async def test_the_http_probe_brackets_an_ipv6_address(respx_mock):
    route = respx_mock.get("https://[fd00::2]/api/health").respond(200, json={"role": "leader", "epoch": 1})
    assert await leader.http_partner("fd00::2")() == ("leader", 1)
    assert route.called


# ── health ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_reports_epoch_and_node(client, monkeypatch):
    leader.gate = None
    monkeypatch.setattr(settings, "ha_node", "")
    assert (await client.get("/api/health")).json() == {"status": "ok", "role": "starting", "epoch": 0, "node": None}
    await _set_state(4)
    monkeypatch.setattr(settings, "ha_node", "a")
    assert (await client.get("/api/health")).json() == {"status": "ok", "role": "starting", "epoch": 4, "node": "a"}


@pytest.mark.asyncio
async def test_health_stays_up_when_the_database_cannot_be_read(client, monkeypatch):
    leader.gate = None

    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(leader, "SessionLocal", broken)
    assert (await client.get("/api/health")).json()["epoch"] is None


# ── settings rows on a replica ───────────────────────────────────────────────

class _ReplicaDb:
    """A session on a read-only database: the row is missing, pg_is_in_recovery() says true, any write is a bug."""

    async def get(self, model, key):
        return None

    async def scalar(self, statement):
        assert "pg_is_in_recovery" in str(statement)
        return True

    def add(self, row):
        raise AssertionError("a replica must not insert")

    async def flush(self):
        raise AssertionError("a replica must not flush")


@pytest.mark.asyncio
async def test_a_missing_settings_row_on_a_replica_is_a_default_object_and_not_an_insert():
    row = await get_or_create(_ReplicaDb(), NotificationSettings)
    assert row.id == 1 and row.host_offline_enabled is True and row.smtp_port == 587
    assert row.service_watchlist and row.gateway_alert_muted == [] and row.poll_interval_seconds == 300
    again = with_defaults(NotificationSettings, id=1)
    again.service_watchlist.append("x")  # the default lists are not shared between objects
    assert "x" not in row.service_watchlist


@pytest.mark.asyncio
async def test_a_missing_settings_row_on_a_primary_is_created():
    async with SessionLocal() as db:
        row = await get_or_create(db, NotificationSettings)
        await db.commit()
    async with SessionLocal() as db:
        assert (await db.get(NotificationSettings, 1)).host_offline_enabled is True
        assert row is not None


# ── grace period after a promote ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grace_period_is_only_open_for_ten_minutes_after_a_promote(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(ha_service, "_now", lambda: now)
    async with SessionLocal() as db:
        assert await ha_service.grace_ends_at(db) is None and await ha_service.last_grace_end(db) is None
    await _set_state(1, promoted_at=now - timedelta(minutes=3))
    async with SessionLocal() as db:
        assert await ha_service.grace_ends_at(db) == now + timedelta(minutes=7)
    monkeypatch.setattr(ha_service, "_now", lambda: now + timedelta(minutes=8))
    async with SessionLocal() as db:
        assert await ha_service.grace_ends_at(db) is None
        assert await ha_service.last_grace_end(db) == now + timedelta(minutes=7)


@pytest.mark.asyncio
async def test_an_overdue_rollout_step_after_a_promote_waits_for_the_end_of_the_grace_period(client, respx_mock, clock, monkeypatch):
    monkeypatch.setattr(ha_service, "_now", clock)
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    name = await _until_step_pending(fleet, clock, run["id"])
    clock.now += timedelta(minutes=30)  # the primary was down; the standby has just been promoted
    promoted = clock.now
    await _set_state(1, promoted_at=promoted)
    await _tick(fleet, clock)
    (sched,) = await _scheds()
    assert sched.status == "pending" and sched.run_at == promoted + ha_service.PROMOTE_GRACE
    assert fleet.fw[name].update.call_count == 0
    assert (await _run(run["id"])).status == "wave_running"  # not paused: nothing was dropped
    # Well into the grace period the engine's own "never started" timeout must not cancel the postponed step.
    clock.now = promoted + timedelta(minutes=9)
    await _tick(fleet, clock)
    assert (await _scheds())[0].status == "pending" and (await _run(run["id"])).status == "wave_running"
    clock.now = promoted + timedelta(minutes=10, seconds=5)
    done = await _until_run(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
    assert done.status == "succeeded", (done.pause_reason, done.pause_detail)
    assert all(fw.update.call_count == 1 for fw in fleet.fw.values())


@pytest.mark.asyncio
async def test_without_a_promote_an_overdue_rollout_step_is_still_dropped(client, respx_mock, clock, monkeypatch):
    monkeypatch.setattr(ha_service, "_now", clock)
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    name = await _until_step_pending(fleet, clock, run["id"])
    clock.now += timedelta(minutes=30)
    await _set_state(0, promoted_at=None)
    await _tick(fleet, clock)
    (sched,) = await _scheds()
    assert sched.status == "failed" and sched.result["not_fired"] and fleet.fw[name].update.call_count == 0
    paused = await _run(run["id"])
    assert _host(paused, name).detail["code"] == "not_fired"


@pytest.mark.asyncio
async def test_a_promote_long_ago_gives_no_grace(client, respx_mock, clock, monkeypatch):
    monkeypatch.setattr(ha_service, "_now", clock)
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    await _until_step_pending(fleet, clock, run["id"])
    await _set_state(1, promoted_at=clock.now - timedelta(hours=2))
    clock.now += timedelta(minutes=30)
    await _tick(fleet, clock)
    (sched,) = await _scheds()
    assert sched.status == "failed" and sched.result["not_fired"]


# ── interrupted maintenance runs ─────────────────────────────────────────────

async def _hung_run(host, *, op_age_minutes, phase=None, op_status="starting", linked=False):
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        op = HostOperation(host_id=uuid.UUID(host["id"]), host_name=host["name"], actor="admin", action="update",
                           expires_at=now, fingerprint="", snapshot={}, status=op_status,
                           started_at=now - timedelta(minutes=op_age_minutes))
        sched = ScheduledMaintenance(
            host_id=uuid.UUID(host["id"]), host_name=host["name"], action="update", run_at=now - timedelta(hours=1),
            status="running", phase=phase, actor="admin", created_at=now - timedelta(hours=1), auto_verify=True)
        db.add_all([op, sched])
        await db.flush()
        if linked:
            sched.host_operation_id = op.id
        await db.commit()
        return sched.id, op.id


async def _recover():
    async with SessionLocal() as db:
        return await svc.recover_interrupted(db)


async def _status(sched_id, op_id):
    async with SessionLocal() as db:
        sched = await db.get(ScheduledMaintenance, sched_id, populate_existing=True)
        op = await db.get(HostOperation, op_id, populate_existing=True)
        return sched, op


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_a_run_without_a_phase_older_than_fifteen_minutes_is_closed_as_interrupted(client, linked):
    host = await _create_host(client, "solo", "https://solo.test")
    sched_id, op_id = await _hung_run(host, op_age_minutes=20, linked=linked)
    assert await _recover() == 1
    sched, op = await _status(sched_id, op_id)
    assert sched.status == "failed" and sched.result["interrupted"] is True and "unklar" in sched.result["error"]
    assert op.status == "unknown"  # stays locked until a person has looked: the call may have gone out
    assert await _recover() == 0


@pytest.mark.asyncio
async def test_a_recent_run_or_one_with_a_phase_is_left_alone(client):
    host = await _create_host(client, "solo", "https://solo.test")
    recent = await _hung_run(host, op_age_minutes=5)
    phased = await _hung_run(host, op_age_minutes=60, phase="job_wait", linked=True)
    assert await _recover() == 0
    for sched_id, op_id in (recent, phased):
        sched, op = await _status(sched_id, op_id)
        assert sched.status == "running" and op.status == "starting"


@pytest.mark.asyncio
async def test_a_run_that_another_backend_is_firing_right_now_is_skipped(client):
    host = await _create_host(client, "solo", "https://solo.test")
    sched_id, op_id = await _hung_run(host, op_age_minutes=20, linked=True)
    async with SessionLocal() as holder:
        await holder.scalar(select(ScheduledMaintenance).where(ScheduledMaintenance.id == sched_id).with_for_update())
        assert await _recover() == 0
        await holder.rollback()
    sched, _ = await _status(sched_id, op_id)
    assert sched.status == "running"


@pytest.mark.asyncio
async def test_an_interrupted_rollout_step_pauses_the_run(client, respx_mock, clock, monkeypatch):
    monkeypatch.setattr(ha_service, "_now", clock)
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    name = await _until_step_pending(fleet, clock, run["id"])
    # The old leader committed "running" and died before it recorded anything else.
    async with SessionLocal() as db:
        (sched,) = (await db.scalars(select(ScheduledMaintenance))).all()
        sched.status = "running"
        db.add(HostOperation(host_id=sched.host_id, host_name=sched.host_name, actor="admin", action="update",
                             expires_at=clock.now, fingerprint="", snapshot={}, status="starting", started_at=clock.now))
        await db.commit()
    clock.now += timedelta(minutes=20)
    async with SessionLocal() as db:
        assert await svc.recover_interrupted(db) == 1
    await _tick(fleet, clock)
    paused = await _run(run["id"])
    assert (paused.status, paused.pause_reason) == ("paused", "host_failed")
    assert _host(paused, name).status == "failed" and fleet.fw[name].update.call_count == 0


# ── review fixes ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_answers_within_a_second_even_if_the_database_hangs(client, monkeypatch):
    leader.gate = None

    class Hanging:
        async def __aenter__(self):
            await asyncio.sleep(30)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(leader, "SessionLocal", lambda: Hanging())
    monkeypatch.setattr(leader, "HEALTH_DB_TIMEOUT", 0.2)
    started = asyncio.get_running_loop().time()
    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok" and body["epoch"] is None
    assert asyncio.get_running_loop().time() - started < 3


@pytest.mark.asyncio
async def test_a_fenced_node_refuses_changes_but_still_reads_and_signs_in(client, monkeypatch):
    gate = leader.LeaderGate({})
    monkeypatch.setattr(leader, "gate", gate)
    gate.role = leader.FENCED
    refused = await client.post("/api/groups", json={"name": "nope", "host_ids": []})
    assert refused.status_code == 503 and refused.json()["detail"]["code"] == "NODE_FENCED"
    assert (await client.put("/api/notifications/settings", json={"ha_alerts_enabled": False})).status_code == 503
    assert (await client.delete(f"/api/groups/{uuid.uuid4()}")).status_code == 503
    assert (await client.get("/api/groups")).status_code == 200
    assert (await client.get("/api/health")).json()["role"] == "fenced"
    gate.role = leader.LEADER
    assert (await client.post("/api/groups", json={"name": "ok", "host_ids": []})).status_code == 201
    gate.role = leader.FENCED
    assert (await client.post("/api/auth/logout")).status_code != 503  # signing in and out stay possible


@pytest.mark.asyncio
async def test_the_maintenance_loop_closes_interrupted_runs_in_every_round_not_only_the_first(monkeypatch):
    from app import main
    calls = {"recover": 0}

    async def recover(db):
        calls["recover"] += 1
        return 0

    async def fire_due(db):
        return {"due": 0, "fired": 0, "blocked_or_failed": 0, "postponed": 0}

    async def advance(db):
        return 0

    async def advance_runs(db):
        return 0

    class Stop(Exception):
        pass

    async def sleep(seconds):
        calls["sleeps"] = calls.get("sleeps", 0) + 1
        if calls["sleeps"] >= 3:  # three rounds, whatever the loop did in them (a broken loop must fail, not hang)
            raise Stop()

    monkeypatch.setattr(svc, "recover_interrupted", recover)
    monkeypatch.setattr(svc, "fire_due", fire_due)
    monkeypatch.setattr(svc, "advance_running", advance)
    monkeypatch.setattr(engine, "advance_runs", advance_runs)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    monkeypatch.setattr(leader, "gate", None)
    with pytest.raises(Stop):
        await main._scheduled_maintenance_poll_loop()
    assert calls["recover"] == 3


@pytest.mark.asyncio
async def test_the_probe_uses_the_partners_proxy_port(respx_mock, monkeypatch):
    route = respx_mock.get("https://192.168.1.21:8443/api/health").respond(200, json={"role": "leader", "epoch": 2})
    monkeypatch.setattr(settings, "ha_peer_port", 8443)
    assert await leader.http_partner("192.168.1.21")() == ("leader", 2)
    assert route.called
    other = respx_mock.get("https://192.168.1.21:9443/api/health").respond(200, json={"role": "replica", "epoch": 0})
    assert await leader.http_partner("192.168.1.21", 9443)() == ("replica", 0)
    assert other.called
    monkeypatch.setattr(settings, "ha_peer_port", 443)
    default = respx_mock.get("https://192.168.1.21/api/health").respond(200, json={"role": "standby", "epoch": 1})
    assert await leader.http_partner("192.168.1.21")() == ("standby", 1)
    assert default.called

# ── ha_check (used by `opnexus ha status` / `ha promote`) ────────────────────

@pytest.mark.asyncio
async def test_ha_check_names_hosts_and_two_factor_accounts_without_a_secret_file(client, monkeypatch):
    from app import ha_check
    from app.models.user import AdminUser
    from app.services import opnsense_secret_store, totp_service
    complete = await _create_host(client, "has-secret", "https://a.test")
    lost = await _create_host(client, "lost-secret", "https://b.test")
    opnsense_secret_store.delete_secret(uuid.UUID(lost["id"]))
    async with SessionLocal() as db:
        with_totp = AdminUser(username="with-totp", password_hash="x", role="admin", totp_enabled=True)
        without_file = AdminUser(username="no-totp-file", password_hash="x", role="admin", totp_enabled=True)
        plain = AdminUser(username="plain", password_hash="x", role="admin")
        db.add_all([with_totp, without_file, plain])
        await db.commit()
        totp_service.save_secret(with_totp.id, "JBSWY3DPEHPK3PXP")
    assert opnsense_secret_store.read_secret(uuid.UUID(complete["id"])) is not None
    assert await ha_check.missing() == {"hosts": ["lost-secret"], "totp": ["no-totp-file"]}
