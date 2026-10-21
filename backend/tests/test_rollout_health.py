"""Health gate checks of the rollout engine, one by one, against a fake firewall pair."""
import uuid

import httpx
import pytest

from app.database import SessionLocal
from app.models.carp_maintenance_window import CarpMaintenanceWindow
from app.models.compliance_result import ComplianceResult
from app.models.host import OpnsenseHost
from app.models.host_operation import HostOperation
from app.services import rollout_health as rh
from tests.test_rules import _create_host

A, B = "https://gw-a.test", "https://gw-b.test"


class Fw:
    def __init__(self, respx_mock, base, *, role="MASTER", version="26.7.7"):
        self.base, self.role, self.version, self.up, self.job, self.maint = base, role, version, True, "ready", False
        self.vips = None
        self.gateways = [{"name": "WAN_GW", "status": "none", "loss": "0.0 %", "delay": "1.0 ms", "stddev": "0.1 ms"}]
        self.services = [{"id": "unbound", "name": "unbound", "locked": 0, "running": 1},
                         {"id": "ntpd", "name": "ntpd", "locked": 0, "running": 1}]
        self.gw_error = False
        r = respx_mock
        r.get(f"{base}/api/diagnostics/interface/getVipStatus").mock(side_effect=self._vip)
        r.get(f"{base}/api/core/firmware/running").mock(side_effect=lambda q: self._ok({"status": self.job}))
        r.get(f"{base}/api/core/firmware/info").mock(side_effect=lambda q: self._ok({"product_version": self.version}))
        r.get(f"{base}/api/routes/gateway/status").mock(side_effect=self._gw)
        r.get(f"{base}/api/core/service/search").mock(side_effect=lambda q: self._ok({"rows": self.services}))

    def _ok(self, data):
        if not self.up:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=data)

    def _gw(self, request):
        if self.gw_error:
            return httpx.Response(403, json={"message": "forbidden"})
        return self._ok({"items": self.gateways, "status": "ok"})

    def _vip(self, request):
        rows = [{"vhid": v, "status": s} for v, s in (self.vips or {"1": self.role}).items()]
        return self._ok({"rows": rows, "carp": {"allow": "1", "maintenancemode": self.maint, "demotion": "0", "status_msg": ""}})


async def _hosts(client, cluster=True):
    a = await _create_host(client, "gw-a", A)
    if not cluster:
        return a, None
    b = await _create_host(client, "gw-b", B)
    if cluster:
        for h in (a, b):
            await client.patch(f"/api/hosts/{h['id']}", json={"cluster_name": "ha"})
    return a, b


async def _eval(host_ids, baselines=None, target="26.7.7", **kw):
    async with SessionLocal() as db:
        hosts = [await db.get(OpnsenseHost, uuid.UUID(h)) for h in host_ids]
        if baselines is None:
            baselines = {str(h.id): await rh.baseline(db, h) for h in hosts}
        return await rh.evaluate(db, hosts, baselines, target_version=target, **kw), baselines


@pytest.mark.asyncio
async def test_healthy_cluster_passes_every_check(client, respx_mock):
    Fw(respx_mock, A)
    Fw(respx_mock, B, role="BACKUP")
    a, _ = await _hosts(client)
    gate, base = await _eval([a["id"]])
    assert gate["verdict"] == "ok", gate
    row = gate["hosts"][a["id"]]
    assert {k: v["result"] for k, v in row.items()} == {
        "online": "ok", "version": "ok", "carp": "ok", "gateways": "ok", "services": "ok", "locks": "ok"}
    assert base[a["id"]]["gateways"] == {"WAN_GW": "online"} and base[a["id"]]["services"]["ntpd"] == 1


@pytest.mark.asyncio
async def test_offline_or_busy_host_and_wrong_version_fail(client, respx_mock):
    fa = Fw(respx_mock, A)
    a, _ = await _hosts(client, cluster=False)
    fa.version = "26.7.6"
    gate, base = await _eval([a["id"]])
    assert gate["hosts"][a["id"]]["version"] == {"result": "fail", "code": "version_mismatch",
                                                 "params": {"expected": "26.7.7", "actual": "26.7.6"}}
    fa.job = "busy"
    gate, _ = await _eval([a["id"]], base)
    assert gate["hosts"][a["id"]]["online"]["code"] == "firmware_busy"
    fa.up = False
    gate, _ = await _eval([a["id"]], base)
    assert gate["hosts"][a["id"]]["online"]["code"] == "offline" and gate["verdict"] == "fail"
    assert gate["hosts"][a["id"]]["gateways"]["result"] == "fail"  # readable before, not now
    assert rh.failed_checks(gate)[0].startswith(a["id"])


@pytest.mark.asyncio
async def test_carp_split_brain_maintenance_mode_and_timer_fail(client, respx_mock):
    fa = Fw(respx_mock, A)
    fb = Fw(respx_mock, B, role="BACKUP")
    a, b = await _hosts(client)
    fb.role = "MASTER"  # both MASTER on VHID 1
    gate, _ = await _eval([a["id"]])
    assert gate["hosts"][a["id"]]["carp"] == {"result": "fail", "code": "vhid_roles", "params": {"vhids": "1"}}
    fb.role = "BACKUP"
    fa.vips, fb.vips = {"1": "MASTER", "2": "MASTER"}, {"1": "BACKUP", "2": "MASTER"}  # partial failover on VHID 2
    gate, _ = await _eval([a["id"]])
    assert gate["hosts"][a["id"]]["carp"]["params"] == {"vhids": "2"}
    fa.vips = fb.vips = None
    fb.maint = True
    gate, _ = await _eval([a["id"]])
    assert gate["hosts"][a["id"]]["carp"]["code"] == "maintenance_mode"
    fb.maint = False
    async with SessionLocal() as db:
        db.add(CarpMaintenanceWindow(host_id=uuid.UUID(b["id"]), host_name="gw-b", status="active"))
        await db.commit()
    gate, _ = await _eval([a["id"]])
    assert gate["hosts"][a["id"]]["carp"]["code"] == "auto_revert_active"
    fb.up = False
    gate, _ = await _eval([a["id"]])
    assert gate["hosts"][a["id"]]["carp"] == {"result": "fail", "code": "node_offline", "params": {"names": "gw-b"}}


@pytest.mark.asyncio
async def test_gateways_compare_against_the_baseline(client, respx_mock):
    fa = Fw(respx_mock, A)
    fa.gateways.append({"name": "OLD_GW", "status": "down", "loss": "100.0 %", "delay": "0.0 ms", "stddev": "0.0 ms"})
    a, _ = await _hosts(client, cluster=False)
    _gate, base = await _eval([a["id"]])
    assert (await _eval([a["id"]], base))[0]["hosts"][a["id"]]["gateways"]["code"] == "gateway_bad_before"  # only warns
    fa.gateways[0] = {"name": "WAN_GW", "status": "down", "loss": "100.0 %", "delay": "~", "stddev": "~"}
    gate, _ = await _eval([a["id"]], base)
    assert gate["hosts"][a["id"]]["gateways"] == {"result": "fail", "code": "gateway_down", "params": {"names": "WAN_GW"}}
    fa.gateways[0] = {"name": "WAN_GW", "status": "loss", "loss": "30 %", "delay": "5 ms", "stddev": "1 ms"}
    assert (await _eval([a["id"]], base))[0]["hosts"][a["id"]]["gateways"]["result"] == "warn"
    fa.gw_error = True
    assert (await _eval([a["id"]], base))[0]["hosts"][a["id"]]["gateways"]["result"] == "fail"
    gate, _ = await _eval([a["id"]], {a["id"]: {**base[a["id"]], "gateways": None}})
    assert gate["hosts"][a["id"]]["gateways"]["result"] == "skip"  # no permission before either: nothing to compare


@pytest.mark.asyncio
async def test_services_that_ran_before_must_run_again(client, respx_mock):
    fa = Fw(respx_mock, A)
    fa.services.append({"id": "dhcpd6", "name": "dhcpd6", "locked": 0, "running": 0})
    a, _ = await _hosts(client, cluster=False)
    _gate, base = await _eval([a["id"]])
    fa.services[1] = {"id": "ntpd", "name": "ntpd", "locked": 0, "running": 0}
    gate, _ = await _eval([a["id"]], base)
    assert gate["hosts"][a["id"]]["services"] == {"result": "fail", "code": "service_stopped", "params": {"names": "ntpd"}}
    fa.services[1]["running"] = 1
    assert (await _eval([a["id"]], base))[0]["hosts"][a["id"]]["services"]["result"] == "ok"  # dhcpd6 never ran: fine


@pytest.mark.asyncio
async def test_open_operation_in_the_cluster_is_a_lock(client, respx_mock):
    Fw(respx_mock, A)
    Fw(respx_mock, B, role="BACKUP")
    a, b = await _hosts(client)
    from datetime import datetime, timezone
    async with SessionLocal() as db:
        db.add(HostOperation(host_id=uuid.UUID(b["id"]), host_name="gw-b", actor="x", action="reboot", status="submitted",
                             expires_at=datetime.now(timezone.utc), fingerprint="", snapshot={}))
        await db.commit()
    gate, _ = await _eval([a["id"]])
    assert gate["hosts"][a["id"]]["locks"] == {"result": "fail", "code": "open_locks", "params": {"count": 1}}


@pytest.mark.parametrize("mode,expected", [("warn", "warn"), ("fail", "fail")])
@pytest.mark.asyncio
async def test_lower_compliance_score_warns_or_fails(client, respx_mock, monkeypatch, mode, expected):
    Fw(respx_mock, A)
    a, _ = await _hosts(client, cluster=False)
    async with SessionLocal() as db:
        db.add(ComplianceResult(host_id=uuid.UUID(a["id"]), checked_at=datetime_now(), score=80, findings=[]))
        await db.commit()

    async def fake_check(db, host):
        row = await db.get(ComplianceResult, (await db.scalar(__import__("sqlalchemy").select(ComplianceResult.id))))
        row.score = 70
        await db.commit()
        return row
    monkeypatch.setattr(rh.compliance_service, "check_host", fake_check)
    gate, _ = await _eval([a["id"]], checks={"compliance": True}, compliance_mode=mode)
    assert gate["hosts"][a["id"]]["compliance"] == {"result": expected, "code": "score_lower", "params": {"before": 80, "after": 70}}
    assert gate["verdict"] == expected
    # the gate cannot be switched off where it matters
    gate, _ = await _eval([a["id"]], checks={"online": False, "version": False, "locks": False})
    assert {"online", "version", "locks"} <= set(gate["hosts"][a["id"]])


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
