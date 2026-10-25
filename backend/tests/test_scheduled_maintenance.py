import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import HostOperation
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services import backup_store, scheduled_maintenance_service
from tests.test_maintenance import mock_reads
from tests.test_rules import _create_host


async def schedule(client, h, action="reboot", run_at=None, **kwargs):
    run_at = run_at or (datetime.now(timezone.utc) + timedelta(hours=1))
    return await client.post(
        f"/api/hosts/{h['id']}/maintenance/schedule",
        json={"action": action, "run_at": run_at.isoformat(), "acknowledge_unattended": True, **kwargs},
    )


@pytest.mark.asyncio
async def test_schedule_requires_future_time(client, respx_mock):
    mock_reads(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    r = await schedule(client, h, run_at=past)
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_schedule_create_list_cancel(client, respx_mock):
    mock_reads(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    created = await schedule(client, h)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "pending"

    listed = await client.get(f"/api/hosts/{h['id']}/maintenance/schedule")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    cancelled = await client.delete(f"/api/hosts/{h['id']}/maintenance/schedule/{body['id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    # Cancelling twice is rejected — no longer pending.
    again = await client.delete(f"/api/hosts/{h['id']}/maintenance/schedule/{body['id']}")
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_fire_due_reboot_success(client, respx_mock):
    mock_reads(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    reboot = respx_mock.post("https://fw1.test/api/core/system/reboot").respond(200, json={"status": "ok"})

    async with SessionLocal() as db:
        sched = ScheduledMaintenance(
            host_id=uuid.UUID(h["id"]), host_name="fw1", action="reboot",
            run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            actor="admin", created_at=datetime.now(timezone.utc),
        )
        db.add(sched)
        await db.commit()

        result = await scheduled_maintenance_service.fire_due(db)
        assert result == {"due": 1, "fired": 1, "blocked_or_failed": 0, "postponed": 0}

        refreshed = await db.get(ScheduledMaintenance, sched.id)
        assert refreshed.status == "succeeded"
        ops = (await db.scalars(select(HostOperation).where(HostOperation.host_id == uuid.UUID(h["id"])))).all()
        assert len(ops) == 1 and ops[0].status == "submitted"
    assert reboot.call_count == 1


async def _due_update(h, action="update"):
    async with SessionLocal() as db:
        sched = ScheduledMaintenance(
            host_id=uuid.UUID(h["id"]), host_name="fw1", action=action,
            run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            actor="admin", created_at=datetime.now(timezone.utc),
        )
        db.add(sched)
        await db.commit()
        return sched.id


@pytest.mark.asyncio
async def test_fire_due_update_takes_its_own_backup_first(client, respx_mock):
    mock_reads(respx_mock, status="update")
    h = await _create_host(client, "fw1", "https://fw1.test")
    download = respx_mock.get("https://fw1.test/api/core/backup/download/this").respond(200, content=b"<opnsense/>")
    update = respx_mock.post("https://fw1.test/api/core/firmware/update").respond(200, json={"status": "ok"})
    sched_id = await _due_update(h)

    async with SessionLocal() as db:
        assert await scheduled_maintenance_service.fire_due(db) == {"due": 1, "fired": 1, "blocked_or_failed": 0, "postponed": 0}
        assert (await db.get(ScheduledMaintenance, sched_id)).status == "succeeded"
    assert download.call_count == 1 and update.call_count == 1
    assert len(backup_store.list_snapshots(uuid.UUID(h["id"]))) == 1


@pytest.mark.asyncio
async def test_fire_due_update_fails_closed_when_backup_fails(client, respx_mock):
    mock_reads(respx_mock, status="update")
    h = await _create_host(client, "fw1", "https://fw1.test")
    respx_mock.get("https://fw1.test/api/core/backup/download/this").mock(side_effect=httpx.ConnectError("down"))
    update = respx_mock.post("https://fw1.test/api/core/firmware/update").respond(200, json={"status": "ok"})
    sched_id = await _due_update(h)

    async with SessionLocal() as db:
        assert await scheduled_maintenance_service.fire_due(db) == {"due": 1, "fired": 0, "blocked_or_failed": 1, "postponed": 0}
        refreshed = await db.get(ScheduledMaintenance, sched_id)
        assert refreshed.status == "failed"
        assert "Backup" in refreshed.result["error"]
    assert update.call_count == 0


@pytest.mark.asyncio
async def test_fire_due_reboot_takes_no_backup(client, respx_mock):
    mock_reads(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    respx_mock.post("https://fw1.test/api/core/system/reboot").respond(200, json={"status": "ok"})
    sched_id = await _due_update(h, action="reboot")  # no backup route mocked: a download would raise
    async with SessionLocal() as db:
        await scheduled_maintenance_service.fire_due(db)
        assert (await db.get(ScheduledMaintenance, sched_id)).status == "succeeded"


@pytest.mark.asyncio
async def test_fire_due_blocked_by_active_operation_on_cluster_peer(client, respx_mock):
    mock_reads(respx_mock, base="https://fw1.test")
    mock_reads(respx_mock, base="https://fw2.test")
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    async with SessionLocal() as db:
        from app.models.host import OpnsenseHost
        host1 = await db.get(OpnsenseHost, uuid.UUID(h1["id"]))
        host2 = await db.get(OpnsenseHost, uuid.UUID(h2["id"]))
        host1.cluster_name = "seg"
        host2.cluster_name = "seg"
        peer_op = HostOperation(
            host_id=host2.id, host_name="fw2", actor="admin", action="reboot", status="starting",
            expires_at=datetime.now(timezone.utc), fingerprint="", snapshot={},
        )
        db.add(peer_op)
        sched = ScheduledMaintenance(
            host_id=host1.id, host_name="fw1", action="reboot",
            run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            actor="admin", created_at=datetime.now(timezone.utc),
        )
        db.add(sched)
        await db.commit()

        result = await scheduled_maintenance_service.fire_due(db)
        assert result == {"due": 1, "fired": 0, "blocked_or_failed": 1, "postponed": 0}
        refreshed = await db.get(ScheduledMaintenance, sched.id)
        assert refreshed.status == "failed"


# ── CARP role is checked when the plan is created, not first at 3 a.m. ────

def _cluster_roles(respx_mock, roles):
    for base, role in roles.items():
        if role is None:
            respx_mock.get(f"{base}/api/diagnostics/interface/getVipStatus").mock(side_effect=httpx.ConnectError("down"))
        else:
            respx_mock.get(f"{base}/api/diagnostics/interface/getVipStatus").respond(
                200, json={"rows": [{"vhid": "1", "status": role}], "carp": {"allow": "1", "maintenancemode": "0", "demotion": "0"}}
            )
        respx_mock.get(f"{base}/api/core/firmware/running").respond(200, json={"status": "ready"})


async def _cluster(client, a_url="https://fw-a.test", b_url="https://fw-b.test"):
    a = await _create_host(client, "fw-a", a_url)
    b = await _create_host(client, "fw-b", b_url)
    await client.patch(f"/api/hosts/{a['id']}", json={"cluster_name": "ha"})
    await client.patch(f"/api/hosts/{b['id']}", json={"cluster_name": "ha"})
    return a, b


@pytest.mark.asyncio
async def test_schedule_on_carp_master_is_rejected_up_front(client, respx_mock):
    _cluster_roles(respx_mock, {"https://fw-a.test": "MASTER", "https://fw-b.test": "BACKUP"})
    a, b = await _cluster(client)

    rejected = await schedule(client, a, action="update")
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "GEPLANTE_WARTUNG_NUR_AUF_DEM_CARP_BACKUP_MOEGLICH"
    assert (await client.get(f"/api/hosts/{a['id']}/maintenance/schedule")).json() == []

    # The backup node is where unattended maintenance belongs.
    accepted = await schedule(client, b, action="update")
    assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
async def test_schedule_accepted_when_role_cannot_be_determined(client, respx_mock):
    # Unreachable now does not mean wrong at run time — the full check still runs when it fires.
    _cluster_roles(respx_mock, {"https://fw-a.test": None, "https://fw-b.test": "MASTER"})
    a, _b = await _cluster(client)
    assert (await schedule(client, a)).status_code == 200


@pytest.mark.asyncio
async def test_schedule_on_standalone_host_skips_role_check(client, respx_mock):
    mock_reads(respx_mock, carp="MASTER")
    h = await _create_host(client, "fw1", "https://fw1.test")
    assert (await schedule(client, h)).status_code == 200


# ── a plan that did not run is reported ───────────────────────────────────

async def _only_enable(db, flag):
    from app.services import notification_settings_service
    row = await notification_settings_service.get_settings(db)
    for column in row.__table__.columns.keys():
        if column.endswith("_enabled"):
            setattr(row, column, column == flag)
    row.webhook_enabled = True
    row.webhook_url = "https://hooks.test/notify"
    await db.commit()


@pytest.mark.asyncio
async def test_failed_schedule_is_notified_once_and_closes_silently(client, respx_mock):
    from app.models.notification import NotificationProblem
    from app.services.notification_service import run_notification_checks

    mock_reads(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    hook = respx_mock.post("https://hooks.test/notify").respond(200)

    async with SessionLocal() as db:
        await _only_enable(db, "scheduled_maintenance_failed_enabled")
        now = datetime.now(timezone.utc)
        failed = ScheduledMaintenance(
            host_id=uuid.UUID(h["id"]), host_name="fw1", action="update", run_at=now - timedelta(hours=2),
            status="failed", actor="admin", created_at=now - timedelta(days=1),
            result={"error": "Cluster-Wartung nur auf dem CARP-BACKUP."},
        )
        ok = ScheduledMaintenance(
            host_id=uuid.UUID(h["id"]), host_name="fw1", action="reboot", run_at=now - timedelta(hours=1),
            status="succeeded", actor="admin", created_at=now - timedelta(days=1),
        )
        old = ScheduledMaintenance(
            host_id=uuid.UUID(h["id"]), host_name="fw1", action="reboot", run_at=now - timedelta(days=10),
            status="failed", actor="admin", created_at=now - timedelta(days=11),
        )
        db.add_all([failed, ok, old])
        await db.commit()

        assert (await run_notification_checks(db))["opened"] == 1
        assert hook.call_count == 1
        sent = json.loads(hook.calls.last.request.content)
        assert sent["problem_type"] == "scheduled_maintenance_failed"
        assert sent["context"]["host_name"] == "fw1" and "CARP-BACKUP" in sent["context"]["message"]

        # Still failed on the next poll: no second message.
        assert (await run_notification_checks(db))["opened"] == 0
        assert hook.call_count == 1

        # Once it drops out of the window nothing was "fixed" — no "behoben" message.
        failed.run_at = now - timedelta(days=4)
        await db.commit()
        result = await run_notification_checks(db)
        assert result["resolved"] == 1
        assert hook.call_count == 1
        problem = (await db.execute(select(NotificationProblem).where(NotificationProblem.problem_type == "scheduled_maintenance_failed"))).scalar_one()
        assert problem.status == "resolved"


@pytest.mark.asyncio
async def test_settings_roundtrip_scheduled_maintenance_failed_toggle(client):
    assert (await client.get("/api/notifications/settings")).json()["scheduled_maintenance_failed_enabled"] is True
    resp = await client.put("/api/notifications/settings", json={"scheduled_maintenance_failed_enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["scheduled_maintenance_failed_enabled"] is False
