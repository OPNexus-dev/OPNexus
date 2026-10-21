"""The rollout engine end to end against a fake fleet: c1 (stand-alone), ha-a (MASTER) + ha-b (BACKUP)
in cluster "ha". A fake clock drives timeouts, soak and re-checks; every tick runs exactly what the
poll loop runs (fire due schedules, step running ones, step the rollout)."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.host_operation import HostOperation
from app.models.rollout import RolloutPlan, RolloutRun
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services import rollout_engine as engine
from app.services import scheduled_maintenance_service as svc
from tests.test_rules import _create_host


class Fw:
    def __init__(self, fleet, name, *, role=None, version="26.7.6", latest="26.7.7"):
        self.fleet, self.name, self.base = fleet, name, f"https://{name}.test"
        self.role, self.maint = role, False
        self.version, self.latest, self.offer = version, latest, "update"
        self.job, self.job_ticks, self.boot, self.up = "ready", 0, "boot-1", True
        self.update_error = False
        self.services = [{"id": "unbound", "name": "unbound", "locked": 0, "running": 1},
                         {"id": "ntpd", "name": "ntpd", "locked": 0, "running": 1}]
        self.gateways = [{"name": "WAN_GW", "status": "none", "loss": "0.0 %", "delay": "1 ms", "stddev": "0.1 ms"}]
        r, b = fleet.mock, self.base
        self.update = r.post(f"{b}/api/core/firmware/update").mock(side_effect=self._update)
        self.check = r.post(f"{b}/api/core/firmware/check").mock(side_effect=lambda q: self._ok({"status": "ok"}))
        self.toggles = r.post(f"{b}/api/diagnostics/interface/carp_status/maintenance").mock(side_effect=self._toggle)
        r.get(f"{b}/api/core/backup/download/this").mock(side_effect=lambda q: self._raw(b"<opnsense/>"))
        r.get(f"{b}/api/diagnostics/interface/getVipStatus").mock(side_effect=self._vip)
        r.get(f"{b}/api/core/firmware/running").mock(side_effect=lambda q: self._ok({"status": self.job}))
        r.get(f"{b}/api/core/firmware/info").mock(side_effect=lambda q: self._ok({"product_version": self.version}))
        r.get(f"{b}/api/core/firmware/status").mock(side_effect=self._status)
        r.get(f"{b}/api/diagnostics/system/systemTime").mock(side_effect=lambda q: self._ok({"boottime": self.boot}))
        r.get(f"{b}/api/routes/gateway/status").mock(side_effect=lambda q: self._ok({"items": self.gateways, "status": "ok"}))
        r.get(f"{b}/api/core/service/search").mock(side_effect=lambda q: self._ok({"rows": self.services}))

    @property
    def down(self):
        return self.job == "down" or not self.up

    def _ok(self, data):
        if self.down:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=data)

    def _raw(self, content):
        if self.down:
            raise httpx.ConnectError("down")
        return httpx.Response(200, content=content)

    def _status(self, request):
        if self.offer == "update":
            return self._ok({"status": "update", "status_msg": "x", "status_reboot": "1", "all_packages": {},
                             "product": {"product_latest": self.latest}})
        return self._ok({"status": self.offer, "status_msg": "x", "status_reboot": "0", "all_packages": {}, "product": {}})

    def _update(self, request):
        if self.update_error:
            return httpx.Response(500, json={"status": "error"})
        self.job, self.job_ticks = "running", 2
        return self._ok({"status": "ok"})

    def _vip(self, request):
        rows = [{"vhid": "1", "status": self.role}] if self.role else []
        return self._ok({"rows": rows, "carp": {"allow": "1", "maintenancemode": self.maint, "demotion": "240" if self.maint else "0",
                                                "status_msg": ""}})

    def _toggle(self, request):
        self.maint = not self.maint
        partner = self.fleet.partner(self)
        if self.maint:
            self.role, partner.role = "BACKUP", "MASTER"
        else:
            self.role, partner.role = "MASTER", "BACKUP"  # preempt: takes MASTER back
        return self._ok({"status": "ok", "action": "enter_maintenance" if self.maint else "leave_maintenance"})

    def progress(self):
        if self.job == "running":
            self.job_ticks -= 1
            if self.job_ticks <= 0:
                self.job, self.job_ticks = "down", 2
        elif self.job == "down":
            self.job_ticks -= 1
            if self.job_ticks <= 0:
                self.job, self.version, self.offer, self.boot = "ready", self.latest, "none", self.boot + "+"


class Fleet:
    def __init__(self, respx_mock):
        self.mock = respx_mock
        self.fw = {"c1": Fw(self, "c1"), "ha-a": Fw(self, "ha-a", role="MASTER"), "ha-b": Fw(self, "ha-b", role="BACKUP")}

    def partner(self, fw):
        return self.fw["ha-b"] if fw.name == "ha-a" else self.fw["ha-a"]

    def progress(self):
        for fw in self.fw.values():
            fw.progress()


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(engine, "_now", c)
    monkeypatch.setattr(svc, "_now", c)
    return c


async def _setup(client, respx_mock, *, waves=None, **plan_kw):
    fleet = Fleet(respx_mock)
    hosts = {n: await _create_host(client, n, f"https://{n}.test") for n in ("c1", "ha-a", "ha-b")}
    for n in ("ha-a", "ha-b"):
        await client.patch(f"/api/hosts/{hosts[n]['id']}", json={"cluster_name": "ha"})
    groups = {}
    for g, members in (("canary", ["c1"]), ("pilot", ["ha-b"]), ("prod", ["ha-a"]), ("cluster", ["ha-a", "ha-b"])):
        groups[g] = (await client.post("/api/groups", json={"name": g, "host_ids": [hosts[m]["id"] for m in members]})).json()["id"]
    waves = waves or [
        {"label": "canary", "group_ids": [groups["canary"]], "soak_minutes": 5},
        {"label": "pilot", "group_ids": [groups["pilot"]], "soak_minutes": 5},
        {"label": "prod", "group_ids": [groups["prod"]], "soak_minutes": 0, "carp_handover": True},
    ]
    waves = [{**w, "group_ids": [groups[g] if g in groups else g for g in w["group_ids"]]} for w in waves]
    plan = (await client.post("/api/rollouts/plans", json={"name": "ringe", "action": "update", "waves": waves, **plan_kw})).json()
    return fleet, hosts, groups, plan


async def _start(client, plan, api=None):
    api = api or client
    token = (await api.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()["preview_token"]
    resp = await api.post(f"/api/rollouts/plans/{plan['id']}/start",
                          json={"preview_token": token, "confirm_name": plan["name"], "acknowledge_unattended": True})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _tick(fleet, clock, seconds=30):
    fleet.progress()
    async with SessionLocal() as db:
        await svc.fire_due(db)
    async with SessionLocal() as db:
        await svc.advance_running(db)
    async with SessionLocal() as db:
        await engine.advance_runs(db)
    clock.now += timedelta(seconds=seconds)


async def _run(run_id):
    async with SessionLocal() as db:
        return await db.scalar(select(RolloutRun).where(RolloutRun.id == uuid.UUID(str(run_id))).execution_options(populate_existing=True))


async def _until(fleet, clock, run_id, pred, max_ticks=400, check=None):
    for _ in range(max_ticks):
        run = await _run(run_id)
        if pred(run):
            return run
        if check:
            await check()
        await _tick(fleet, clock)
    raise AssertionError(f"not reached: status={run.status} reason={run.pause_reason} detail={run.pause_detail}")


def _host(run, name):
    return next(h for h in run.hosts if h.host_name == name)


async def _scheds():
    async with SessionLocal() as db:
        return list((await db.scalars(select(ScheduledMaintenance).order_by(ScheduledMaintenance.created_at))).all())


async def _no_two_at_once():
    active = [s for s in await _scheds() if s.status in ("pending", "running")]
    assert len(active) <= 1, [(s.host_name, s.status, s.phase) for s in active]


# ── happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_three_rings_run_through_and_the_master_hands_over(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"), check=_no_two_at_once)
    assert done.status == "succeeded", (done.pause_reason, done.pause_detail)
    assert done.target_version == "26.7.7"
    assert [(h.host_name, h.status, h.version_before, h.version_after) for h in sorted(done.hosts, key=lambda h: h.wave_index)] == [
        ("c1", "succeeded", "26.7.6", "26.7.7"), ("ha-b", "succeeded", "26.7.6", "26.7.7"), ("ha-a", "succeeded", "26.7.6", "26.7.7")]
    assert all(fw.update.call_count == 1 for fw in fleet.fw.values())
    # The MASTER went through the CARP handover and is MASTER again; the BACKUP never needed one.
    assert fleet.fw["ha-a"].toggles.call_count == 2 and fleet.fw["ha-b"].toggles.call_count == 0
    assert (fleet.fw["ha-a"].role, fleet.fw["ha-b"].role) == ("MASTER", "BACKUP")
    scheds = await _scheds()
    assert [s.host_name for s in scheds] == ["c1", "ha-b", "ha-a"]
    assert all(s.origin == "rollout" and s.auto_verify and s.status == "succeeded" for s in scheds)
    async with SessionLocal() as db:
        ops = (await db.scalars(select(HostOperation))).all()
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "rollout"))).all()
    assert {o.status for o in ops} == {"acknowledged"}  # nothing left locked
    assert sum(1 for e in events if e.action == "gate" and e.status == "ok") == 3
    assert any(e.action == "finish" and e.status == "succeeded" for e in events)
    host_events = [e for e in (await _audit_host()) if (e.details or {}).get("rollout")]
    assert host_events and all(e.details["rollout"]["run_id"] == run["id"] for e in host_events)


async def _audit_host():
    async with SessionLocal() as db:
        return (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "host"))).all()


# ── the gate stops the rollout ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_failure_in_the_canary_ring_stops_everything_after_it(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    await _until(fleet, clock, run["id"], lambda r: r.status == "soaking")
    fleet.fw["c1"].services[1]["running"] = 0  # ntpd did not come back
    paused = await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
    assert paused.pause_reason == "gate_failed" and paused.paused_from == "gate"
    assert paused.waves[0].gate_attempts == 2  # looked at twice (slow start), then stopped
    assert paused.waves[0].gate_result["hosts"][_host(paused, "c1").host_id.__str__()]["services"]["code"] == "service_stopped"
    for _ in range(20):  # time passes, nothing moves
        await _tick(fleet, clock)
    assert fleet.fw["ha-b"].update.call_count == 0 and fleet.fw["ha-a"].update.call_count == 0
    assert [s.host_name for s in await _scheds()] == ["c1"]
    # Notification while paused.
    from app.services import notification_checks
    async with SessionLocal() as db:
        problems = await notification_checks.check_rollout_paused(db)
    assert problems[run["id"]]["grund"] == "gate_failed"

    # Fixed by hand, resumed: the gate is checked again first, then it goes on.
    fleet.fw["c1"].services[1]["running"] = 1
    resp = await client.post(f"/api/rollouts/runs/{run['id']}/resume")
    assert resp.status_code == 200 and resp.json()["status"] == "resuming"
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
    assert done.status == "succeeded", (done.pause_reason, done.pause_detail)
    assert fleet.fw["ha-a"].update.call_count == 1


@pytest.mark.asyncio
async def test_resume_rechecks_and_pauses_again_if_still_broken(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    await _until(fleet, clock, run["id"], lambda r: r.status == "soaking")
    fleet.fw["c1"].gateways[0] = {"name": "WAN_GW", "status": "down", "loss": "100 %", "delay": "~", "stddev": "~"}
    await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
    assert (await client.post(f"/api/rollouts/runs/{run['id']}/resume")).status_code == 200
    again = await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
    assert again.pause_reason == "gate_failed" and again.pause_detail.get("on_resume") is True
    assert fleet.fw["ha-b"].update.call_count == 0


# ── never twice, never in parallel ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restart_in_the_middle_of_job_wait_submits_exactly_once(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)

    async def in_job_wait(r):
        return any(s.host_name == "ha-b" and s.phase == "job_wait" for s in await _scheds())
    for _ in range(300):
        if await in_job_wait(None):
            break
        await _tick(fleet, clock)
    # "Restart": nothing survives but the database (each tick opens fresh sessions anyway); two backends
    # even step the same run at the very same moment.
    await asyncio.gather(*(engine.tick(uuid.UUID(run["id"])) for _ in range(3)))
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
    assert done.status == "succeeded"
    assert fleet.fw["ha-b"].update.call_count == 1 and len([s for s in await _scheds() if s.host_name == "ha-b"]) == 1


@pytest.mark.asyncio
async def test_two_backends_deciding_at_once_create_one_schedule(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    await _until(fleet, clock, run["id"], lambda r: r.status == "wave_running")
    await asyncio.gather(*(engine.tick(uuid.UUID(run["id"])) for _ in range(4)))
    assert len(await _scheds()) == 1


@pytest.mark.asyncio
async def test_a_cluster_in_one_wave_goes_backup_first_and_never_both_at_once(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock, waves=[
        {"label": "cluster", "group_ids": ["cluster"], "soak_minutes": 1, "carp_handover": True}])
    run = await _start(client, plan)
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"), check=_no_two_at_once)
    assert done.status == "succeeded", (done.pause_reason, done.pause_detail)
    assert [s.host_name for s in await _scheds()] == ["ha-b", "ha-a"]


@pytest.mark.asyncio
async def test_master_without_handover_pauses_before_anything_happens_to_it(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock, waves=[
        {"label": "cluster", "group_ids": ["cluster"], "soak_minutes": 1, "carp_handover": False}])
    run = await _start(client, plan)
    paused = await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
    assert paused.pause_reason == "master_needs_handover" and paused.pause_detail["host"] == "ha-a"
    assert fleet.fw["ha-b"].update.call_count == 1 and fleet.fw["ha-a"].update.call_count == 0
    assert fleet.fw["ha-a"].toggles.call_count == 0


@pytest.mark.asyncio
async def test_cluster_gate_before_the_second_node(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock, waves=[
        {"label": "cluster", "group_ids": ["cluster"], "soak_minutes": 1, "carp_handover": True}])
    run = await _start(client, plan)

    async def break_backup_after_update():
        if fleet.fw["ha-b"].version == "26.7.7":
            fleet.fw["ha-b"].services[0]["running"] = 0  # unbound dead on the freshly updated node
    paused = await _until(fleet, clock, run["id"], lambda r: r.status == "paused", check=break_backup_after_update)
    assert paused.pause_reason == "cluster_gate_failed" and paused.pause_detail["host"] == "ha-a"
    assert fleet.fw["ha-a"].update.call_count == 0 and fleet.fw["ha-a"].toggles.call_count == 0


# ── stop when in doubt ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_different_target_version_in_a_later_ring_pauses(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    fleet.fw["ha-b"].latest = "26.7.8"  # a newer release appeared after the canary ran
    run = await _start(client, plan)
    paused = await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
    assert paused.pause_reason == "target_version_changed"
    assert paused.pause_detail == {"expected": "26.7.7", "offered": "26.7.8"}
    assert fleet.fw["ha-b"].update.call_count == 0


@pytest.mark.asyncio
async def test_hosts_without_an_update_are_skipped(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    fleet.fw["ha-b"].offer = "none"
    run = await _start(client, plan)
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
    assert done.status == "succeeded" and _host(done, "ha-b").status == "skipped"
    assert _host(done, "ha-b").detail["code"] == "no_update_offered" and done.waves[1].status == "skipped"


@pytest.mark.asyncio
async def test_failed_host_pauses_and_resume_needs_an_explicit_skip(client, respx_mock, clock):
    fleet, hosts, _groups, plan = await _setup(client, respx_mock)
    fleet.fw["c1"].update_error = True
    run = await _start(client, plan)
    paused = await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
    assert paused.pause_reason == "host_failed" and _host(paused, "c1").status == "failed"
    refused = await client.post(f"/api/rollouts/runs/{run['id']}/resume")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ROLLOUT_HOST_FAILED"
    skip = await client.post(f"/api/rollouts/runs/{run['id']}/skip-host", json={"host_id": hosts["c1"]["id"], "reason": "geprüft, später"})
    assert skip.status_code == 200
    # The failed operation stays "unknown" (locks c1, notifies) until a human closes it; it is not retried.
    assert (await client.post(f"/api/rollouts/runs/{run['id']}/resume")).status_code == 200
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
    assert done.status == "succeeded", (done.pause_reason, done.pause_detail)
    assert fleet.fw["c1"].update.call_count == 1 and _host(done, "c1").detail["code"] == "skipped_by"


@pytest.mark.asyncio
async def test_unreachable_host_in_preflight_is_blocked_then_paused(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    fleet.fw["c1"].up = False
    run = await _start(client, plan)
    paused = await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
    assert paused.pause_reason == "preflight_blocked" and _host(paused, "c1").status == "blocked"
    assert clock.now - datetime.fromisoformat(paused.progress["paused_at"]) < timedelta(minutes=1)
    fleet.fw["c1"].up = True
    assert (await client.post(f"/api/rollouts/runs/{run['id']}/resume")).status_code == 200
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
    assert done.status == "succeeded"


@pytest.mark.asyncio
async def test_someone_elses_open_maintenance_blocks_then_pauses(client, respx_mock, clock):
    fleet, hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    await _until(fleet, clock, run["id"], lambda r: r.current_wave == 1 and r.status == "wave_running")
    async with SessionLocal() as db:
        db.add(HostOperation(host_id=uuid.UUID(hosts["ha-a"]["id"]), host_name="ha-a", actor="someone", action="reboot",
                             status="submitted", expires_at=clock.now, fingerprint="", snapshot={}))
        await db.commit()
    paused = await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
    assert paused.pause_reason in ("blocked_by_maintenance", "cluster_gate_failed")
    assert fleet.fw["ha-b"].update.call_count == 0


@pytest.mark.asyncio
async def test_actor_who_lost_the_right_pauses_the_run(client, respx_mock, clock):
    from tests.test_operator_role import _operator
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    op = await _operator(client, "rollengine-op")
    try:
        run = await _start(client, plan, api=op)
        assert run["started_by"] == "rollengine-op"
        await _until(fleet, clock, run["id"], lambda r: r.status == "soaking")
        await client.put("/api/settings/governance", json={"operators_may_deploy": False})
        paused = await _until(fleet, clock, run["id"], lambda r: r.status == "paused")
        assert paused.pause_reason == "actor_invalid" and fleet.fw["ha-b"].update.call_count == 0
        # An admin takes over by resuming.
        assert (await client.post(f"/api/rollouts/runs/{run['id']}/resume")).json()["started_by"] == "admin"
        done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
        assert done.status == "succeeded"
    finally:
        await op.aclose()
        await client.put("/api/settings/governance", json={"operators_may_deploy": True})


# ── windows, approval, skip, abort, automatic start ───────────────────────────

async def _window_now(client, clock, *, cutoff=60, duration=120):
    local = clock.now
    body = {"name": f"jetzt-{uuid.uuid4().hex[:6]}", "timezone": "UTC", "recurrence": "weekly", "weekdays": [local.weekday()],
            "start_time": (local - timedelta(minutes=1)).strftime("%H:%M"), "duration_minutes": duration, "start_cutoff_minutes": cutoff}
    if (local - timedelta(minutes=1)).date() != local.date():
        pytest.skip("too close to midnight for a same-day window")
    resp = await client.post("/api/maintenance-windows", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_closed_window_waits_and_the_next_occurrence_resumes(client, respx_mock, clock):
    window = await _window_now(client, clock, cutoff=15)
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock, window_id=window["id"])
    run = await _start(client, plan)
    waiting = await _until(fleet, clock, run["id"], lambda r: r.status in ("waiting_window", "paused", "succeeded"))
    assert waiting.status == "waiting_window" and waiting.current_wave >= 1
    started = {s.host_name for s in await _scheds()}
    assert "c1" in started and "ha-a" not in started
    clock.now += timedelta(days=7) - timedelta(minutes=40)  # one week later, a few minutes into the next occurrence
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
    assert done.status == "succeeded", (done.pause_reason, done.pause_detail)


@pytest.mark.asyncio
async def test_approval_holds_the_wave(client, respx_mock, clock):
    fleet, _hosts, groups, plan = await _setup(client, respx_mock, waves=[
        {"label": "canary", "group_ids": ["canary"], "soak_minutes": 1},
        {"label": "rest", "group_ids": ["cluster"], "soak_minutes": 1, "carp_handover": True, "require_approval": True}])
    run = await _start(client, plan)
    held = await _until(fleet, clock, run["id"], lambda r: r.status == "awaiting_approval")
    for _ in range(10):
        await _tick(fleet, clock)
    assert fleet.fw["ha-b"].update.call_count == 0
    resp = await client.post(f"/api/rollouts/runs/{run['id']}/approve")
    assert resp.status_code == 200 and resp.json()["waves"][1]["approved_by"] == "admin"
    done = await _until(fleet, clock, run["id"], lambda r: r.status in ("succeeded", "paused"))
    assert done.status == "succeeded" and held.current_wave == 1


@pytest.mark.asyncio
async def test_abort_cancels_what_has_not_started_and_lets_the_running_host_finish(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    await _until(fleet, clock, run["id"], lambda r: any(h.status == "running" for h in r.hosts))
    bad = await client.post(f"/api/rollouts/runs/{run['id']}/abort", json={"reason": "Notfall", "confirm_name": "falsch"})
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "ROLLOUT_CONFIRM_NAME"
    resp = await client.post(f"/api/rollouts/runs/{run['id']}/abort", json={"reason": "Notfall", "confirm_name": "ringe"})
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    for _ in range(40):
        await _tick(fleet, clock)
    run_now = await _run(run["id"])
    assert run_now.status == "cancelled"
    assert {h.host_name: h.status for h in run_now.hosts} in (
        {"c1": "succeeded", "ha-b": "cancelled", "ha-a": "cancelled"}, {"c1": "cancelled", "ha-b": "cancelled", "ha-a": "cancelled"})
    assert fleet.fw["ha-b"].update.call_count == 0 and fleet.fw["ha-a"].update.call_count == 0
    assert (await client.post(f"/api/rollouts/runs/{run['id']}/resume")).status_code == 409


@pytest.mark.asyncio
async def test_pause_by_hand_and_operator_may_always_brake(client, respx_mock, clock):
    from tests.test_operator_role import _operator
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    await client.put("/api/settings/governance", json={"operators_may_deploy": False})
    op = await _operator(client, "brake-op")
    try:
        resp = await op.post(f"/api/rollouts/runs/{run['id']}/pause", json={"reason": "Bauchgefühl"})
        assert resp.status_code == 200 and resp.json()["pause_reason"] == "manual"
        denied = await op.post(f"/api/rollouts/runs/{run['id']}/resume")
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "DEPLOY_NEEDS_ADMIN"
    finally:
        await op.aclose()
        await client.put("/api/settings/governance", json={"operators_may_deploy": True})
    for _ in range(10):
        await _tick(fleet, clock)
    assert sum(fw.update.call_count for fw in fleet.fw.values()) == 0
    second = await client.post(f"/api/rollouts/plans/{plan['id']}/start", json={
        "preview_token": (await client.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()["preview_token"],
        "confirm_name": "ringe", "acknowledge_unattended": True})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "ROLLOUT_ALREADY_ACTIVE"


async def _until_step_pending(fleet, clock, run_id):
    """Right after the engine created a host's schedule (fire_due has not seen it yet)."""
    run = await _until(fleet, clock, run_id, lambda r: any(h.status == "running" for h in r.hosts))
    name = next(h.host_name for h in run.hosts if h.status == "running")
    assert [s.status for s in await _scheds()] == ["pending"]
    return name


async def _resume_to_the_end(client, fleet, clock, run_id):
    assert (await client.post(f"/api/rollouts/runs/{run_id}/resume")).status_code == 200
    done = await _until(fleet, clock, run_id, lambda r: r.status in ("succeeded", "paused"), check=_no_two_at_once)
    assert done.status == "succeeded", (done.pause_reason, done.pause_detail)
    assert all(fw.update.call_count == 1 for fw in fleet.fw.values())


@pytest.mark.asyncio
async def test_pause_while_a_step_is_pending_really_brakes_and_resume_runs_it_once(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    name = await _until_step_pending(fleet, clock, run["id"])
    resp = await client.post(f"/api/rollouts/runs/{run['id']}/pause", json={"reason": "halt"})
    assert resp.status_code == 200
    for _ in range(10):
        await _tick(fleet, clock)
    assert fleet.fw[name].update.call_count == 0  # the pending step did not fire behind the pause
    paused = await _run(run["id"])
    assert paused.status == "paused" and _host(paused, name).status == "queued"
    assert _host(paused, name).scheduled_maintenance_id is None
    assert [s.status for s in await _scheds()] == ["cancelled"]
    await _resume_to_the_end(client, fleet, clock, run["id"])


@pytest.mark.asyncio
async def test_a_step_found_long_overdue_after_an_outage_is_not_fired(client, respx_mock, clock):
    fleet, _hosts, _groups, plan = await _setup(client, respx_mock)
    run = await _start(client, plan)
    name = await _until_step_pending(fleet, clock, run["id"])
    clock.now += timedelta(minutes=30)  # OPNexus was down (e.g. a web update); the window may be over by now
    await _tick(fleet, clock)
    assert fleet.fw[name].update.call_count == 0
    paused = await _run(run["id"])
    assert (paused.status, paused.pause_reason) == ("paused", "host_not_started")
    assert _host(paused, name).status == "queued" and _host(paused, name).detail["code"] == "not_fired"
    (sched,) = await _scheds()
    assert sched.status == "failed" and sched.result["not_fired"]
    await _resume_to_the_end(client, fleet, clock, run["id"])


@pytest.mark.asyncio
async def test_automatic_start_once_per_occurrence_and_only_while_unchanged(client, respx_mock, clock):
    window = await _window_now(client, clock)
    fleet, _hosts, groups, plan = await _setup(client, respx_mock, window_id=window["id"], auto_start=True)
    token = (await client.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()["preview_token"]
    assert (await client.post(f"/api/rollouts/plans/{plan['id']}/activate", json={"preview_token": token})).status_code == 200
    for _ in range(3):
        async with SessionLocal() as db:
            await engine.auto_start(db)
    async with SessionLocal() as db:
        runs = (await db.scalars(select(RolloutRun))).all()
    assert len(runs) == 1 and runs[0].trigger == "window" and runs[0].started_by == "admin"
    assert runs[0].window_occurrence is not None

    # Next week, but somebody added a host to the canary group meanwhile: no start, a notification instead.
    async with SessionLocal() as db:
        (await db.get(RolloutRun, runs[0].id)).status = "cancelled"
        await db.commit()
    await client.put(f"/api/groups/{groups['canary']}", json={"name": "canary", "host_ids": [_hosts["c1"]["id"], _hosts["ha-a"]["id"]]})
    clock.now += timedelta(days=7)
    async with SessionLocal() as db:
        await engine.auto_start(db)
    async with SessionLocal() as db:
        assert len((await db.scalars(select(RolloutRun))).all()) == 1
        stored = await db.get(RolloutPlan, uuid.UUID(plan["id"]))
    assert stored.auto_start_error["code"] == "plan_changed"
    from app.services import notification_checks
    async with SessionLocal() as db:
        problems = await notification_checks.check_rollout_start_failed(db)
    assert list(problems.values())[0]["grund"] == "plan_changed"


@pytest.mark.asyncio
async def test_a_second_backend_waiting_for_the_lock_reports_no_refusal(client, respx_mock, clock, monkeypatch):
    window = await _window_now(client, clock)
    _fleet, _hosts, _groups, plan = await _setup(client, respx_mock, window_id=window["id"], auto_start=True)
    token = (await client.post(f"/api/rollouts/plans/{plan['id']}/preview")).json()["preview_token"]
    assert (await client.post(f"/api/rollouts/plans/{plan['id']}/activate", json={"preview_token": token})).status_code == 200
    real_lock = engine.policy_lock

    async def racing_lock(db):
        monkeypatch.setattr(engine, "policy_lock", real_lock)
        async with SessionLocal() as other:  # backend A starts the run while B waits for the lock
            assert await engine.auto_start(other) == 1
        await real_lock(db)
    monkeypatch.setattr(engine, "policy_lock", racing_lock)
    async with SessionLocal() as db:
        assert await engine.auto_start(db) == 0
    async with SessionLocal() as db:
        assert len((await db.scalars(select(RolloutRun))).all()) == 1
        assert (await db.get(RolloutPlan, uuid.UUID(plan["id"]))).auto_start_error is None


def test_target_version_comes_from_the_core_package_not_product_latest():
    # Shape seen live on test-fw-3 (26.7.1_1): product_latest lagged behind what would really be installed.
    status = {"status": "update", "product": {"product_latest": "26.7.2"},
              "all_packages": {"opnsense": {"old": "26.7.1_1", "new": "26.7.7", "reason": "upgrade"}},
              "upgrade_packages": [{"name": "opnsense", "current_version": "26.7.1_1", "new_version": "26.7.7"}]}
    assert engine.target_of(status, "update") == "26.7.7"
    assert engine.target_of({**status, "all_packages": {}}, "update") == "26.7.7"
    assert engine.target_of({"product": {"product_latest": "26.7.7"}}, "update") == "26.7.7"
    assert engine.target_of({"upgrade_major_version": "27.1"}, "major_update") == "27.1"
