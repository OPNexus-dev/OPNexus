"""1.29: missing API rights become visible (OpnsenseForbiddenError, `denied` in the sample, api_rights_missing alert,
dashboard, metrics) and the gateway alert can be switched off per gateway."""
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.host import OpnsenseHost
from app.models.notification import NotificationProblem
from app.models.resource_sample import HostResourceSample
from app.services import monitor_checks, notification_delivery, opnsense_client, resource_monitor_service
from app.services.notification_service import run_notification_checks
from tests.test_monitoring_alerts import (
    STATUS_PAYLOAD, _add_samples, _gw, _gateway_problems, _only, _open_count, _set,
)
from tests.test_notifications import _create_host
from tests.test_resource_monitor import _mock_resource_endpoints, _mock_vip
from tests.helpers import delete_host


async def _host_row(host_id: str) -> OpnsenseHost:
    async with SessionLocal() as db:
        return await db.get(OpnsenseHost, uuid.UUID(host_id))


async def _clear_samples():
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()


async def _rights_problems():
    async with SessionLocal() as db:
        return await monitor_checks.check_api_rights_missing(db)


# ── Client: 403 is told apart from everything else (a 401 is a wrong key, not a missing right) ──────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403])
async def test_403_raises_forbidden_error_with_unchanged_message(client, respx_mock, status):
    _mock_vip(respx_mock)
    host = await _host_row((await _create_host(client, "fw1", "https://fw1.test"))["id"])
    respx_mock.get("https://fw1.test/api/routes/gateway/status").respond(status, text="denied")
    with pytest.raises(opnsense_client.OpnsenseForbiddenError) as exc:
        await opnsense_client.get_gateway_status(host)
    assert isinstance(exc.value, opnsense_client.OpnsenseAPIError)  # existing `except OpnsenseAPIError` stays valid
    assert not isinstance(exc.value, opnsense_client.OpnsenseAmbiguousError)
    assert str(exc.value).startswith(f"{status} /routes/gateway/status")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,ambiguous", [(500, True), (502, True), (404, False), (400, False), (401, False)])
async def test_other_errors_are_not_forbidden(client, respx_mock, status, ambiguous):
    _mock_vip(respx_mock)
    host = await _host_row((await _create_host(client, "fw1", "https://fw1.test"))["id"])
    respx_mock.get("https://fw1.test/api/routes/gateway/status").respond(status)
    with pytest.raises(opnsense_client.OpnsenseAPIError) as exc:
        await opnsense_client.get_gateway_status(host)
    assert not isinstance(exc.value, opnsense_client.OpnsenseForbiddenError)
    assert isinstance(exc.value, opnsense_client.OpnsenseAmbiguousError) is ambiguous


@pytest.mark.asyncio
async def test_gateway_endpoint_reports_missing_rights_not_a_generic_502(client, respx_mock):
    _mock_vip(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    route = respx_mock.get("https://fw1.test/api/routes/gateway/status")
    route.respond(403)
    resp = await client.get(f"/api/hosts/{host['id']}/gateways")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "API_RIGHTS_MISSING"
    route.respond(500)
    assert (await client.get(f"/api/hosts/{host['id']}/gateways")).status_code == 502


# ── Sampler records what was refused ─────────────────────────────────────────

def _mock_optional_reads(respx_mock, *, gateways=200, services=200, swap=200, cpu=200):
    base = "https://fw1.test/api"
    respx_mock.get(f"{base}/diagnostics/system/systemSwap").respond(swap, json={"swap": []})
    respx_mock.get(f"{base}/diagnostics/cpu_usage/getCPUType").respond(cpu, json=["X (4 cores, 4 threads)"])
    respx_mock.get(f"{base}/routes/gateway/status").respond(gateways, json=STATUS_PAYLOAD)
    respx_mock.get(f"{base}/core/service/search").respond(services, json={"rows": []})


async def _sample_raw() -> dict:
    async with SessionLocal() as db:
        await resource_monitor_service.poll_all_hosts(db)
        return (await db.scalars(select(HostResourceSample).order_by(HostResourceSample.sampled_at.desc()))).first().raw


@pytest.mark.asyncio
async def test_sample_records_only_refused_reads_in_denied(client, respx_mock):
    _mock_vip(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)
    _mock_optional_reads(respx_mock, gateways=403, services=403, swap=500, cpu=401)
    raw = await _sample_raw()
    # forbidden -> denied (sorted); a server error and a missing endpoint are "unknown", no finding about rights
    assert raw["denied"] == ["gateways", "services"]
    assert raw["gateways"] is None and raw["services"] is None and raw["swap_used_pct"] is None
    assert raw["ok"] == ["base"]  # only the base reading went through


@pytest.mark.asyncio
async def test_sample_denied_is_an_empty_list_when_nothing_is_refused(client, respx_mock):
    _mock_vip(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)
    _mock_optional_reads(respx_mock)
    raw = await _sample_raw()
    assert raw["denied"] == [] and raw["ok"] == ["base", "cpu", "gateways", "services", "swap"]
    # unreachable optional endpoints (connect error / timeout) are not refusals either
    _mock_optional_reads(respx_mock)
    respx_mock.get("https://fw1.test/api/routes/gateway/status").mock(side_effect=httpx.ReadTimeout("slow"))
    assert (await _sample_raw())["denied"] == []


@pytest.mark.asyncio
async def test_base_read_refusal_becomes_a_finding_instead_of_a_silent_failure(client, respx_mock):
    """Fehlen schon die Basis-Rechte, gab es nie einen Messpunkt und der Rechte-Alarm konnte nicht auslösen."""
    _mock_vip(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)
    respx_mock.get("https://fw1.test/api/diagnostics/system/systemResources").respond(403)
    async with SessionLocal() as db:
        result = await resource_monitor_service.poll_all_hosts(db)
    assert result["hosts_failed"] == 0 and result["sampled"] == 1
    async with SessionLocal() as db:
        sample = (await db.scalars(select(HostResourceSample))).one()
    assert sample.raw == {"denied": ["base"], "ok": []}
    assert sample.mem_used_pct is None and sample.disk_used_pct is None  # "unbekannt", nicht "in Ordnung"


@pytest.mark.asyncio
async def test_base_read_timeout_is_still_a_failed_sample_not_a_rights_finding(client, respx_mock):
    _mock_vip(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)
    respx_mock.get("https://fw1.test/api/diagnostics/system/systemResources").respond(500)
    async with SessionLocal() as db:
        result = await resource_monitor_service.poll_all_hosts(db)
    assert result["hosts_failed"] == 1 and result["sampled"] == 0


@pytest.mark.asyncio
async def test_base_rights_alert_opens_and_closes_again(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    key = f"{hid}:base"
    await _only("api_rights_enabled")
    await _add_samples(hid, [{"denied": ["base"], "ok": []}] * 3)
    problems = await _rights_problems()
    assert list(problems) == [key]
    assert problems[key]["fehlendes_recht"] == "Lobby: Dashboard" and problems[key]["endpunkt"].endswith("systemResources")
    async with SessionLocal() as db:
        await run_notification_checks(db)
    await _clear_samples()
    await _add_samples(hid, [{"denied": [], "ok": ["base"]}] * 3)
    assert dict(await _rights_problems()) == {}


# ── The alert ────────────────────────────────────────────────────────────────

DENIED_GW = {"denied": ["gateways"], "ok": ["cpu", "services", "swap"], "gateways": None, "services": {"unbound": 1}, "cpu_threads": 4}
FINE = {"denied": [], "ok": ["cpu", "gateways", "services", "swap"], "gateways": [_gw()], "services": {"unbound": 1}, "cpu_threads": 4}


@pytest.mark.asyncio
async def test_api_rights_alert_needs_consecutive_samples_and_names_the_missing_right(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_samples(hid, [DENIED_GW, DENIED_GW, FINE])
    assert dict(await _rights_problems()) == {}  # two in a row are not enough (debounce)
    await _clear_samples()
    await _add_samples(hid, [DENIED_GW] * 3)
    problems = await _rights_problems()
    assert list(problems) == [f"{hid}:gateways"]
    ctx = problems[f"{hid}:gateways"]
    assert ctx["host_name"] == "fw1" and ctx["fehlendes_recht"] == "System: Gateways"
    assert ctx["endpunkt"] == "/api/routes/gateway/status" and "fehlt das Recht" in ctx["hinweis"]


@pytest.mark.asyncio
async def test_api_rights_alert_opens_once_resolves_and_is_delivered(client, respx_mock):
    _mock_vip(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _only("api_rights_enabled")
    await _set(webhook_enabled=True, webhook_url="https://hooks.test/n")
    hook = respx_mock.post("https://hooks.test/n").mock(return_value=httpx.Response(200))
    await _add_samples(hid, [DENIED_GW] * 3)
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 1
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 0
        assert await _open_count(db, "api_rights_missing") == 1
    assert hook.call_count == 1
    assert b"API-Rechte fehlen" in hook.calls.last.request.content and b"System: Gateways" in hook.calls.last.request.content
    await _clear_samples()
    await _add_samples(hid, [FINE] * 3)
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["resolved"] == 1
    assert hook.call_count == 2 and b"behoben" in hook.calls.last.request.content


@pytest.mark.asyncio
async def test_api_rights_alert_stays_open_through_unknown_and_flapping_samples(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    key = f"{hid}:gateways"
    await _only("api_rights_enabled")
    await _add_samples(hid, [DENIED_GW] * 3)
    async with SessionLocal() as db:
        await run_notification_checks(db)
    # the gateway read now times out (gateways None, not denied): no proof the right is back
    await _clear_samples()
    await _add_samples(hid, [{"denied": [], "gateways": None}] * 3)
    assert list(await _rights_problems()) == [key]
    # samples from before the denied list existed are unknown too
    await _clear_samples()
    await _add_samples(hid, [{"gateways": [_gw()]}] * 3)
    assert list(await _rights_problems()) == [key]
    # one good sample between refusals does not close it
    await _clear_samples()
    await _add_samples(hid, [DENIED_GW, FINE, DENIED_GW])
    assert list(await _rights_problems()) == [key]


@pytest.mark.asyncio
async def test_api_rights_alert_ignores_stale_probe_and_keeps_open_problem(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _only("api_rights_enabled")
    await _add_samples(hid, [DENIED_GW] * 3)
    async with SessionLocal() as db:
        await run_notification_checks(db)
    await _clear_samples()
    await _add_samples(hid, [FINE] * 3, age_minutes=30)  # the host stopped answering
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["resolved"] == 0
        assert await _open_count(db) == 1
    # a stale refusal does not open anything either
    await _clear_samples()
    async with SessionLocal() as db:
        await db.execute(NotificationProblem.__table__.delete())
        await db.commit()
    await _add_samples(hid, [DENIED_GW] * 3, age_minutes=30)
    assert dict(await _rights_problems()) == {}


@pytest.mark.asyncio
async def test_api_rights_alert_is_on_by_default_and_can_be_switched_off(client):
    data = (await client.get("/api/notifications/settings")).json()
    assert data["api_rights_enabled"] is True
    resp = await client.put("/api/notifications/settings", json={"api_rights_enabled": False})
    assert resp.status_code == 200 and resp.json()["api_rights_enabled"] is False
    host = await _create_host(client, "fw1", "https://fw1.test")
    await _add_samples(uuid.UUID(host["id"]), [DENIED_GW] * 3)
    await _only()  # everything off, including api_rights_enabled
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 0


def test_alert_label_exists():
    assert notification_delivery._LABELS["api_rights_missing"] == "API-Rechte fehlen"


# ── Dashboard and metrics ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dashboard_and_metrics_show_missing_rights(client):
    host = await _create_host(client, "fw1", "https://fw1.test", cluster_name=None)
    hid = uuid.UUID(host["id"])
    await _add_samples(hid, [{"mem": 10, "load": 0.1, **DENIED_GW, "denied": ["gateways", "services"]}])
    summary = (await client.get("/api/dashboard")).json()["hosts"][0]
    assert summary["api_rights_missing"] == ["gateways", "services"] and summary["gateways"] is None
    text = (await client.get("/api/metrics")).text
    assert 'opnexus_host_api_rights_missing{host="fw1",cluster="",read="gateways"} 1' in text
    assert 'opnexus_host_api_rights_missing{host="fw1",cluster="",read="services"} 1' in text

    await _clear_samples()
    await _add_samples(hid, [{"mem": 10, "load": 0.1, **FINE}])
    assert (await client.get("/api/dashboard")).json()["hosts"][0]["api_rights_missing"] == []
    assert 'api_rights_missing{' not in (await client.get("/api/metrics")).text.replace("# HELP", "").replace("# TYPE", "")

    await _clear_samples()
    await _add_samples(hid, [DENIED_GW], age_minutes=20)
    assert (await client.get("/api/dashboard")).json()["hosts"][0]["api_rights_missing"] is None


# ── Mute a single gateway ────────────────────────────────────────────────────

BAD = {"gateways": [_gw("offline", 100.0, None), _gw("offline", 100.0, None, name="LTE_GW")]}


@pytest.mark.asyncio
async def test_muted_gateway_raises_no_alert_but_the_others_do(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = host["id"]
    await _add_samples(uuid.UUID(hid), [BAD] * 3)
    assert sorted(await _gateway_problems()) == sorted([f"{hid}:WAN_GW", f"{hid}:LTE_GW"])
    resp = await client.put(f"/api/hosts/{hid}/gateways/LTE_GW/alert", json={"enabled": False})
    assert resp.status_code == 200 and resp.json() == {"name": "LTE_GW", "alert_enabled": False}
    assert list(await _gateway_problems()) == [f"{hid}:WAN_GW"]
    # a mute is per host: the same name on another host is still watched
    other = await _create_host(client, "fw2", "https://fw2.test")
    await _add_samples(uuid.UUID(other["id"]), [BAD] * 3)
    assert f"{other['id']}:LTE_GW" in await _gateway_problems()
    # switching it on again brings the alert back
    await client.put(f"/api/hosts/{hid}/gateways/LTE_GW/alert", json={"enabled": True})
    assert f"{hid}:LTE_GW" in await _gateway_problems()


@pytest.mark.asyncio
async def test_muting_resolves_an_open_gateway_problem(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = host["id"]
    await _only("gateway_down_enabled")
    await _add_samples(uuid.UUID(hid), [BAD] * 3)
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 2
    await client.put(f"/api/hosts/{hid}/gateways/LTE_GW/alert", json={"enabled": False})
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["resolved"] == 1
        open_keys = (await db.scalars(select(NotificationProblem.dedup_key).where(NotificationProblem.status == "open"))).all()
    assert open_keys == [f"{hid}:WAN_GW"]


@pytest.mark.asyncio
async def test_mute_endpoint_is_idempotent_validates_and_audits(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = host["id"]
    for _ in range(2):
        assert (await client.put(f"/api/hosts/{hid}/gateways/WAN_DHCP6/alert", json={"enabled": False})).status_code == 200
    stored = (await client.get("/api/notifications/settings")).json()["gateway_alert_muted"]
    assert stored == [f"{hid}:WAN_DHCP6"]  # no duplicate

    assert (await client.put(f"/api/hosts/{hid}/gateways/WAN_DHCP6/alert", json={})).status_code == 422
    assert (await client.put(f"/api/hosts/{hid}/gateways/WAN_DHCP6/alert", json={"enabled": "maybe"})).status_code == 422
    for bad in ["x" * 65, "a%0Ab", "a%20b", "%E2%80%AE", "a%3Ab", "a%2Fb"]:
        resp = await client.put(f"/api/hosts/{hid}/gateways/{bad}/alert", json={"enabled": False})
        assert resp.status_code in (404, 422), (bad, resp.status_code)
        assert resp.status_code != 200
    assert (await client.get("/api/notifications/settings")).json()["gateway_alert_muted"] == stored
    missing = await client.put(f"/api/hosts/{uuid.uuid4()}/gateways/WAN_GW/alert", json={"enabled": False})
    assert missing.status_code == 404

    async with SessionLocal() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.action.like("gateway_alert_%")))).all()
    assert [e.action for e in events] == ["gateway_alert_off", "gateway_alert_off"]
    assert events[0].host_id == hid and events[0].message == "Gateway WAN_DHCP6" and events[0].actor


@pytest.mark.asyncio
async def test_mute_list_is_capped(client, monkeypatch):
    from app.routers import gateways
    monkeypatch.setattr(gateways, "MAX_MUTED_GATEWAYS", 3)
    hid = (await _create_host(client, "fw1", "https://fw1.test"))["id"]
    for i in range(3):
        assert (await client.put(f"/api/hosts/{hid}/gateways/GW{i}/alert", json={"enabled": False})).status_code == 200
    resp = await client.put(f"/api/hosts/{hid}/gateways/GW3/alert", json={"enabled": False})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "GATEWAY_MUTE_LIMIT"
    # an already muted one and un-muting still work at the limit
    assert (await client.put(f"/api/hosts/{hid}/gateways/GW0/alert", json={"enabled": False})).status_code == 200
    assert (await client.put(f"/api/hosts/{hid}/gateways/GW0/alert", json={"enabled": True})).status_code == 200


@pytest.mark.asyncio
async def test_only_admins_may_switch_a_gateway_alert(client):
    from tests.test_api_tokens import _login
    from tests.test_operator_role import PW, _operator
    hid = (await _create_host(client, "fw1", "https://fw1.test"))["id"]
    ro = await client.post("/api/users", json={"username": "reader", "password": PW, "role": "read_only"})
    assert ro.status_code == 201
    readers = [await _operator(client, "gw-op"), await _login("reader", PW)]
    try:
        for who in readers:
            resp = await who.put(f"/api/hosts/{hid}/gateways/WAN_GW/alert", json={"enabled": False})
            assert resp.status_code == 403, resp.text
    finally:
        for who in readers:
            await who.aclose()
    assert (await client.get("/api/notifications/settings")).json()["gateway_alert_muted"] == []


@pytest.mark.asyncio
async def test_gateway_list_and_dashboard_carry_the_alert_flag(client, respx_mock):
    _mock_vip(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = host["id"]
    respx_mock.get("https://fw1.test/api/routes/gateway/status").respond(200, json=STATUS_PAYLOAD)
    await client.put(f"/api/hosts/{hid}/gateways/LTE_GW/alert", json={"enabled": False})
    rows = {g["name"]: g for g in (await client.get(f"/api/hosts/{hid}/gateways")).json()}
    assert rows["LTE_GW"]["alert_enabled"] is False and rows["WAN_GW"]["alert_enabled"] is True
    await _add_samples(uuid.UUID(hid), [{"gateways": [_gw(), _gw(name="LTE_GW")]}])
    dash = {g["name"]: g for g in (await client.get("/api/dashboard")).json()["hosts"][0]["gateways"]}
    assert dash["LTE_GW"]["alert_enabled"] is False and dash["WAN_GW"]["alert_enabled"] is True


@pytest.mark.asyncio
async def test_api_rights_endpoint_reads_the_newest_fresh_sample(client):
    hid = (await _create_host(client, "fw1", "https://fw1.test"))["id"]
    url = f"/api/hosts/{hid}/gateways/api-rights"
    assert (await client.get(url)).json() == {"missing": None}  # no sample at all: unknown, not "fine"
    await _add_samples(uuid.UUID(hid), [DENIED_GW, FINE])
    assert (await client.get(url)).json() == {"missing": ["gateways"]}
    await _clear_samples()
    await _add_samples(uuid.UUID(hid), [FINE, DENIED_GW])
    assert (await client.get(url)).json() == {"missing": []}
    await _clear_samples()
    await _add_samples(uuid.UUID(hid), [DENIED_GW], age_minutes=20)
    assert (await client.get(url)).json() == {"missing": None}  # stale
    assert (await client.get(f"/api/hosts/{uuid.uuid4()}/gateways/api-rights")).status_code == 404


@pytest.mark.asyncio
async def test_right_back_closes_the_alert_even_when_the_answer_is_unparsable(client):
    """cpu_threads stays None when the CPU string has no "threads"/"cores": only the `ok` list proves the read went through."""
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _only("api_rights_enabled")
    await _add_samples(hid, [{"denied": ["cpu"], "ok": ["gateways", "services", "swap"], "cpu_threads": None}] * 3)
    assert list(await _rights_problems()) == [f"{hid}:cpu"]
    await _clear_samples()
    await _add_samples(hid, [{"denied": [], "ok": ["cpu", "gateways", "services", "swap"], "cpu_threads": None}] * 3)
    assert list(await _rights_problems()) == []


@pytest.mark.asyncio
async def test_mute_refuses_an_unknown_gateway_name_but_unmute_always_works(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_samples(hid, [FINE])
    assert (await client.put(f"/api/hosts/{hid}/gateways/wan_gw/alert", json={"enabled": False})).status_code == 404
    assert (await client.get("/api/notifications/settings")).json()["gateway_alert_muted"] == []
    assert (await client.put(f"/api/hosts/{hid}/gateways/{_gw()['name']}/alert", json={"enabled": False})).status_code == 200
    assert (await client.put(f"/api/hosts/{hid}/gateways/wan_gw/alert", json={"enabled": True})).status_code == 200


@pytest.mark.asyncio
async def test_deleting_a_host_removes_its_gateway_mutes(client):
    keep = await _create_host(client, "fw-keep", "https://fwkeep.test")
    gone = await _create_host(client, "fw-gone", "https://fwgone.test")
    for h in (keep, gone):
        assert (await client.put(f"/api/hosts/{h['id']}/gateways/WAN_GW/alert", json={"enabled": False})).status_code == 200
    assert (await delete_host(client, gone['id'])).status_code == 204
    assert (await client.get("/api/notifications/settings")).json()["gateway_alert_muted"] == [f"{keep['id']}:WAN_GW"]
