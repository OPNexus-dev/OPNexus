"""Monitoring alerts (1.24): gateway health, resource thresholds, stopped services, extra channels."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.notification import NotificationChannel, NotificationProblem, NotificationSettings
from app.models.resource_sample import HostResourceSample
from app.services import (
    gateway_health_service, monitor_checks, notification_channels, notification_secret_store, resource_monitor_service,
    url_guard,
)
from app.services.notification_delivery import NotificationDeliveryError
from app.services.notification_service import run_notification_checks
from tests.test_notifications import _create_host
from tests.test_resource_monitor import _mock_resource_endpoints, _mock_vip

# What routes/gateway/status returns on a real 26.7 firewall (test-fw-3, live), plus a degraded and a dead gateway.
STATUS_PAYLOAD = {"status": "ok", "items": [
    {"name": "WAN_GW", "address": "203.0.113.1", "status": "none", "loss": "0.0 %", "delay": "1.9 ms", "stddev": "0.3 ms", "monitor": "203.0.113.1"},
    {"name": "LTE_GW", "address": "192.0.2.9", "status": "delay+loss", "loss": "12.5 %", "delay": "310.2 ms", "stddev": "40.0 ms", "monitor": "192.0.2.9"},
    {"name": "DOWN_GW", "address": "192.0.2.1", "status": "down", "loss": "100.0 %", "delay": "~", "stddev": "~", "monitor": "192.0.2.1"},
    {"name": "WAN_DHCP6", "address": "~", "status": "none", "loss": "~", "delay": "~", "stddev": "~", "monitor": "~"},
    {"name": "OFF_GW", "address": "192.0.2.5", "status": "force_down", "loss": "~", "delay": "~", "stddev": "~", "monitor": "~"},
]}


# ── Gateway parsing ──────────────────────────────────────────────────────────

def test_parse_gateway_status_maps_states_and_numbers():
    by_name = {g.name: g for g in gateway_health_service.parse_gateway_status(STATUS_PAYLOAD)}
    assert by_name["WAN_GW"].state == "online" and by_name["WAN_GW"].loss == 0.0 and by_name["WAN_GW"].delay == 1.9
    assert by_name["WAN_GW"].stddev == 0.3
    assert by_name["LTE_GW"].state == "degraded" and by_name["LTE_GW"].loss == 12.5
    assert by_name["DOWN_GW"].state == "offline" and by_name["DOWN_GW"].delay is None
    # nothing measured -> unknown, never "online" and never 0 ms
    assert by_name["WAN_DHCP6"].state == "unknown" and by_name["WAN_DHCP6"].loss is None and by_name["WAN_DHCP6"].address is None
    assert by_name["OFF_GW"].state == "forced_down"


def test_dead_gateway_has_no_round_trip_time():
    # dpinger reports "100.0 %" loss together with "0.0 ms" when nothing answers: 0 ms must not look fast
    payload = {"items": [{"name": "GW", "status": "down", "loss": "100.0 %", "delay": "0.0 ms", "stddev": "0.0 ms", "monitor": "192.0.2.1"}]}
    gw = gateway_health_service.parse_gateway_status(payload)[0]
    assert gw.state == "offline" and gw.loss == 100.0 and gw.delay is None and gw.stddev is None


def test_parse_gateway_status_tolerates_failed_answer():
    assert gateway_health_service.parse_gateway_status({"items": [], "status": "failed"}) == []
    assert gateway_health_service.parse_gateway_status(None) == []


def test_is_bad_thresholds():
    by_name = {g.name: g for g in gateway_health_service.parse_gateway_status(STATUS_PAYLOAD)}
    assert gateway_health_service.is_bad(by_name["DOWN_GW"])
    assert not gateway_health_service.is_bad(by_name["OFF_GW"])  # switched off on purpose
    assert not gateway_health_service.is_bad(by_name["WAN_DHCP6"])
    assert not gateway_health_service.is_bad(by_name["LTE_GW"])  # degraded by OPNsense' limits only, own thresholds off
    assert gateway_health_service.is_bad(by_name["LTE_GW"], loss_threshold_pct=10)
    assert gateway_health_service.is_bad(by_name["LTE_GW"], delay_threshold_ms=300)
    assert not gateway_health_service.is_bad(by_name["WAN_GW"], loss_threshold_pct=10, delay_threshold_ms=300)


@pytest.mark.asyncio
async def test_get_gateway_health_reads_live(client, respx_mock):
    _mock_vip(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    respx_mock.get("https://fw1.test/api/routes/gateway/status").respond(200, json=STATUS_PAYLOAD)
    resp = await client.get(f"/api/hosts/{host['id']}/gateways")
    assert resp.status_code == 200, resp.text
    rows = {g["name"]: g for g in resp.json()}
    assert rows["DOWN_GW"]["state"] == "offline" and rows["WAN_GW"]["delay"] == 1.9
    respx_mock.get("https://fw1.test/api/routes/gateway/status").respond(500)
    assert (await client.get(f"/api/hosts/{host['id']}/gateways")).status_code == 502


# ── Sampler stores the extra readings ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sampler_stores_gateways_services_swap_and_cpu(client, respx_mock):
    _mock_vip(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)
    respx_mock.get("https://fw1.test/api/diagnostics/system/systemSwap").respond(200, json={"swap": [{"device": "/dev/da0p3", "total": "8388608", "used": "2097152"}]})
    respx_mock.get("https://fw1.test/api/diagnostics/cpu_usage/getCPUType").respond(200, json=["QEMU Virtual CPU version 2.5+ (6 cores, 6 threads)"])
    respx_mock.get("https://fw1.test/api/routes/gateway/status").respond(200, json=STATUS_PAYLOAD)
    respx_mock.get("https://fw1.test/api/core/service/search").respond(200, json={"rows": [
        {"id": "unbound", "name": "unbound", "locked": 0, "running": 1},
        {"id": "dhcpd6", "name": "dhcpd6", "locked": 0, "running": 0},
        {"id": "pf", "name": "pf", "locked": 1, "running": 1},
        {"id": "3f2a", "name": "openvpn", "locked": 0, "running": 0},
    ]})
    async with SessionLocal() as db:
        await resource_monitor_service.poll_all_hosts(db)
        raw = (await db.scalars(select(HostResourceSample))).one().raw
    assert raw["swap_used_pct"] == 25.0 and raw["cpu_threads"] == 6
    assert {g["name"]: g["state"] for g in raw["gateways"]}["DOWN_GW"] == "offline"
    assert raw["services"] == {"unbound": 1, "dhcpd6": 0, "openvpn/3f2a": 0}  # locked services are not recorded


@pytest.mark.asyncio
async def test_sampler_survives_missing_optional_endpoints(client, respx_mock):
    _mock_vip(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)  # nothing for swap/cpu/gateways/services -> unmocked routes fail
    async with SessionLocal() as db:
        result = await resource_monitor_service.poll_all_hosts(db)
        row = (await db.scalars(select(HostResourceSample))).one()
    assert result["sampled"] == 1 and row.mem_used_pct == 25.0
    assert row.raw["gateways"] is None and row.raw["services"] is None and row.raw["swap_used_pct"] is None


# ── Debouncing ───────────────────────────────────────────────────────────────

def test_verdict_needs_agreement_and_keeps_state_otherwise():
    v = monitor_checks.verdict
    assert v([True, True, True], 3, False) is True
    assert v([True, True, False], 3, False) is False          # no single spike / short blip
    assert v([True, True], 3, False) is False                  # not enough history yet
    assert v([False, False, False], 3, True) is False         # needs the full window to close
    assert v([False, True, False], 3, True) is True           # flapping keeps an open alert open
    assert v([True, None, True], 3, False) is False           # unreadable sample is no evidence
    assert v([None, None, None], 3, True) is True             # ... and does not clear either
    assert v([True], 1, False) is True


async def _add_samples(host_id, rows: list[dict], *, age_minutes: int = 0) -> None:
    """rows: newest first; each {mem, load, disk, raw...}. One sample per minute, the newest ``age_minutes`` old."""
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        for i, row in enumerate(rows):
            raw = {k: v for k, v in row.items() if k not in ("mem", "load")}
            db.add(HostResourceSample(
                host_id=host_id, sampled_at=now - timedelta(minutes=age_minutes + i),
                mem_used_pct=row.get("mem"), load_avg_1m=row.get("load"), disk_used_pct=None, raw=raw,
            ))
        await db.commit()


def _gw(state="online", loss=0.0, delay=2.0, name="WAN_GW", status=None):
    return {"name": name, "status": status or {"offline": "down", "online": "none"}.get(state, state), "state": state,
            "loss": loss, "delay": delay, "stddev": 0.1}


async def _set(**fields):
    async with SessionLocal() as db:
        row = await db.get(NotificationSettings, 1) or NotificationSettings(id=1)
        for k, v in fields.items():
            setattr(row, k, v)
        db.add(row)
        await db.commit()


_ALL_FLAGS = [
    "host_offline_enabled", "cert_expiring_enabled", "rule_drift_enabled", "revert_failed_enabled", "maintenance_unknown_enabled",
    "carp_health_enabled", "nat_drift_enabled", "network_drift_enabled", "carp_auto_revert_failed_enabled",
    "scheduled_maintenance_failed_enabled", "backup_failed_enabled", "compliance_alerts_enabled",
    "gateway_down_enabled", "resource_threshold_enabled", "service_down_enabled", "api_rights_enabled",
]


async def _only(*enabled: str):
    """Run run_notification_checks with just these checks on (the mocked firewalls are unreachable, which would
    otherwise also open host_offline problems)."""
    await _set(**{flag: flag in enabled for flag in _ALL_FLAGS})


async def _open_count(db, problem_type: str | None = None) -> int:
    query = select(NotificationProblem).where(NotificationProblem.status == "open")
    if problem_type:
        query = query.where(NotificationProblem.problem_type == problem_type)
    return len((await db.scalars(query)).all())


async def _gateway_problems():
    async with SessionLocal() as db:
        return await monitor_checks.check_gateway_down(db)


@pytest.mark.asyncio
async def test_gateway_down_needs_consecutive_samples(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_samples(hid, [{"gateways": [_gw("offline", loss=100.0, delay=None)]}, {"gateways": [_gw("offline", 100.0, None)]}, {"gateways": [_gw()]}])
    assert dict(await _gateway_problems()) == {}  # only two bad samples in a row

    await _add_samples(hid, [{"gateways": [_gw("offline", 100.0, None)]}] * 3, age_minutes=-3)  # three newer bad ones
    problems = await _gateway_problems()
    assert list(problems) == [f"{hid}:WAN_GW"]
    ctx = problems[f"{hid}:WAN_GW"]
    assert ctx["host_name"] == "fw1" and ctx["gateway"] == "WAN_GW" and ctx["grund"] == "Gateway offline"


@pytest.mark.asyncio
async def test_gateway_alert_resolves_only_after_full_recovery_and_ignores_unknown(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    bad, good = {"gateways": [_gw("offline", 100.0, None)]}, {"gateways": [_gw()]}
    await _only("gateway_down_enabled")
    await _add_samples(hid, [bad, bad, bad, good, good])
    async with SessionLocal() as db:
        await run_notification_checks(db)
        assert await _open_count(db) == 1

    # one good sample after the outage, then a relapse: still open (flapping link must not re-notify)
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [bad, good, bad, bad, good])
    assert list(await _gateway_problems()) == [f"{hid}:WAN_GW"]
    # a sample that could not read the gateways does not clear it either
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [{"gateways": None}] * 3)
    assert list(await _gateway_problems()) == [f"{hid}:WAN_GW"]
    # three good ones close it
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [good, good, good])
    assert dict(await _gateway_problems()) == {}


@pytest.mark.asyncio
async def test_gateway_thresholds_and_forced_down(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    lossy = {"gateways": [_gw("degraded", loss=35.0, delay=20.0, status="loss"), _gw("forced_down", None, None, name="OFF", status="force_down")]}
    await _add_samples(hid, [lossy] * 3)
    assert list(await _gateway_problems()) == [f"{hid}:WAN_GW"]  # 35 % >= 20 %; the switched-off gateway is not reported
    await _set(gateway_loss_threshold_pct=0)
    assert dict(await _gateway_problems()) == {}  # own loss threshold off; OPNsense says "degraded", not "down"


@pytest.mark.asyncio
async def test_stale_samples_are_not_judged_and_keep_open_problems(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    bad = {"gateways": [_gw("offline", 100.0, None)]}
    await _only("gateway_down_enabled")
    await _add_samples(hid, [bad] * 3)
    async with SessionLocal() as db:
        await run_notification_checks(db)
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [{"gateways": [_gw()]}] * 3, age_minutes=30)  # the host stopped answering 30 min ago
    async with SessionLocal() as db:
        result = await run_notification_checks(db)
        assert result["resolved"] == 0
        assert await _open_count(db) == 1


@pytest.mark.asyncio
async def test_gateway_alert_is_delivered_once_and_resolution_follows(client, respx_mock):
    _mock_vip(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _only("gateway_down_enabled")
    await _set(webhook_enabled=True, webhook_url="https://hooks.test/n")
    hook = respx_mock.post("https://hooks.test/n").mock(return_value=httpx.Response(200))
    bad, good = {"gateways": [_gw("offline", 100.0, None)]}, {"gateways": [_gw()]}
    await _add_samples(hid, [bad] * 3)
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 1
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 0
    assert hook.call_count == 1 and b"Gateway" in hook.calls.last.request.content
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [good] * 3)
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["resolved"] == 1
    assert hook.call_count == 2 and b"behoben" in hook.calls.last.request.content


@pytest.mark.asyncio
async def test_gateway_check_is_on_by_default_and_the_others_are_off(client):
    data = (await client.get("/api/notifications/settings")).json()
    assert data["gateway_down_enabled"] is True
    assert data["resource_threshold_enabled"] is False and data["service_down_enabled"] is False
    assert data["service_watchlist"][:2] == ["unbound", "dnsmasq"]


# ── Resource thresholds ──────────────────────────────────────────────────────────

DISK_OK = [{"device": "zroot/ROOT/default", "type": "zfs", "used_pct": 40, "mountpoint": "/"}, {"device": "devfs", "type": "devfs", "used_pct": 100, "mountpoint": "/dev"}]
DISK_FULL = [{"device": "zroot/ROOT/default", "type": "zfs", "used_pct": 93, "mountpoint": "/"},
             {"device": "zroot/var/log", "type": "zfs", "used_pct": 91, "mountpoint": "/var/log"},
             {"device": "devfs", "type": "devfs", "used_pct": 100, "mountpoint": "/dev"}]


async def _resource_problems():
    async with SessionLocal() as db:
        return await monitor_checks.check_resource_threshold(db)


@pytest.mark.asyncio
async def test_disk_threshold_one_alert_per_pool_and_ignores_devfs(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_samples(hid, [{"disk": DISK_FULL, "mem": 10, "load": 0.1}] * 5)
    problems = await _resource_problems()
    assert list(problems) == [f"{hid}:disk:zroot"]
    assert problems[f"{hid}:disk:zroot"]["wert_prozent"] == 93 and problems[f"{hid}:disk:zroot"]["schwelle_prozent"] == 90


@pytest.mark.asyncio
async def test_memory_spike_does_not_alert_but_sustained_use_does(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    calm = {"disk": DISK_OK, "mem": 40, "load": 0.1}
    await _add_samples(hid, [{"disk": DISK_OK, "mem": 99, "load": 0.1}, calm, calm, calm, calm])
    assert dict(await _resource_problems()) == {}
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [{"disk": DISK_OK, "mem": 96, "load": 0.1}] * 5)
    assert list(await _resource_problems()) == [f"{hid}:memory"]
    await _set(memory_threshold_pct=0)  # 0 = off
    assert dict(await _resource_problems()) == {}


@pytest.mark.asyncio
async def test_swap_and_cpu_per_thread(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    # load 6 on 2 threads = 3 per thread (>= 2.0); load 6 on 8 threads would be fine
    await _add_samples(hid, [{"disk": DISK_OK, "mem": 10, "load": 6.0, "swap_used_pct": 70.0, "cpu_threads": 2}] * 5)
    assert sorted(await _resource_problems()) == sorted([f"{hid}:cpu", f"{hid}:swap"])
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [{"disk": DISK_OK, "mem": 10, "load": 6.0, "swap_used_pct": None, "cpu_threads": 8}] * 5)
    assert dict(await _resource_problems()) == {}
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    # unknown thread count: the load cannot be judged, so it is not reported
    await _add_samples(hid, [{"disk": DISK_OK, "mem": 10, "load": 50.0, "swap_used_pct": None, "cpu_threads": None}] * 5)
    assert dict(await _resource_problems()) == {}


@pytest.mark.asyncio
async def test_open_resource_alert_without_a_current_reading_shows_no_number(client):
    """An open alert stays open while the value is unreadable (verdict: unreadable is no evidence either way); the
    message must then not say "None %"."""
    from app.services import notification_dedup
    from app.services.notification_delivery import render_message
    from app.models.notification import NotificationProblem
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_samples(hid, [{"disk": DISK_OK, "mem": 10, "load": 0.1, "swap_used_pct": 70.0, "cpu_threads": 2}] * 5)
    async with SessionLocal() as db:
        opened, _ = await notification_dedup.diff_and_update(db, "resource_threshold", await monitor_checks.check_resource_threshold(db))
        await db.commit()
    assert [o.dedup_key for o in opened] == [f"{hid}:swap"] and opened[0].context["wert"] == "70.0 %"
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [{"disk": DISK_OK, "mem": 10, "load": 0.1, "swap_used_pct": None, "cpu_threads": 2}] * 5)
    problems = await _resource_problems()
    context = problems[f"{hid}:swap"]
    assert context["wert"] is None and context["schwelle"].endswith(" %")
    _subject, body = render_message(NotificationProblem(problem_type="resource_threshold", dedup_key="k", context=context), "opened")
    assert "None" not in body and "schwelle:" in body and "wert:" not in body


# ── Services ─────────────────────────────────────────────────────────────────────

async def _service_problems():
    async with SessionLocal() as db:
        return await monitor_checks.check_service_down(db)


@pytest.mark.asyncio
async def test_service_down_only_after_it_was_seen_running(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    up = {"services": {"unbound": 1, "dhcpd6": 0, "cron": 0}}
    down = {"services": {"unbound": 0, "dhcpd6": 0, "cron": 0}}
    await _add_samples(hid, [down, down, down, up, up])
    problems = await _service_problems()
    # unbound stopped; dhcpd6 never ran; cron is not on the watchlist
    assert list(problems) == [f"{hid}:unbound"]
    assert problems[f"{hid}:unbound"]["dienst"] == "unbound"


@pytest.mark.asyncio
async def test_service_that_never_ran_or_stopped_long_ago_is_not_reported(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_samples(hid, [{"services": {"unbound": 0}}] * 6)
    assert dict(await _service_problems()) == {}


@pytest.mark.asyncio
async def test_service_alert_resolves_when_it_runs_again_or_leaves_the_watchlist(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    up, down = {"services": {"unbound": 1}}, {"services": {"unbound": 0}}
    await _only("service_down_enabled")
    await _add_samples(hid, [down, down, down, up])
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 1
    await _set(service_watchlist=["ntpd"])
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["resolved"] == 1
    await _set(service_watchlist=["unbound"])
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 1  # reopens (the row is reused)
    async with SessionLocal() as db:
        await db.execute(HostResourceSample.__table__.delete())
        await db.commit()
    await _add_samples(hid, [up, up, up, down])
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["resolved"] == 1


@pytest.mark.asyncio
async def test_service_watchlist_validation(client):
    ok = await client.put("/api/notifications/settings", json={"service_watchlist": [" unbound ", "openvpn", "unbound"]})
    assert ok.status_code == 200 and ok.json()["service_watchlist"] == ["unbound", "openvpn"]
    assert (await client.put("/api/notifications/settings", json={"service_watchlist": ["bad name!"]})).status_code == 422
    assert (await client.put("/api/notifications/settings", json={"disk_threshold_pct": 101})).status_code == 422
    assert (await client.put("/api/notifications/settings", json={"alert_confirm_samples": 0})).status_code == 422


# ── Metrics and dashboard ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_and_dashboard_show_gateways_and_services(client):
    host = await _create_host(client, "fw1", "https://fw1.test", cluster_name=None)
    hid = uuid.UUID(host["id"])
    await _add_samples(hid, [{"disk": DISK_OK, "mem": 10, "load": 0.1, "swap_used_pct": 5.0,
                              "gateways": [_gw(), _gw("offline", 100.0, None, name="DOWN_GW")],
                              "services": {"unbound": 1, "openvpn/3f2a": 0, "cron": 0}}])
    text = (await client.get("/api/metrics")).text
    assert 'opnexus_host_gateway_up{host="fw1",cluster="",gateway="WAN_GW"} 1' in text
    assert 'opnexus_host_gateway_up{host="fw1",cluster="",gateway="DOWN_GW"} 0' in text
    assert 'opnexus_host_gateway_delay_seconds{host="fw1",cluster="",gateway="WAN_GW"} 0.002' in text
    assert 'opnexus_host_gateway_loss_percent{host="fw1",cluster="",gateway="DOWN_GW"} 100' in text
    assert 'opnexus_host_swap_used_percent{host="fw1",cluster=""} 5' in text
    assert 'opnexus_host_service_running{host="fw1",cluster="",service="unbound"} 1' in text
    assert 'service="openvpn",instance="3f2a"} 0' in text
    assert 'service="cron"' not in text  # not on the watchlist

    summary = (await client.get("/api/dashboard")).json()
    gateways = {g["name"]: g for g in summary["hosts"][0]["gateways"]}
    assert gateways["DOWN_GW"]["state"] == "offline"


@pytest.mark.asyncio
async def test_dashboard_has_no_gateways_without_a_fresh_sample(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    await _add_samples(uuid.UUID(host["id"]), [{"gateways": [_gw()]}], age_minutes=20)
    assert (await client.get("/api/dashboard")).json()["hosts"][0]["gateways"] is None


# ── URL guard ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8000/x", "http://localhost/x", "http://[::1]/x",
    "http://0.0.0.0/x", "http://[::ffff:169.254.169.254]/x", "ftp://ntfy.example/x", "file:///etc/passwd",
    "https://user:pw@ntfy.example/", "https://", "not a url", "http://224.0.0.1/x", "http://[fe80::1]/x",
])
def test_url_guard_refuses(url):
    with pytest.raises(ValueError):
        url_guard.validate_url(url)


@pytest.mark.parametrize("url", ["https://ntfy.sh", "http://10.20.30.5:8080/path", "https://gotify.lan.example/", "http://192.168.1.10"])
def test_url_guard_allows_private_and_public_targets(url):
    assert url_guard.validate_url(url) == url


@pytest.mark.asyncio
async def test_url_guard_refuses_names_resolving_to_metadata_address(monkeypatch):
    async def fake(self, host, port, **kw):
        return [(2, 1, 6, "", ("169.254.169.254", 0))]
    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", fake)
    with pytest.raises(ValueError):
        await url_guard.ensure_resolved_safe("https://innocent.example/hook")


@pytest.mark.asyncio
async def test_webhook_url_is_validated_when_saved_and_sent(client, respx_mock):
    resp = await client.put("/api/notifications/settings", json={"webhook_url": "http://169.254.169.254/x"})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "NOTIFICATION_WEBHOOK_URL_INVALID"
    row = NotificationSettings(webhook_url="http://127.0.0.1:9/x")
    from app.services import notification_delivery
    with pytest.raises(NotificationDeliveryError):
        await notification_delivery.send_webhook(row, NotificationProblem(problem_type="test", dedup_key="t", context={}), "opened")


# ── Channels: formats ────────────────────────────────────────────────────────────

PROBLEM = NotificationProblem(problem_type="gateway_down", dedup_key="k", context={"host_name": "fw1", "gateway": "WAN_GW", "grund": "Gateway offline"})


def _channel(kind: str, config: dict, secret: str | None, name: str = "c1") -> NotificationChannel:
    ch = NotificationChannel(id=uuid.uuid4(), name=name, channel_type=kind, enabled=True, config=config, secret_set=secret is not None)
    if secret is not None:
        notification_secret_store.save_secret(notification_channels.secret_name(ch.id), secret)
    return ch


@pytest.mark.asyncio
async def test_ntfy_posts_json_to_root_with_bearer(respx_mock):
    ch = _channel("ntfy", {"server_url": "https://ntfy.test", "topic": "alerts", "priority": 5}, "tk_secret")
    route = respx_mock.post("https://ntfy.test/").mock(return_value=httpx.Response(200, json={"id": "x"}))
    await notification_channels.send(ch, PROBLEM, "opened")
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tk_secret"
    body = httpx.Response(200, content=req.content).json()
    assert body["topic"] == "alerts" and body["priority"] == 5 and body["tags"] == ["warning"]
    assert body["title"] == "OPNexus: Gateway gestört" and "gateway: WAN_GW" in body["message"]
    await notification_channels.send(ch, PROBLEM, "resolved")
    resolved = httpx.Response(200, content=route.calls.last.request.content).json()
    assert resolved["priority"] == 3 and resolved["title"].endswith("behoben") and resolved["tags"] == ["white_check_mark"]


@pytest.mark.asyncio
async def test_ntfy_without_token_sends_no_auth_header(respx_mock):
    ch = _channel("ntfy", {"server_url": "https://ntfy.test", "topic": "alerts"}, None)
    route = respx_mock.post("https://ntfy.test/").mock(return_value=httpx.Response(200))
    await notification_channels.send(ch, PROBLEM, "opened")
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.asyncio
async def test_gotify_uses_key_header_not_query(respx_mock):
    ch = _channel("gotify", {"server_url": "https://gotify.test/", "priority": 8}, "AppTok123")
    route = respx_mock.post("https://gotify.test/message").mock(return_value=httpx.Response(200, json={"id": 1}))
    await notification_channels.send(ch, PROBLEM, "opened")
    req = route.calls.last.request
    assert req.headers["x-gotify-key"] == "AppTok123" and "AppTok123" not in str(req.url)
    body = httpx.Response(200, content=req.content).json()
    assert body["priority"] == 8 and body["title"] == "OPNexus: Gateway gestört"


@pytest.mark.asyncio
async def test_telegram_sendmessage_and_ok_flag(respx_mock):
    ch = _channel("telegram", {"chat_id": "-1001234", "api_base": "https://api.telegram.test"}, "123456:ABC-token")
    route = respx_mock.post("https://api.telegram.test/bot123456:ABC-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    await notification_channels.send(ch, PROBLEM, "opened")
    body = httpx.Response(200, content=route.calls.last.request.content).json()
    assert body["chat_id"] == "-1001234" and body["text"].startswith("OPNexus: Gateway gestört\n\n") and len(body["text"]) <= 4096
    route.mock(return_value=httpx.Response(200, json={"ok": False, "description": "chat not found"}))
    with pytest.raises(NotificationDeliveryError):
        await notification_channels.send(ch, PROBLEM, "opened")


def test_telegram_text_is_clipped_to_the_api_limit():
    big = NotificationProblem(problem_type="gateway_down", dedup_key="k", context={"x": "y" * 9000})
    text = notification_channels.format_telegram({"chat_id": "1"}, big, "opened")["text"]
    assert len(text) == 4096 and text.endswith("…")


@pytest.mark.asyncio
async def test_teams_adaptive_card_envelope(respx_mock):
    url = "https://prod-12.westeurope.logic.azure.test/workflows/abc/triggers/manual/paths/invoke?sig=SECRETSIG"
    ch = _channel("teams", {}, url)
    route = respx_mock.post(url.split("?")[0]).mock(return_value=httpx.Response(202))
    await notification_channels.send(ch, PROBLEM, "opened")
    body = httpx.Response(200, content=route.calls.last.request.content).json()
    assert body["type"] == "message"
    att = body["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive" and att["content"]["type"] == "AdaptiveCard"
    assert att["content"]["body"][0]["text"] == "OPNexus: Gateway gestört"
    assert "sig=SECRETSIG" in str(route.calls.last.request.url)


@pytest.mark.asyncio
async def test_matrix_put_with_transaction_id_and_encoded_room(respx_mock):
    ch = _channel("matrix", {"homeserver_url": "https://matrix.test", "room_id": "!abc123:matrix.test"}, "syt_token")
    route = respx_mock.put(url__regex=r"^https://matrix\.test/_matrix/client/v3/rooms/%21abc123%3Amatrix\.test/send/m\.room\.message/[0-9a-f]{32}$").mock(
        return_value=httpx.Response(200, json={"event_id": "$e"}))
    await notification_channels.send(ch, PROBLEM, "opened")
    await notification_channels.send(ch, PROBLEM, "opened")
    first, second = (c.request for c in route.calls)
    assert first.headers["authorization"] == "Bearer syt_token"
    assert first.url.path != second.url.path  # a new transaction id per message
    body = httpx.Response(200, content=first.content).json()
    assert body["msgtype"] == "m.text" and body["body"].startswith("OPNexus: Gateway gestört")


@pytest.mark.asyncio
async def test_failures_and_logs_never_contain_the_credential(respx_mock, caplog):
    token = "123456:SECRET-BOT-TOKEN"
    ch = _channel("telegram", {"chat_id": "42", "api_base": "https://api.telegram.test"}, token)
    respx_mock.post(f"https://api.telegram.test/bot{token}/sendMessage").mock(return_value=httpx.Response(401, json={"ok": False}))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(NotificationDeliveryError) as exc_info:
            await notification_channels.send(ch, PROBLEM, "opened")
        respx_mock.post(f"https://api.telegram.test/bot{token}/sendMessage").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(NotificationDeliveryError) as exc_info2:
            await notification_channels.send(ch, PROBLEM, "opened")
    assert "HTTP 401" in str(exc_info.value) and "ConnectError" in str(exc_info2.value)
    for text in (str(exc_info.value), str(exc_info2.value), caplog.text):
        assert "SECRET-BOT-TOKEN" not in text
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__


@pytest.mark.asyncio
async def test_missing_or_unreadable_secret_fails_closed(respx_mock):
    ch = _channel("gotify", {"server_url": "https://gotify.test"}, "tok")
    notification_secret_store.delete_secret(notification_channels.secret_name(ch.id))
    with pytest.raises(NotificationDeliveryError, match="nicht lesbar"):
        await notification_channels.send(ch, PROBLEM, "opened")
    ch.secret_set = False
    with pytest.raises(NotificationDeliveryError, match="fehlen"):
        await notification_channels.send(ch, PROBLEM, "opened")


@pytest.mark.asyncio
async def test_redirects_are_not_followed(respx_mock):
    ch = _channel("gotify", {"server_url": "https://gotify.test"}, "tok")
    respx_mock.post("https://gotify.test/message").mock(return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
    with pytest.raises(NotificationDeliveryError, match="HTTP 302"):
        await notification_channels.send(ch, PROBLEM, "opened")


# ── Channels: API ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_channel_crud_keeps_the_secret_out_of_database_and_responses(client, respx_mock):
    resp = await client.post("/api/notifications/channels", json={
        "name": "Telegram Ops", "channel_type": "telegram", "config": {"chat_id": "-100123"}, "secret": "111:TOKEN-VALUE",
    })
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["secret_set"] is True and "TOKEN-VALUE" not in resp.text
    assert created["config"] == {"chat_id": "-100123", "api_base": "https://api.telegram.org"}
    cid = created["id"]
    assert notification_secret_store.read_secret(f"channel_{cid}") == "111:TOKEN-VALUE"
    async with SessionLocal() as db:
        row = await db.get(NotificationChannel, uuid.UUID(cid))
        assert "TOKEN-VALUE" not in str(row.config) and "TOKEN-VALUE" not in repr(row.__dict__)
    assert "TOKEN-VALUE" not in (await client.get("/api/notifications/channels")).text

    # test button
    route = respx_mock.post("https://api.telegram.org/bot111:TOKEN-VALUE/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert (await client.post(f"/api/notifications/channels/{cid}/test")).json() == {"success": True}
    assert b"Testbenachrichtigung" in route.calls.last.request.content
    route.mock(return_value=httpx.Response(500))
    failed = await client.post(f"/api/notifications/channels/{cid}/test")
    assert failed.status_code == 502 and failed.json()["detail"]["code"] == "NOTIFICATION_CHANNEL_TEST_FAILED"
    assert "TOKEN-VALUE" not in failed.text

    # update: rename, disable, rotate secret, clear secret
    upd = await client.put(f"/api/notifications/channels/{cid}", json={"name": "Telegram", "enabled": False, "secret": "222:NEW"})
    assert upd.status_code == 200 and upd.json()["enabled"] is False
    assert notification_secret_store.read_secret(f"channel_{cid}") == "222:NEW"
    assert (await client.put(f"/api/notifications/channels/{cid}", json={"clear_secret": True})).json()["secret_set"] is False
    assert notification_secret_store.read_secret(f"channel_{cid}") is None

    assert (await client.delete(f"/api/notifications/channels/{cid}")).status_code == 204
    assert (await client.get("/api/notifications/channels")).json() == []
    assert (await client.delete(f"/api/notifications/channels/{cid}")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"name": "a", "channel_type": "ntfy", "config": {"topic": "bad topic!"}},
    {"name": "a", "channel_type": "ntfy", "config": {"topic": "ok", "server_url": "http://169.254.169.254"}},
    {"name": "a", "channel_type": "ntfy", "config": {"topic": "ok", "priority": 9}},
    {"name": "a", "channel_type": "gotify", "config": {"server_url": "https://g.test"}},  # token missing
    {"name": "a", "channel_type": "gotify", "config": {}, "secret": "t"},                  # server missing
    {"name": "a", "channel_type": "telegram", "config": {"chat_id": "abc"}, "secret": "t"},
    {"name": "a", "channel_type": "teams", "config": {}, "secret": "http://insecure.test/hook"},
    {"name": "a", "channel_type": "teams", "config": {}},
    {"name": "a", "channel_type": "matrix", "config": {"homeserver_url": "https://m.test", "room_id": "#alias:m.test"}, "secret": "t"},
    {"name": "a", "channel_type": "carrier-pigeon", "config": {}},
    {"name": "   ", "channel_type": "ntfy", "config": {"topic": "ok"}},  # name only blanks
])
async def test_channel_validation(client, payload):
    resp = await client.post("/api/notifications/channels", json=payload)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_channel_names_are_unique_and_ntfy_needs_no_secret(client):
    body = {"name": "Phone", "channel_type": "ntfy", "config": {"topic": "opnexus-alerts"}}
    first = await client.post("/api/notifications/channels", json=body)
    assert first.status_code == 201 and first.json()["secret_set"] is False
    assert first.json()["config"]["server_url"] == "https://ntfy.sh" and first.json()["config"]["priority"] == 4
    dup = await client.post("/api/notifications/channels", json=body)
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "NOTIFICATION_CHANNEL_NAME_TAKEN"


@pytest.mark.asyncio
async def test_operator_cannot_change_channels_and_does_not_see_their_settings(client):
    from tests.test_operator_role import PW, _operator
    created = await client.post("/api/notifications/channels", json={"name": "Phone", "channel_type": "ntfy", "config": {"topic": "secret-topic"}})
    op = await _operator(client, "chan-op")
    try:
        for method, path, body in [
            ("POST", "/api/notifications/channels", {"name": "x", "channel_type": "ntfy", "config": {"topic": "t"}}),
            ("PUT", f"/api/notifications/channels/{created.json()['id']}", {"enabled": False}),
            ("DELETE", f"/api/notifications/channels/{created.json()['id']}", None),
            ("POST", f"/api/notifications/channels/{created.json()['id']}/test", {}),
        ]:
            resp = await op.request(method, path, json=body) if body is not None else await op.request(method, path)
            assert resp.status_code == 403, (method, path, resp.text)
        listing = await op.get("/api/notifications/channels")
        assert listing.status_code == 200 and listing.json()[0]["config"] == {} and "secret-topic" not in listing.text
    finally:
        await op.aclose()


@pytest.mark.asyncio
async def test_alerts_reach_every_enabled_channel_and_one_failure_does_not_block_the_rest(client, respx_mock):
    _mock_vip(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    async with SessionLocal() as db:
        for kind, cfg, secret, name in [
            ("gotify", {"server_url": "https://gotify.test"}, "g-tok", "A gotify"),
            ("ntfy", {"server_url": "https://ntfy.test", "topic": "t"}, None, "B ntfy"),
        ]:
            ch = _channel(kind, cfg, secret, name)
            db.add(ch)
        off = _channel("ntfy", {"server_url": "https://off.test", "topic": "t"}, None, "C disabled")
        off.enabled = False
        db.add(off)
        await db.commit()
    await _only("gateway_down_enabled")
    gotify = respx_mock.post("https://gotify.test/message").mock(return_value=httpx.Response(500))
    ntfy = respx_mock.post("https://ntfy.test/").mock(return_value=httpx.Response(200))
    await _add_samples(hid, [{"gateways": [_gw("offline", 100.0, None)]}] * 3)
    async with SessionLocal() as db:
        assert (await run_notification_checks(db))["opened"] == 1
    assert gotify.call_count == 1 and ntfy.call_count == 1
    assert "off.test" not in str([c.request.url for c in respx_mock.calls])
