"""H0 (docs/HA-PLAN.md): only the process holding the leader lock runs the background loops.

These run against the real test database: the lock, its loss when the session is terminated and the takeover are
PostgreSQL behaviour, not something a mock would prove.
"""
import asyncio
import logging

import httpx
import pytest
from sqlalchemy import text

from app import leader, main
from app.config import settings
from app.database import SessionLocal, engine
from app.services import auth_service, heartbeat_service, url_guard

FAST = {"check_interval": 0.2, "check_timeout": 1.0, "retry_interval": 0.2, "takeover_delay": 0.6}


class Tracker:
    """Loop bodies that record which gate is running an iteration right now."""

    def __init__(self):
        self.running: set[str] = set()
        self.max_concurrent = 0
        self.iterations: dict[str, int] = {}

    def loop(self, name: str, gate_ref: dict, iteration_seconds: float = 0.05):
        async def body():
            while True:
                await gate_ref["gate"].checkpoint()
                self.running.add(name)
                self.max_concurrent = max(self.max_concurrent, len(self.running))
                self.iterations[name] = self.iterations.get(name, 0) + 1
                try:
                    await asyncio.sleep(iteration_seconds)
                finally:
                    self.running.discard(name)
        return body


def _gate(tracker: Tracker, name: str, iteration_seconds: float = 0.05, **kw) -> leader.LeaderGate:
    ref: dict = {}
    gate = leader.LeaderGate({"work": tracker.loop(name, ref, iteration_seconds)}, **{**FAST, **kw})
    ref["gate"] = gate
    return gate


async def _until(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


async def _stop(*gates_and_tasks):
    for gate, task in gates_and_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await gate.stop()


@pytest.mark.asyncio
async def test_a_single_gate_waits_the_takeover_delay_then_runs_the_loops():
    tracker = Tracker()
    gate = _gate(tracker, "a")
    task = asyncio.create_task(gate.run())
    try:
        await _until(lambda: gate.role == leader.TAKEOVER)
        assert tracker.iterations == {}  # nothing runs during the takeover wait
        await _until(lambda: gate.role == leader.LEADER)
        await _until(lambda: tracker.iterations.get("a", 0) >= 3)
    finally:
        await _stop((gate, task))
    assert gate.role == leader.STANDBY


@pytest.mark.asyncio
async def test_a_second_gate_stays_in_standby_and_runs_nothing():
    tracker = Tracker()
    a, b = _gate(tracker, "a"), _gate(tracker, "b")
    ta = asyncio.create_task(a.run())
    try:
        await _until(lambda: a.role == leader.LEADER)
        tb = asyncio.create_task(b.run())
        try:
            await _until(lambda: b.role == leader.STANDBY)
            await asyncio.sleep(1.0)
            assert b.role == leader.STANDBY and "b" not in tracker.iterations
            assert tracker.max_concurrent == 1
        finally:
            await _stop((b, tb))
    finally:
        await _stop((a, ta))


@pytest.mark.asyncio
async def test_a_terminated_lock_session_stops_the_loops_and_the_other_takes_over_without_overlap():
    """The review's case: the connection dies under a living process. The loops must stop before anyone else runs."""
    tracker = Tracker()
    a, b = _gate(tracker, "a"), _gate(tracker, "b")
    ta = asyncio.create_task(a.run())
    tb = None
    try:
        await _until(lambda: a.role == leader.LEADER and tracker.iterations.get("a", 0) > 0)
        tb = asyncio.create_task(b.run())
        await _until(lambda: b.role == leader.STANDBY)
        pid = await a._conn.scalar(text("SELECT pg_backend_pid()"))
        await a._conn.commit()
        async with engine.connect() as admin:
            assert await admin.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        await _until(lambda: b.role == leader.LEADER and tracker.iterations.get("b", 0) > 0)
        assert a.role != leader.LEADER
        assert "a" not in tracker.running
        assert tracker.max_concurrent == 1, "both gates ran an iteration at the same time"
    finally:
        if tb is not None:
            await _stop((b, tb))
        await _stop((a, ta))


@pytest.mark.asyncio
async def test_a_long_iteration_is_cancelled_when_the_lock_is_lost():
    """A notification cycle can take half a minute; it must not keep running after the lock is gone."""
    tracker = Tracker()
    # The old leader notices its loss only at its next check (up to 1.5 s + timeout), while the new one applies every
    # 0.1 s: only the takeover delay (default: check interval + timeout + margin) keeps the two apart.
    slow = {"check_interval": 1.5, "takeover_delay": None}
    a = _gate(tracker, "a", iteration_seconds=60, **slow)
    b = _gate(tracker, "b", iteration_seconds=60, retry_interval=0.1, **slow)
    ta = asyncio.create_task(a.run())
    tb = None
    try:
        await _until(lambda: "a" in tracker.running, timeout=15)
        tb = asyncio.create_task(b.run())
        await _until(lambda: b.role == leader.STANDBY)
        pid = await a._conn.scalar(text("SELECT pg_backend_pid()"))
        await a._conn.commit()
        async with engine.connect() as admin:
            await admin.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        await _until(lambda: "a" not in tracker.running, timeout=5)
        await _until(lambda: "b" in tracker.running, timeout=15)
        assert tracker.max_concurrent == 1
    finally:
        if tb is not None:
            await _stop((b, tb))
        await _stop((a, ta))


@pytest.mark.asyncio
async def test_checkpoint_refuses_outside_leadership():
    tracker = Tracker()
    gate = _gate(tracker, "a")
    with pytest.raises(leader.LeadershipLost):
        await gate.checkpoint()
    # Without a gate (a test driving a loop directly) the module-level checkpoint is a no-op.
    leader.gate = None
    await leader.checkpoint()
    assert leader.current_role() == leader.STARTING


@pytest.mark.asyncio
async def test_stop_releases_the_lock_at_once():
    tracker = Tracker()
    a, b = _gate(tracker, "a"), _gate(tracker, "b")
    ta = asyncio.create_task(a.run())
    await _until(lambda: a.role == leader.LEADER)
    await _stop((a, ta))
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": leader.LOCK_KEY})
        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": leader.LOCK_KEY})
    tb = asyncio.create_task(b.run())
    try:
        await _until(lambda: b.role == leader.LEADER)
    finally:
        await _stop((b, tb))


@pytest.mark.asyncio
async def test_a_crashed_loop_is_restarted_and_lost_leadership_is_not_mistaken_for_a_crash():
    starts = []

    async def flaky():
        starts.append(1)
        await asyncio.sleep(0.05)
        if len(starts) == 1:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    gate = leader.LeaderGate({"flaky": flaky}, **FAST)
    task = asyncio.create_task(gate.run())
    try:
        await _until(lambda: len(starts) >= 2)
        assert gate.role == leader.LEADER
    finally:
        await _stop((gate, task))


class _ReplicaConn:
    """A connection to a database in recovery: pg_try_advisory_lock would succeed there, so it must never be asked."""

    def __init__(self):
        self.statements: list[str] = []

    async def scalar(self, statement, params=None):
        self.statements.append(str(statement))
        return True

    async def commit(self):
        pass

    async def invalidate(self):
        pass

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_a_replica_never_becomes_leader():
    conn = _ReplicaConn()

    async def connect():
        return conn

    tracker = Tracker()
    ref: dict = {}
    gate = leader.LeaderGate({"work": tracker.loop("r", ref)}, connect=connect, **FAST)
    ref["gate"] = gate
    task = asyncio.create_task(gate.run())
    try:
        await _until(lambda: gate.role == leader.REPLICA)
        await asyncio.sleep(0.6)
        assert gate.role == leader.REPLICA and tracker.iterations == {}
        assert not any("advisory" in s for s in conn.statements)
    finally:
        await _stop((gate, task))


@pytest.mark.asyncio
async def test_health_and_metrics_report_the_role(client):
    leader.gate = None
    assert (await client.get("/api/health")).json() == {"status": "ok", "role": "starting"}
    tracker = Tracker()
    gate = _gate(tracker, "a")
    leader.gate = gate
    task = asyncio.create_task(gate.run())
    try:
        await _until(lambda: gate.role == leader.LEADER)
        assert (await client.get("/api/health")).json()["role"] == "leader"
        metrics = (await client.get("/api/metrics")).text
        assert 'opnexus_leader{role="leader"} 1' in metrics
    finally:
        await _stop((gate, task))
        leader.gate = None


@pytest.mark.asyncio
async def test_bootstrap_admin_waits_for_a_concurrent_first_start():
    async with engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": auth_service._BOOTSTRAP_LOCK_KEY})
        async with SessionLocal() as db:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(auth_service.bootstrap_admin(db), 0.5)
        await other.rollback()  # releases the lock
    async with SessionLocal() as db:
        await asyncio.wait_for(auth_service.bootstrap_admin(db), 5)


# ── Heartbeat ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["", "http://hc.example.test/ping/abc", "https://127.0.0.1/ping", "https://localhost/x",
                                   "not a url"])
def test_heartbeat_is_off_unless_a_valid_https_url_is_set(monkeypatch, value):
    monkeypatch.setattr(settings, "heartbeat_url", value)
    assert heartbeat_service.configured_url() is None


@pytest.mark.asyncio
async def test_heartbeat_pings_and_never_logs_the_url(monkeypatch, respx_mock, caplog):
    url = "https://hc.example.test/ping/secret-check-id"
    monkeypatch.setattr(settings, "heartbeat_url", url)

    async def public(host, port=None):
        return ["203.0.113.10"]
    monkeypatch.setattr(url_guard, "resolve_host", public)
    route = respx_mock.get(url).mock(return_value=httpx.Response(200))
    caplog.set_level(logging.DEBUG)
    assert await heartbeat_service.ping() is True
    assert route.call_count == 1
    route.mock(return_value=httpx.Response(500))
    assert await heartbeat_service.ping() is False
    assert "secret-check-id" not in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_refuses_a_name_that_resolves_to_loopback(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_url", "https://hc.example.test/ping/x")

    async def rebound(host, port=None):
        return ["127.0.0.1"]
    monkeypatch.setattr(url_guard, "resolve_host", rebound)
    assert await heartbeat_service.ping() is False


class _StopLoop(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("checks_fail", [False, True])
async def test_the_notification_loop_sends_the_heartbeat_only_after_a_successful_cycle(monkeypatch, checks_fail):
    pings = []

    async def checks(db):
        if checks_fail:
            raise RuntimeError("database gone")

    async def ping():
        pings.append(1)
        return True

    async def no_carp(db):
        return []

    async def stop(_):
        raise _StopLoop()

    leader.gate = None
    monkeypatch.setattr(main.notification_service, "run_notification_checks", checks)
    monkeypatch.setattr(main.heartbeat_service, "ping", ping)
    monkeypatch.setattr(main.carp_event_service, "detect_and_record", no_carp)
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    with pytest.raises(_StopLoop):
        await main._notification_poll_loop()
    assert pings == ([] if checks_fail else [1])
