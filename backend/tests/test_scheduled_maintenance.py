import uuid
from datetime import datetime, timedelta, timezone

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
        assert result == {"due": 1, "fired": 1, "blocked_or_failed": 0}

        refreshed = await db.get(ScheduledMaintenance, sched.id)
        assert refreshed.status == "succeeded"
        ops = (await db.scalars(select(HostOperation).where(HostOperation.host_id == uuid.UUID(h["id"])))).all()
        assert len(ops) == 1 and ops[0].status == "submitted"
    assert reboot.call_count == 1


@pytest.mark.asyncio
async def test_fire_due_update_blocked_without_fresh_backup(client, respx_mock):
    mock_reads(respx_mock, status="update")
    h = await _create_host(client, "fw1", "https://fw1.test")

    async with SessionLocal() as db:
        sched = ScheduledMaintenance(
            host_id=uuid.UUID(h["id"]), host_name="fw1", action="update",
            run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            actor="admin", created_at=datetime.now(timezone.utc),
        )
        db.add(sched)
        await db.commit()

        result = await scheduled_maintenance_service.fire_due(db)
        assert result == {"due": 1, "fired": 0, "blocked_or_failed": 1}
        refreshed = await db.get(ScheduledMaintenance, sched.id)
        assert refreshed.status == "failed"
        assert "Konfigurationsbackup" in refreshed.result["error"]


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
        assert result == {"due": 1, "fired": 0, "blocked_or_failed": 1}
        refreshed = await db.get(ScheduledMaintenance, sched.id)
        assert refreshed.status == "failed"
