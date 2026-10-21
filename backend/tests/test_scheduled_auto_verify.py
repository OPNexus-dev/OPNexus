"""auto_verify (1.25): an unattended action checks its own result and closes the operation,
so the host/cluster is not left locked until a human clicks "Wartung abschließen"."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import HostOperation
from app.models.audit import AuditEvent
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services import scheduled_maintenance_service as svc
from tests.test_rules import _create_host
from tests.test_scheduled_handover import FakeCluster, _setup

H = "https://fw-solo.test"


class FakeHost:
    """One stand-alone firewall: job = ready | running | down; boot = what systemTime reports."""

    def __init__(self, respx_mock, *, firmware="update", reboot_flag="1", version="26.7.5"):
        self.job, self.boot, self.version = "ready", "Mon Oct 5 10:00:00 UTC 2026", version
        self.update = respx_mock.post(f"{H}/api/core/firmware/update").respond(200, json={"status": "ok"})
        self.reboot = respx_mock.post(f"{H}/api/core/system/reboot").respond(200, json={"status": "ok"})
        respx_mock.get(f"{H}/api/core/backup/download/this").respond(200, content=b"<opnsense/>")
        respx_mock.get(f"{H}/api/diagnostics/interface/getVipStatus").mock(side_effect=self._vip)
        respx_mock.get(f"{H}/api/core/firmware/info").mock(side_effect=lambda r: self._up(json={"product_version": self.version}))
        respx_mock.get(f"{H}/api/core/firmware/status").respond(
            200, json={"status": firmware, "all_packages": {}, "status_msg": "x", "status_reboot": reboot_flag})
        respx_mock.get(f"{H}/api/core/firmware/running").mock(side_effect=lambda r: self._up(json={"status": self.job}))
        respx_mock.get(f"{H}/api/diagnostics/system/systemTime").mock(side_effect=lambda r: self._up(json={"boottime": self.boot}))

    def _up(self, **kw):
        if self.job == "down":
            raise httpx.ConnectError("rebooting")
        return httpx.Response(200, **kw)

    def _vip(self, request):
        return self._up(json={"rows": [], "carp": {"allow": "1", "maintenancemode": False, "demotion": "0", "status_msg": ""}})

    def reboot_now(self, new_boot="Wed Oct 21 03:00:00 UTC 2026", version=None):
        self.boot = new_boot
        if version:
            self.version = version


async def _plan(host, action="update", *, auto_verify=True, handover=False):
    async with SessionLocal() as db:
        s = ScheduledMaintenance(
            host_id=uuid.UUID(host["id"]), host_name=host["name"], action=action, carp_handover=handover,
            auto_verify=auto_verify, run_at=datetime.now(timezone.utc) - timedelta(minutes=1), actor="admin",
            created_at=datetime.now(timezone.utc))
        db.add(s)
        await db.commit()
        return s.id


async def _fire():
    async with SessionLocal() as db:
        return await svc.fire_due(db)


async def _advance():
    async with SessionLocal() as db:
        return await svc.advance_running(db)


async def _get(sid):
    async with SessionLocal() as db:
        s = await db.get(ScheduledMaintenance, sid)
        op = await db.get(HostOperation, s.host_operation_id) if s.host_operation_id else None
        return s, op


async def _shift(sid, *, phase=None, ready=None):
    """Pretend the phase began / the firmware turned ready that long ago."""
    async with SessionLocal() as db:
        s = await db.get(ScheduledMaintenance, sid)
        if phase is not None:
            s.phase_since = datetime.now(timezone.utc) - phase
        if ready is not None:
            s.progress = {**s.progress, "ready_since": (datetime.now(timezone.utc) - ready).isoformat()}
        await db.commit()


async def _settle(sid):
    """verify needs the firmware idle for VERIFY_SETTLE: one tick to notice, then pretend it has been a while."""
    await _advance()
    await _shift(sid, ready=svc.VERIFY_SETTLE + timedelta(seconds=1))
    await _advance()


@pytest.mark.asyncio
async def test_update_is_verified_and_the_operation_closed(client, respx_mock):
    fw = FakeHost(respx_mock)
    host = await _create_host(client, "fw-solo", H)
    sid = await _plan(host)
    await _fire()
    s, op = await _get(sid)
    assert (s.status, s.phase, op.status, fw.update.call_count) == ("running", "job_wait", "submitted", 1)
    assert s.progress["reboot_expected"] is True and s.progress["boot_before"] == fw.boot

    fw.job = "running"
    await _advance()
    fw.job = "down"
    await _advance()
    fw.job = "ready"
    fw.reboot_now(version="26.7.7")
    await _advance()
    assert (await _get(sid))[0].phase == "verify"
    await _settle(sid)

    s, op = await _get(sid)
    assert s.status == "succeeded" and s.phase is None
    assert s.result["auto_verified"] is True and s.result["version_after"] == "26.7.7" and s.result["version_before"] == "26.7.5"
    assert op.status == "acknowledged" and "Automatisch geprüft" in op.message
    assert fw.update.call_count == 1
    # the host is free again: a new preview is not blocked by a forgotten operation
    resp = await client.get(f"/api/hosts/{host['id']}/maintenance")
    assert not any("frühere Wartungsaktion" in b for b in resp.json()["state"]["blocked"])
    async with SessionLocal() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "host"))).all()
    assert {e.actor for e in events if (e.details or {}).get("scheduled")} == {"admin (geplant)"}


@pytest.mark.asyncio
async def test_boot_time_proves_a_reboot_the_poll_never_saw(client, respx_mock):
    fw = FakeHost(respx_mock)
    host = await _create_host(client, "fw-solo", H)
    sid = await _plan(host)
    await _fire()
    await _shift(sid, phase=timedelta(0))
    fw.reboot_now()  # rebooted between two polls: never seen offline
    async with SessionLocal() as db:  # the job grace has passed
        s = await db.get(ScheduledMaintenance, sid)
        s.progress = {**s.progress, "job_started_at": (datetime.now(timezone.utc) - svc.JOB_GRACE).isoformat()}
        await db.commit()
    await _advance()
    assert (await _get(sid))[0].phase == "verify"
    await _settle(sid)
    s, op = await _get(sid)
    assert s.status == "succeeded" and s.result["rebooted"] is True and op.status == "acknowledged"


@pytest.mark.asyncio
async def test_expected_reboot_never_happened_ends_unknown_after_timeout(client, respx_mock):
    FakeHost(respx_mock)
    host = await _create_host(client, "fw-solo", H)
    sid = await _plan(host)
    await _fire()
    async with SessionLocal() as db:
        s = await db.get(ScheduledMaintenance, sid)
        s.progress = {**s.progress, "job_started_at": (datetime.now(timezone.utc) - svc.JOB_GRACE).isoformat()}
        await db.commit()
    await _advance()
    await _settle(sid)
    s, op = await _get(sid)
    assert (s.status, s.phase, op.status) == ("running", "verify", "submitted")  # waiting, not done
    assert "Neustart" in s.progress["waiting_for"]
    await _shift(sid, phase=svc.VERIFY_TIMEOUT)
    await _advance()
    s, op = await _get(sid)
    assert s.status == "failed" and op.status == "unknown" and "nicht bestanden" in s.result["error"]


@pytest.mark.asyncio
async def test_ready_must_hold_a_while_a_short_ready_before_the_reboot_does_not_count(client, respx_mock):
    fw = FakeHost(respx_mock, reboot_flag="0")
    host = await _create_host(client, "fw-solo", H)
    sid = await _plan(host)
    await _fire()
    fw.job = "running"
    await _advance()
    fw.job = "ready"
    await _advance()  # job_wait -> verify
    await _advance()  # ready noticed
    s, _ = await _get(sid)
    assert s.phase == "verify" and s.progress["ready_since"]
    fw.job = "running"  # busy again (e.g. the reboot at the end of the update)
    await _advance()
    assert (await _get(sid))[0].progress["ready_since"] is None
    fw.job = "ready"
    await _settle(sid)
    assert (await _get(sid))[0].status == "succeeded"


@pytest.mark.asyncio
async def test_without_auto_verify_nothing_changes(client, respx_mock):
    fw = FakeHost(respx_mock)
    host = await _create_host(client, "fw-solo", H)
    sid = await _plan(host, auto_verify=False)
    await _fire()
    s, op = await _get(sid)
    assert (s.status, s.phase, op.status, fw.update.call_count) == ("succeeded", None, "submitted", 1)
    assert await _advance() == 0


@pytest.mark.asyncio
async def test_human_closing_the_operation_stops_the_check(client, respx_mock):
    FakeHost(respx_mock)
    host = await _create_host(client, "fw-solo", H)
    sid = await _plan(host)
    await _fire()
    s, op = await _get(sid)
    async with SessionLocal() as db:
        (await db.get(HostOperation, op.id)).status = "cancelled"
        await db.commit()
    await _advance()
    s, op = await _get(sid)
    assert s.status == "failed" and "von Hand beendet" in s.result["error"] and op.status == "cancelled"


@pytest.mark.asyncio
async def test_handover_verifies_before_taking_the_traffic_back(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    cluster.boot = "before"

    def system_time(request):
        if cluster.job == "down":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"boottime": cluster.boot})
    respx_mock.get("https://fw-a.test/api/diagnostics/system/systemTime").mock(side_effect=system_time)
    a, _b = await _setup(client)
    async with SessionLocal() as db:
        s = ScheduledMaintenance(host_id=uuid.UUID(a["id"]), host_name="fw-a", action="update", carp_handover=True, auto_verify=True,
                                 run_at=datetime.now(timezone.utc) - timedelta(minutes=1), actor="admin", created_at=datetime.now(timezone.utc))
        db.add(s)
        await db.commit()
        sid = s.id
    await _fire()
    await _advance()  # partner MASTER -> job submitted
    cluster.job = "down"
    await _advance()
    cluster.job = "ready"
    cluster.boot = "after"
    await _advance()
    s, _ = await _get(sid)
    assert s.phase == "verify" and cluster.toggles.call_count == 1  # still demoted while checking
    await _settle(sid)
    assert (await _get(sid))[0].phase == "leave" and cluster.toggles.call_count == 1
    await _advance()  # leave
    await _advance()  # leave_wait -> done
    s, op = await _get(sid)
    assert s.status == "succeeded" and s.result["handover"] is True and s.result["auto_verified"] is True
    assert s.result["rebooted"] is True  # found live: the handover path dropped what verify had seen
    assert op.status == "acknowledged" and cluster.toggles.call_count == 2 and cluster.update.call_count == 1


@pytest.mark.asyncio
async def test_a_due_row_locked_by_another_backend_is_not_fired_twice(client, respx_mock):
    fw = FakeHost(respx_mock)
    host = await _create_host(client, "fw-solo", H)
    sid = await _plan(host)
    async with SessionLocal() as other:  # another backend is firing this very row right now
        await other.scalar(select(ScheduledMaintenance).where(ScheduledMaintenance.id == sid).with_for_update())
        # Without the row lock this would try to update the locked row and hang: bounded, so it fails instead.
        result = await asyncio.wait_for(_fire(), timeout=10)
        assert result["due"] == 0 and fw.update.call_count == 0
        await other.rollback()
    await _fire()
    assert fw.update.call_count == 1


@pytest.mark.asyncio
async def test_a_running_row_locked_by_another_backend_is_not_stepped_twice(client, respx_mock):
    cluster = FakeCluster(respx_mock)
    a, _b = await _setup(client)
    async with SessionLocal() as db:
        s = ScheduledMaintenance(host_id=uuid.UUID(a["id"]), host_name="fw-a", action="update", carp_handover=True,
                                 run_at=datetime.now(timezone.utc) - timedelta(minutes=1), actor="admin", created_at=datetime.now(timezone.utc))
        db.add(s)
        await db.commit()
        sid = s.id
    await _fire()
    async with SessionLocal() as other:
        await other.scalar(select(ScheduledMaintenance).where(ScheduledMaintenance.id == sid).with_for_update())
        assert await asyncio.wait_for(_advance(), timeout=10) == 1  # still counted as running, but not touched
        assert cluster.update.call_count == 0
        await other.rollback()
    await _advance()
    assert cluster.update.call_count == 1


@pytest.mark.asyncio
async def test_cancel_waits_for_a_firing_row_and_never_overwrites_running(client, respx_mock):
    FakeHost(respx_mock)
    host = await _create_host(client, "fw-solo", H)
    async with SessionLocal() as db:
        s = ScheduledMaintenance(host_id=uuid.UUID(host["id"]), host_name="fw-solo", action="update", auto_verify=True,
                                 run_at=datetime.now(timezone.utc) + timedelta(hours=1), actor="admin", created_at=datetime.now(timezone.utc))
        db.add(s)
        await db.commit()
        sid = s.id
    async with SessionLocal() as firing:  # the poll loop has the row and is about to mark it running
        row = await firing.scalar(select(ScheduledMaintenance).where(ScheduledMaintenance.id == sid).with_for_update())
        cancel = asyncio.create_task(client.delete(f"/api/hosts/{host['id']}/maintenance/schedule/{sid}"))
        await asyncio.sleep(0.5)
        row.status = "running"
        await firing.commit()
    resp = await asyncio.wait_for(cancel, timeout=10)
    assert resp.status_code == 409
    assert (await _get(sid))[0].status == "running"
