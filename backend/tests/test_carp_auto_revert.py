import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import HostOperation
from app.models.carp_maintenance_window import CarpMaintenanceWindow
from app.services import carp_maintenance_window_service, notification_checks
from tests.test_rules import _create_host


async def _backdate_and_acknowledge(client, host, operation_id: str):
    async with SessionLocal() as db:
        op = await db.get(HostOperation, uuid.UUID(operation_id))
        op.started_at = datetime.now(timezone.utc) - timedelta(seconds=6)
        await db.commit()
    resp = await client.post(f"/api/hosts/{host['id']}/maintenance/{operation_id}/acknowledge", json={"confirm_name": host["name"]})
    assert resp.status_code == 200, resp.text


def _stateful_cluster(respx_mock):
    """Same stateful role/maintenance mock shape as
    test_maintenance.py::test_carp_handover_and_return_are_preview_bound_and_verified,
    reused here since the auto-revert sweep needs the same realistic
    toggle-flips-the-role behavior to verify against."""
    roles = {
        "https://fw-primary.test": {"role": "MASTER", "maintenance": False},
        "https://fw-secondary.test": {"role": "BACKUP", "maintenance": False},
    }
    for base in roles:
        def vip_response(request, base=base):
            current = roles[base]
            return httpx.Response(200, json={
                "rows": [{"vhid": "1", "status": current["role"]}],
                "carp": {"allow": "1", "maintenancemode": current["maintenance"], "demotion": "240" if current["maintenance"] else "0", "status_msg": ""},
            })
        respx_mock.get(f"{base}/api/diagnostics/interface/getVipStatus").mock(side_effect=vip_response)
        respx_mock.get(f"{base}/api/core/firmware/running").respond(200, json={"status": "ready"})

    def toggle(request):
        current = roles["https://fw-primary.test"]
        current["maintenance"] = not current["maintenance"]
        if current["maintenance"]:
            current["role"] = "BACKUP"; roles["https://fw-secondary.test"]["role"] = "MASTER"; action = "enter_maintenance"
        else:
            current["role"] = "MASTER"; roles["https://fw-secondary.test"]["role"] = "BACKUP"; action = "leave_maintenance"
        return httpx.Response(200, json={"status": "ok", "action": action})
    switch = respx_mock.post("https://fw-primary.test/api/diagnostics/interface/carp_status/maintenance").mock(side_effect=toggle)
    return roles, switch


async def _make_cluster(client):
    primary = await _create_host(client, "fw-primary", "https://fw-primary.test")
    secondary = await _create_host(client, "fw-secondary", "https://fw-secondary.test")
    await client.patch(f"/api/hosts/{primary['id']}", json={"cluster_name": "ha"})
    await client.patch(f"/api/hosts/{secondary['id']}", json={"cluster_name": "ha"})
    return primary, secondary


@pytest.mark.asyncio
async def test_carp_execute_with_auto_revert_creates_window(client, respx_mock):
    _stateful_cluster(respx_mock)
    primary, secondary = await _make_cluster(client)

    plan = (await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    resp = await client.post(
        f"/api/hosts/{primary['id']}/maintenance/carp-execute",
        json={"preview_id": plan["preview_id"], "confirm_name": "fw-primary", "acknowledge_downtime": True, "auto_revert_minutes": 15},
    )
    assert resp.status_code == 200, resp.text

    async with SessionLocal() as db:
        window = await carp_maintenance_window_service.active_window(db, uuid.UUID(primary["id"]))
    assert window is not None
    assert window.status == "active"
    assert window.auto_revert_at > datetime.now(timezone.utc)

    await _backdate_and_acknowledge(client, primary, resp.json()["id"])

    # The preview for the follow-up carp_leave should surface the window.
    leave_plan = (await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    assert leave_plan["state"]["auto_revert_window"] is not None


@pytest.mark.asyncio
async def test_manual_carp_leave_cancels_pending_window(client, respx_mock):
    _stateful_cluster(respx_mock)
    primary, secondary = await _make_cluster(client)

    plan = (await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    entered = await client.post(
        f"/api/hosts/{primary['id']}/maintenance/carp-execute",
        json={"preview_id": plan["preview_id"], "confirm_name": "fw-primary", "acknowledge_downtime": True, "auto_revert_minutes": 30},
    )
    await _backdate_and_acknowledge(client, primary, entered.json()["id"])

    leave_plan = (await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    assert leave_plan["action"] == "carp_leave"
    resp = await client.post(
        f"/api/hosts/{primary['id']}/maintenance/carp-execute",
        json={"preview_id": leave_plan["preview_id"], "confirm_name": "fw-primary", "acknowledge_downtime": True},
    )
    assert resp.status_code == 200, resp.text

    async with SessionLocal() as db:
        window = await carp_maintenance_window_service.active_window(db, uuid.UUID(primary["id"]))
        assert window is None
        all_windows = (await db.execute(select(CarpMaintenanceWindow))).scalars().all()
    assert len(all_windows) == 1
    assert all_windows[0].status == "cancelled_manual"


@pytest.mark.asyncio
async def test_sweep_ignores_window_not_yet_due(client, respx_mock):
    roles, switch = _stateful_cluster(respx_mock)
    primary, secondary = await _make_cluster(client)
    plan = (await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    await client.post(
        f"/api/hosts/{primary['id']}/maintenance/carp-execute",
        json={"preview_id": plan["preview_id"], "confirm_name": "fw-primary", "acknowledge_downtime": True, "auto_revert_minutes": 30},
    )
    assert switch.call_count == 1

    async with SessionLocal() as db:
        resolved = await carp_maintenance_window_service.sweep_expired(db)
    assert resolved == []
    assert switch.call_count == 1


@pytest.mark.asyncio
async def test_sweep_reverts_expired_window(client, respx_mock):
    roles, switch = _stateful_cluster(respx_mock)
    primary, secondary = await _make_cluster(client)
    plan = (await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    await client.post(
        f"/api/hosts/{primary['id']}/maintenance/carp-execute",
        json={"preview_id": plan["preview_id"], "confirm_name": "fw-primary", "acknowledge_downtime": True, "auto_revert_minutes": 30},
    )
    assert roles["https://fw-primary.test"]["maintenance"] is True

    async with SessionLocal() as db:
        window = await carp_maintenance_window_service.active_window(db, uuid.UUID(primary["id"]))
        window.auto_revert_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        resolved = await carp_maintenance_window_service.sweep_expired(db)

    assert len(resolved) == 1
    assert resolved[0].status == "reverted"
    assert switch.call_count == 2
    assert roles["https://fw-primary.test"]["maintenance"] is False

    async with SessionLocal() as db:
        assert await carp_maintenance_window_service.active_window(db, uuid.UUID(primary["id"])) is None


async def _enter_with_expired_window(client, primary):
    plan = (await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    await client.post(
        f"/api/hosts/{primary['id']}/maintenance/carp-execute",
        json={"preview_id": plan["preview_id"], "confirm_name": "fw-primary", "acknowledge_downtime": True, "auto_revert_minutes": 30},
    )
    async with SessionLocal() as db:
        window = await carp_maintenance_window_service.active_window(db, uuid.UUID(primary["id"]))
        window.auto_revert_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    return plan["preview_id"]


async def test_sweep_revert_closes_the_enter_operation_and_unblocks_deploys(client, respx_mock):
    """Nach dem Auto-Revert darf die carp_enter-Aktion nicht offen bleiben:
    sie ließe sich nie mehr bestätigen und blockiert sonst alle Deploys im Cluster."""
    from app.services.host_maintenance_service import ACTIVE, ensure_no_maintenance

    roles, switch = _stateful_cluster(respx_mock)
    primary, secondary = await _make_cluster(client)
    op_id = await _enter_with_expired_window(client, primary)

    async with SessionLocal() as db:
        op = await db.get(HostOperation, uuid.UUID(op_id))
        assert op.status == "submitted"
        await carp_maintenance_window_service.sweep_expired(db)

    async with SessionLocal() as db:
        op = await db.get(HostOperation, uuid.UUID(op_id))
        assert op.status == "cancelled" and op.status not in ACTIVE
        assert "Automatisch" in op.message
        host = await db.get(__import__("app.models.host", fromlist=["OpnsenseHost"]).OpnsenseHost, uuid.UUID(primary["id"]))
        await ensure_no_maintenance(db, [host])  # raises if still blocked


async def test_sweep_failed_revert_keeps_the_enter_operation_open(client, respx_mock):
    """Schlägt der automatische Rückschalt-Versuch fehl, muss ein Mensch nachsehen:
    die Aktion bleibt offen."""
    roles, switch = _stateful_cluster(respx_mock)
    primary, secondary = await _make_cluster(client)
    op_id = await _enter_with_expired_window(client, primary)
    switch.mock(side_effect=httpx.ConnectError("boom"))

    async with SessionLocal() as db:
        resolved = await carp_maintenance_window_service.sweep_expired(db)
    assert resolved[0].status == "revert_failed"

    async with SessionLocal() as db:
        assert (await db.get(HostOperation, uuid.UUID(op_id))).status == "submitted"


@pytest.mark.asyncio
async def test_sweep_marks_revert_failed_on_toggle_error(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": [{"vhid": "1", "status": "BACKUP"}], "carp": {"allow": "1", "maintenancemode": True, "demotion": "240"}})
    )
    a = await _create_host(client, "fw-a", "https://fw-a.test")

    async with SessionLocal() as db:
        from app.models.host import OpnsenseHost
        host = await db.get(OpnsenseHost, uuid.UUID(a["id"]))
        window = await carp_maintenance_window_service.open_window(db, host, 30)
        window.auto_revert_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    respx_mock.post("https://fw-a.test/api/diagnostics/interface/carp_status/maintenance").mock(side_effect=httpx.ConnectError("down"))

    async with SessionLocal() as db:
        resolved = await carp_maintenance_window_service.sweep_expired(db)
    assert len(resolved) == 1
    assert resolved[0].status == "revert_failed"

    async with SessionLocal() as db:
        problems = await notification_checks.check_carp_auto_revert_failed(db)
    assert len(problems) == 1
    assert next(iter(problems.values()))["host_name"] == "fw-a"


@pytest.mark.asyncio
async def test_settings_roundtrip_carp_auto_revert_failed_toggle(client):
    resp = await client.get("/api/notifications/settings")
    assert resp.json()["carp_auto_revert_failed_enabled"] is True

    resp = await client.put("/api/notifications/settings", json={"carp_auto_revert_failed_enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["carp_auto_revert_failed_enabled"] is False
