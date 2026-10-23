"""Way back to the previous firmware (1.32, B5): rollback point before every update, guided rollback."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.host_operation import HostOperation
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.services import backup_store, scheduled_maintenance_service
from tests.test_maintenance import execute, preview
from tests.test_rules import _create_host
from tests.test_scheduled_maintenance import _due_update

BASE = "https://fw1.test"


class Snapshots:
    """A firewall with ZFS boot environments, as Core/Api/SnapshotsController answers."""

    def __init__(self, mock, *, version="26.7.6", status="update", fail_add=False, hide_new=False, fail_reboot=False,
                 mark_on_activate=True):
        self.rows = [{"uuid": "be-default", "name": "default", "active": "NR", "created_str": "2026-10-01 10:00"}]
        self.fail_add, self.hide_new, self.fail_reboot, self.mark_on_activate = fail_add, hide_new, fail_reboot, mark_on_activate
        self.activated: list[str] = []
        mock.get(f"{BASE}/api/diagnostics/interface/getVipStatus").respond(200, json={"rows": []})
        mock.get(f"{BASE}/api/core/firmware/info").respond(200, json={"product_version": version})
        mock.get(f"{BASE}/api/core/firmware/status").respond(200, json={"status": status, "all_packages": {}, "status_msg": "ok"})
        mock.get(f"{BASE}/api/core/firmware/running").respond(200, json={"status": "ready"})
        mock.get(f"{BASE}/api/core/snapshots/isSupported").respond(200, json={"supported": True})
        self.fail_list = False
        mock.get(f"{BASE}/api/core/snapshots/search").mock(side_effect=self._search)
        self.add = mock.post(f"{BASE}/api/core/snapshots/add").mock(side_effect=self._add)
        self.delete = mock.post(url__regex=rf"{BASE}/api/core/snapshots/del/.*").mock(side_effect=self._del)
        self.activate = mock.post(url__regex=rf"{BASE}/api/core/snapshots/activate/.*").mock(side_effect=self._activate)
        self.update = mock.post(f"{BASE}/api/core/firmware/update").respond(200, json={"status": "ok"})
        self.upgrade = mock.post(f"{BASE}/api/core/firmware/upgrade").respond(200, json={"status": "ok"})
        self.reboot = mock.post(f"{BASE}/api/core/system/reboot").mock(side_effect=self._reboot)
        self.download = mock.get(f"{BASE}/api/core/backup/download/this").respond(200, content=b"<opnsense/>")

    def _search(self, request):
        if self.fail_list:
            return httpx.Response(502, json={})
        return httpx.Response(200, json={"rows": self.rows})

    def point(self, name: str, active: str = "-") -> None:
        self.rows.append({"uuid": f"be-{name}", "name": name, "active": active, "created_str": "x"})

    def _add(self, request):
        if self.fail_add:
            return httpx.Response(200, json={"status": "failed", "validations": {"snapshot.name": "nope"}})
        import json
        name = json.loads(request.content)["name"]
        if not self.hide_new:
            self.point(name)
        return httpx.Response(200, json={"status": "ok"})

    def _del(self, request):
        uid = str(request.url).rsplit("/", 1)[-1]
        self.rows = [r for r in self.rows if r["uuid"] != uid]
        return httpx.Response(200, json={"status": "ok"})

    def _activate(self, request):
        uid = str(request.url).rsplit("/", 1)[-1]
        self.activated.append(uid)
        if getattr(self, "fail_list_after_activate", False) and len(self.activated) == 1:
            self.fail_list = True
        if self.mark_on_activate:
            for r in self.rows:
                r["active"] = ("N" if "N" in r["active"] else "") + ("R" if r["uuid"] == uid else "")
                r["active"] = r["active"] or "-"
        return httpx.Response(200, json={"status": "ok"})

    def _reboot(self, request):
        if self.fail_reboot:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"status": "ok"})


async def _host_with_backup(client):
    h = await _create_host(client, "fw1", BASE)
    backup_store.save_snapshot(uuid.UUID(h["id"]), b"<opnsense/>")
    return h


async def _op(op_id) -> HostOperation:
    async with SessionLocal() as db:
        return await db.get(HostOperation, uuid.UUID(str(op_id)))


async def test_a_manual_update_creates_a_rollback_point_first_and_keeps_only_two(client, respx_mock):
    fw = Snapshots(respx_mock)
    fw.point("opnexus-pre-26.7.4-20261001-100000")
    fw.point("opnexus-pre-26.7.5-20261010-100000")
    fw.point("handmade")  # never touched
    h = await _host_with_backup(client)
    p = await preview(client, h, "update")
    r = await execute(client, h, p.json())
    assert r.json()["status"] == "submitted" and fw.update.call_count == 1
    names = [row["name"] for row in fw.rows]
    new = next(n for n in names if n.startswith("opnexus-pre-26.7.6-"))
    assert "opnexus-pre-26.7.4-20261001-100000" not in names  # the oldest of three is cleaned up
    assert {"default", "handmade", "opnexus-pre-26.7.5-20261010-100000", new} == set(names)
    op = await _op(r.json()["id"])
    assert op.snapshot["rollback_point"]["name"] == new and op.snapshot["rollback_point"]["version"] == "26.7.6"
    assert fw.add.call_count == 1
    order = [str(c.request.url) for c in respx_mock.calls if c.request.method == "POST"]
    assert order.index(f"{BASE}/api/core/snapshots/add") < order.index(f"{BASE}/api/core/firmware/update")


@pytest.mark.parametrize("problem", ["fail_add", "hide_new"])
async def test_no_rollback_point_no_update(client, respx_mock, problem):
    fw = Snapshots(respx_mock, **{problem: True})
    h = await _host_with_backup(client)
    p = await preview(client, h, "update")
    r = await execute(client, h, p.json())
    assert r.json()["status"] == "cancelled" and "Update nicht gestartet" in r.json()["message"]
    assert fw.update.call_count == 0


async def test_without_zfs_the_update_runs_and_says_there_is_no_way_back(client, respx_mock):
    fw = Snapshots(respx_mock)
    respx_mock.get(f"{BASE}/api/core/snapshots/isSupported").respond(200, json={"supported": False})
    h = await _host_with_backup(client)
    r = await execute(client, h, (await preview(client, h, "update")).json())
    assert r.json()["status"] == "submitted" and "Kein Rückweg-Punkt" in r.json()["message"]
    assert fw.add.call_count == 0 and fw.update.call_count == 1


async def test_a_scheduled_update_fails_closed_without_a_rollback_point(client, respx_mock):
    fw = Snapshots(respx_mock, fail_add=True)
    respx_mock.get(f"{BASE}/api/core/backup/download/this").respond(200, content=b"<opnsense/>")
    h = await _create_host(client, "fw1", BASE)
    sched_id = await _due_update(h)
    async with SessionLocal() as db:
        await scheduled_maintenance_service.fire_due(db)
    async with SessionLocal() as db:
        sched = await db.get(ScheduledMaintenance, sched_id)
    assert sched.status == "failed" and "Rückweg-Punkt" in sched.result["error"]
    assert fw.update.call_count == 0


async def test_rollback_needs_a_point_from_opnexus(client, respx_mock):
    fw = Snapshots(respx_mock, status="none")
    fw.point("handmade")
    h = await _create_host(client, "fw1", BASE)
    r = await preview(client, h, "firmware_rollback")
    assert r.status_code == 409 and "Kein Rückweg-Punkt" in r.json()["detail"]["params"]["reason"]
    assert (await client.post(f"/api/hosts/{h['id']}/snapshots", json={"name": "opnexus-pre-1-20260101-000000"})).status_code == 422


async def test_rollback_activates_the_newest_point_reboots_and_lists_what_is_lost(client, respx_mock):
    fw = Snapshots(respx_mock, version="26.7.7", status="none")
    stamp = datetime.now(timezone.utc) - timedelta(hours=2)
    fw.point(f"opnexus-pre-26.7.5-{(stamp - timedelta(days=5)):%Y%m%d-%H%M%S}")
    fw.point(f"opnexus-pre-26.7.6-{stamp:%Y%m%d-%H%M%S}")
    h = await _create_host(client, "fw1", BASE)
    async with SessionLocal() as db:
        rule = FirewallRule(description="nachher", interface="lan", direction="in")
        db.add(rule)
        await db.flush()
        db.add(RuleDeployment(rule_id=rule.id, host_id=uuid.UUID(h["id"]), status="success", opnsense_rule_uuid="u"))
        await db.commit()
    p = await preview(client, h, "firmware_rollback")
    assert p.status_code == 200, p.text
    rollback = p.json()["state"]["rollback"]
    assert rollback["target"]["version"] == "26.7.6" and rollback["current"]["name"] == "default"
    assert [c["kind"] for c in rollback["changes_since"]] == ["rule"]
    r = await execute(client, h, p.json())
    assert r.json()["status"] == "submitted" and "erwartet 26.7.6" in r.json()["message"]
    assert fw.activated == [rollback["target"]["uuid"]] and fw.reboot.call_count == 1


async def test_rollback_that_is_not_marked_for_the_next_start_never_reboots(client, respx_mock):
    fw = Snapshots(respx_mock, status="none", mark_on_activate=False)
    fw.point("opnexus-pre-26.7.6-20261020-100000")
    h = await _create_host(client, "fw1", BASE)
    r = await execute(client, h, (await preview(client, h, "firmware_rollback")).json())
    assert r.json()["status"] == "unknown" and fw.reboot.call_count == 0
    assert fw.activated == ["be-opnexus-pre-26.7.6-20261020-100000", "be-default"]


async def test_a_failed_reboot_reactivates_the_current_boot_environment(client, respx_mock):
    fw = Snapshots(respx_mock, status="none", fail_reboot=True)
    fw.point("opnexus-pre-26.7.6-20261020-100000")
    h = await _create_host(client, "fw1", BASE)
    r = await execute(client, h, (await preview(client, h, "firmware_rollback")).json())
    assert r.json()["status"] == "unknown"
    assert fw.activated == ["be-opnexus-pre-26.7.6-20261020-100000", "be-default"]
    assert next(row for row in fw.rows if row["uuid"] == "be-default")["active"].endswith("R")


async def test_rollback_on_a_carp_master_is_refused_like_a_reboot(client, respx_mock):
    fw = Snapshots(respx_mock, status="none")
    fw.point("opnexus-pre-26.7.6-20261020-100000")
    h = await _create_host(client, "fw1", BASE)
    await client.patch(f"/api/hosts/{h['id']}", json={"cluster_name": "ha"})
    r = await preview(client, h, "firmware_rollback")
    assert r.status_code == 409 and fw.activate.call_count == 0


async def test_the_rollback_endpoint_needs_the_deploy_right_like_every_maintenance_execute():
    from app.services import api_token_service

    assert api_token_service.is_rollout("POST", "/api/hosts/x/maintenance/execute")
    assert not api_token_service.is_rollout("POST", "/api/hosts/x/maintenance/preview")


async def test_rollback_saves_the_current_configuration_first_and_refuses_without_it(client, respx_mock):
    """Going back discards every change since the point on the firewall: OPNexus keeps a copy of the config first."""
    fw = Snapshots(respx_mock, status="none")
    fw.point("opnexus-pre-26.7.6-20261020-100000")
    download = respx_mock.get(f"{BASE}/api/core/backup/download/this").respond(200, content=b"<opnsense>now</opnsense>")
    h = await _create_host(client, "fw1", BASE)
    r = await execute(client, h, (await preview(client, h, "firmware_rollback")).json())
    assert r.json()["status"] == "submitted" and download.call_count == 1
    assert backup_store.list_snapshots(uuid.UUID(h["id"]))  # stored locally
    order = [str(c.request.url) for c in respx_mock.calls]
    assert order.index(f"{BASE}/api/core/backup/download/this") < order.index(f"{BASE}/api/core/snapshots/activate/be-opnexus-pre-26.7.6-20261020-100000")


async def test_rollback_without_a_configuration_copy_does_not_start(client, respx_mock):
    fw = Snapshots(respx_mock, status="none")
    fw.point("opnexus-pre-26.7.6-20261020-100000")
    respx_mock.get(f"{BASE}/api/core/backup/download/this").mock(side_effect=httpx.ConnectError("down"))
    h = await _create_host(client, "fw1", BASE)
    r = await execute(client, h, (await preview(client, h, "firmware_rollback")).json())
    assert r.json()["status"] == "cancelled" and fw.activate.call_count == 0 and fw.reboot.call_count == 0


async def test_an_unreadable_list_after_activating_switches_back_and_never_reboots(client, respx_mock):
    fw = Snapshots(respx_mock, status="none")
    fw.fail_list_after_activate = True
    fw.point("opnexus-pre-26.7.6-20261020-100000")
    h = await _create_host(client, "fw1", BASE)
    r = await execute(client, h, (await preview(client, h, "firmware_rollback")).json())
    assert r.json()["status"] == "unknown" and fw.reboot.call_count == 0
    assert fw.activated == ["be-opnexus-pre-26.7.6-20261020-100000", "be-default"]


async def test_after_a_rollback_newer_deployments_are_marked_so_the_next_deploy_really_writes(client, respx_mock):
    fw = Snapshots(respx_mock, status="none")
    stamp = datetime.now(timezone.utc) - timedelta(hours=2)
    fw.point(f"opnexus-pre-26.7.6-{stamp:%Y%m%d-%H%M%S}")
    h = await _create_host(client, "fw1", BASE)
    async with SessionLocal() as db:
        old, new = FirewallRule(description="alt", interface="lan", direction="in"), FirewallRule(description="neu", interface="lan", direction="in")
        db.add_all([old, new])
        await db.flush()
        db.add_all([
            RuleDeployment(rule_id=old.id, host_id=uuid.UUID(h["id"]), status="success", opnsense_rule_uuid="u1",
                           last_payload={"x": 1}, deployed_at=stamp - timedelta(days=1)),
            RuleDeployment(rule_id=new.id, host_id=uuid.UUID(h["id"]), status="success", opnsense_rule_uuid="u2",
                           last_payload={"x": 2}, deployed_at=stamp + timedelta(minutes=30)),
        ])
        await db.commit()
    r = await execute(client, h, (await preview(client, h, "firmware_rollback")).json())
    assert r.json()["status"] == "submitted" and "1 Deployment(s) als unklar markiert" in r.json()["message"]
    async with SessionLocal() as db:
        rows = {d.opnsense_rule_uuid: d for d in (await db.scalars(select(RuleDeployment))).all()}
    assert rows["u1"].status == "success" and rows["u1"].last_payload == {"x": 1}  # older than the point: untouched
    assert rows["u2"].status == "error" and rows["u2"].last_payload is None and "Firmware-Rückweg" in rows["u2"].message
