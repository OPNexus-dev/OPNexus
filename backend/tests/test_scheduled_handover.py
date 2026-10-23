"""Scheduled maintenance with the opt-in CARP handover: MASTER -> maintenance -> partner MASTER ->
action -> node back -> leave maintenance. A tiny fake cluster stands in for the two firewalls."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import HostOperation
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services import backup_store, scheduled_maintenance_service as svc
from tests.test_rules import _create_host

A, B = "https://fw-a.test", "https://fw-b.test"


class FakeCluster:
    """fw-a is the node being maintained (MASTER), fw-b the partner."""

    def __init__(self, respx_mock, *, toggle_flips=True, partner_follows=True, toggle_error=None, firmware="update"):
        self.mock = respx_mock
        self.roles = {A: {"role": "MASTER", "maintenance": False}, B: {"role": "BACKUP", "maintenance": False}}
        self.job = "ready"  # what fw-a's firmware/running reports: ready | running | down
        self.toggle_flips, self.partner_follows, self.toggle_error, self.firmware = toggle_flips, partner_follows, toggle_error, firmware
        self.toggles = respx_mock.post(f"{A}/api/diagnostics/interface/carp_status/maintenance").mock(side_effect=self._toggle)
        self.update = respx_mock.post(f"{A}/api/core/firmware/update").respond(200, json={"status": "ok"})
        self.reboot = respx_mock.post(f"{A}/api/core/system/reboot").respond(200, json={"status": "ok"})
        self.backup = respx_mock.get(f"{A}/api/core/backup/download/this").respond(200, content=b"<opnsense/>")
        respx_mock.get(f"{A}/api/core/snapshots/isSupported").respond(200, json={"supported": False})
        for base in (A, B):
            respx_mock.get(f"{base}/api/diagnostics/interface/getVipStatus").mock(side_effect=lambda r, b=base: self._vip(b))
            respx_mock.get(f"{base}/api/core/firmware/info").respond(200, json={"product_version": "26.7.5"})
            respx_mock.get(f"{base}/api/core/firmware/status").respond(
                200, json={"status": firmware, "all_packages": {}, "status_msg": "x", "status_reboot": "0"})
        respx_mock.get(f"{A}/api/core/firmware/running").mock(side_effect=self._running_a)
        respx_mock.get(f"{B}/api/core/firmware/running").respond(200, json={"status": "ready"})

    def _vip(self, base):
        if base == A and self.job == "down":
            raise httpx.ConnectError("rebooting")
        cur = self.roles[base]
        return httpx.Response(200, json={"rows": [{"vhid": "1", "status": cur["role"]}], "carp": {
            "allow": "1", "maintenancemode": cur["maintenance"], "demotion": "240" if cur["maintenance"] else "0", "status_msg": ""}})

    def _running_a(self, request):
        if self.job == "down":
            raise httpx.ConnectError("rebooting")
        return httpx.Response(200, json={"status": self.job})

    def _toggle(self, request):
        if self.toggle_error:
            raise self.toggle_error
        cur = self.roles[A]
        cur["maintenance"] = not cur["maintenance"]
        entering = cur["maintenance"]
        if self.toggle_flips:
            cur["role"] = "BACKUP" if entering else "MASTER"
            if self.partner_follows:
                self.roles[B]["role"] = "MASTER" if entering else "BACKUP"
        return httpx.Response(200, json={"status": "ok", "action": "enter_maintenance" if entering else "leave_maintenance"})


async def _setup(client, **kwargs):
    a = await _create_host(client, "fw-a", A)
    b = await _create_host(client, "fw-b", B)
    await client.patch(f"/api/hosts/{a['id']}", json={"cluster_name": "ha"})
    await client.patch(f"/api/hosts/{b['id']}", json={"cluster_name": "ha"})
    return a, b


async def _plan(host, action="update", handover=True):
    async with SessionLocal() as db:
        s = ScheduledMaintenance(
            host_id=uuid.UUID(host["id"]), host_name=host["name"], action=action, carp_handover=handover,
            run_at=datetime.now(timezone.utc) - timedelta(minutes=1), actor="admin", created_at=datetime.now(timezone.utc))
        db.add(s)
        await db.commit()
        return s.id


async def _fire():
    async with SessionLocal() as db:
        return await svc.fire_due(db)


async def _advance():
    async with SessionLocal() as db:
        return await svc.advance_running(db)


async def _get(sched_id):
    async with SessionLocal() as db:
        s = await db.get(ScheduledMaintenance, sched_id)
        op = await db.get(HostOperation, s.host_operation_id) if s.host_operation_id else None
        return s, op


async def _age(sched_id, *, phase=None, job=None):
    """Pretend the phase (or the job) started `phase`/`job` ago."""
    async with SessionLocal() as db:
        s = await db.get(ScheduledMaintenance, sched_id)
        if phase is not None:
            s.phase_since = datetime.now(timezone.utc) - phase
        if job is not None:
            s.progress = {**s.progress, "job_started_at": (datetime.now(timezone.utc) - job).isoformat()}
        await db.commit()


# ── happy paths ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_with_handover_runs_end_to_end(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    a, _b = await _setup(client)
    sid = await _plan(a, "update")

    assert (await _fire())["fired"] == 1
    s, op = await _get(sid)
    assert (s.status, s.phase, op.status) == ("running", "handover_wait", "starting")
    assert cluster.backup.call_count == 1 and cluster.toggles.call_count == 1 and cluster.update.call_count == 0
    assert cluster.roles[B]["role"] == "MASTER"

    assert await _advance() == 1  # partner confirmed -> job submitted
    s, op = await _get(sid)
    assert (s.phase, op.status) == ("job_wait", "submitted") and cluster.update.call_count == 1

    cluster.job = "running"
    await _advance()
    cluster.job = "ready"
    await _advance()
    assert (await _get(sid))[0].phase == "leave"
    assert cluster.toggles.call_count == 1  # not back yet

    await _advance()  # leave: toggles once
    assert cluster.toggles.call_count == 2 and (await _get(sid))[0].phase == "leave_wait"
    assert await _advance() == 0  # roles back to normal -> done

    s, op = await _get(sid)
    assert s.status == "succeeded" and s.phase is None and s.result["handover"] is True
    assert s.result["version_before"] == "26.7.5"
    assert op.status == "acknowledged"
    assert cluster.roles[A] == {"role": "MASTER", "maintenance": False}
    assert cluster.update.call_count == 1 and cluster.toggles.call_count == 2


@pytest.mark.asyncio
async def test_reboot_with_handover_needs_to_see_the_node_go_down(client, respx_mock):
    cluster = FakeCluster(respx_mock, firmware="none")
    a, _b = await _setup(client)
    sid = await _plan(a, "reboot")
    await _fire()
    assert cluster.backup.call_count == 0  # no backup for a plain reboot
    await _advance()
    assert cluster.reboot.call_count == 1

    await _advance()  # still "ready" and never seen down: not a finished reboot
    assert (await _get(sid))[0].phase == "job_wait"
    cluster.job = "down"
    await _advance()
    cluster.job = "ready"
    await _advance()
    assert (await _get(sid))[0].phase == "leave"
    await _advance()
    await _advance()
    assert (await _get(sid))[0].status == "succeeded"


@pytest.mark.asyncio
async def test_handover_flag_on_a_backup_node_just_runs_the_action(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    cluster.roles[A].update(role="BACKUP")
    cluster.roles[B].update(role="MASTER")
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    s, _op = await _get(sid)
    assert s.status == "succeeded" and s.phase is None
    assert cluster.toggles.call_count == 0 and cluster.update.call_count == 1


@pytest.mark.asyncio
async def test_without_the_flag_a_master_is_still_refused(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    a, _b = await _setup(client)
    sid = await _plan(a, "update", handover=False)
    await _fire()
    s, _ = await _get(sid)
    assert s.status == "failed" and "CARP-BACKUP" in s.result["error"]
    assert cluster.toggles.call_count == 0 and cluster.update.call_count == 0


# ── failures: what happens to the node ────────────────────────────────────

@pytest.mark.asyncio
async def test_partner_never_takes_over_restores_normal_operation(client, respx_mock):
    cluster = FakeCluster(respx_mock, toggle_flips=False)
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    cluster.roles[A]["maintenance"] = True  # the toggle was accepted, but no role change happened
    await _advance()  # too early: keep waiting
    assert (await _get(sid))[0].phase == "handover_wait" and cluster.toggles.call_count == 1

    await _age(sid, phase=timedelta(minutes=4))
    await _advance()
    s, op = await _get(sid)
    assert s.status == "failed" and "wiederhergestellt" in s.result["error"] and op.status == "cancelled"
    assert cluster.toggles.call_count == 2 and cluster.roles[A]["maintenance"] is False
    assert cluster.update.call_count == 0


@pytest.mark.asyncio
async def test_demoted_node_but_partner_not_master_never_starts_the_job(client, respx_mock):
    """The dangerous case: both nodes BACKUP, nobody carries traffic. The action must not start."""
    cluster = FakeCluster(respx_mock, partner_follows=False)
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    assert cluster.roles[A]["role"] == "BACKUP" and cluster.roles[B]["role"] == "BACKUP"
    await _advance()
    assert (await _get(sid))[0].phase == "handover_wait" and cluster.update.call_count == 0

    await _age(sid, phase=timedelta(minutes=4))
    await _advance()
    s, op = await _get(sid)
    assert s.status == "failed" and op.status == "cancelled" and cluster.update.call_count == 0
    assert cluster.roles[A]["maintenance"] is False  # normal operation restored


@pytest.mark.asyncio
async def test_unclear_toggle_answer_is_never_replayed(client, respx_mock):
    cluster = FakeCluster(respx_mock, toggle_error=httpx.ReadTimeout("timeout"))
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    s, op = await _get(sid)
    assert s.status == "failed" and op.status == "unknown" and "unklar" in s.result["error"]
    assert await _advance() == 0
    assert cluster.toggles.call_count == 1 and cluster.update.call_count == 0


@pytest.mark.asyncio
async def test_job_timeout_leaves_the_node_demoted(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    await _advance()
    cluster.job = "running"
    await _age(sid, job=timedelta(minutes=61))
    await _advance()
    s, op = await _get(sid)
    assert s.status == "failed" and op.status == "unknown" and "Wartungsmodus" in s.result["error"]
    assert cluster.roles[A]["maintenance"] is True and cluster.toggles.call_count == 1  # the safe side: partner keeps traffic


@pytest.mark.asyncio
async def test_blocked_preconditions_change_nothing(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    cluster.roles[B]["role"] = "MASTER"  # no BACKUP partner to hand over to (split-brain-like)
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    s, op = await _get(sid)
    assert s.status == "failed" and op is None
    assert cluster.toggles.call_count == 0 and cluster.update.call_count == 0


@pytest.mark.asyncio
async def test_failed_backup_aborts_before_any_switch(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    respx_mock.get(f"{A}/api/core/backup/download/this").mock(side_effect=httpx.ConnectError("down"))
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    s, _ = await _get(sid)
    assert s.status == "failed" and "nichts wurde verändert" in s.result["error"]
    assert cluster.toggles.call_count == 0


@pytest.mark.asyncio
async def test_run_stops_when_a_human_closes_the_operation(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    _s, op = await _get(sid)
    async with SessionLocal() as db:
        (await db.get(HostOperation, op.id)).status = "cancelled"
        await db.commit()
    assert await _advance() == 0
    s, _ = await _get(sid)
    assert s.status == "failed" and "von Hand" in s.result["error"]
    assert cluster.toggles.call_count == 1 and cluster.update.call_count == 0


@pytest.mark.asyncio
async def test_step_survives_backend_restart(client, respx_mock):
    """Every step reads its state from the row; nothing lives in memory between ticks."""
    cluster = FakeCluster(respx_mock)
    a, _b = await _setup(client)
    sid = await _plan(a, "update")
    await _fire()
    async with SessionLocal() as db:  # a fresh session per step is what a restarted backend does
        assert await svc.advance_running(db) == 1
    async with SessionLocal() as db:
        s = await db.get(ScheduledMaintenance, sid)
        assert s.phase == "job_wait" and s.progress["job_started_at"]


# ── API ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_api_accepts_handover_for_the_master_only_with_the_flag(client, respx_mock):
    FakeCluster(respx_mock)
    a, _b = await _setup(client)
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    url = f"/api/hosts/{a['id']}/maintenance/schedule"

    plain = await client.post(url, json={"action": "update", "run_at": when, "acknowledge_unattended": True})
    assert plain.status_code == 409
    ok = await client.post(url, json={"action": "update", "run_at": when, "acknowledge_unattended": True, "carp_handover": True})
    assert ok.status_code == 200 and ok.json()["carp_handover"] is True and ok.json()["phase"] is None


@pytest.mark.asyncio
async def test_schedule_api_handover_needs_a_cluster(client, respx_mock):
    respx_mock.get("https://solo.test/api/diagnostics/interface/getVipStatus").respond(200, json={"rows": []})
    h = await _create_host(client, "solo", "https://solo.test")
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = await client.post(f"/api/hosts/{h['id']}/maintenance/schedule",
                          json={"action": "reboot", "run_at": when, "acknowledge_unattended": True, "carp_handover": True})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CARP_UEBERGABE_BRAUCHT_EINEN_CLUSTER"
