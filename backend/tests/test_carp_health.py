import uuid

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.carp_event import CarpEvent
from app.services import carp_event_service, notification_checks


async def _create_host(client, name: str, base_url: str, cluster_name: str) -> dict:
    payload = {
        "name": name, "base_url": base_url, "api_key": "key", "api_secret": "secret",
        "verify_tls": False, "cluster_name": cluster_name,
    }
    resp = await client.post("/api/hosts", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _vip(status: str | None) -> dict:
    rows = [{"vhid": "1", "status": status}] if status else []
    return {"rows": rows, "carp": {"allow": "1", "maintenancemode": "0", "demotion": "0"}}


# ── check_carp_health ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_carp_health_detects_split_brain(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("MASTER")))
    respx_mock.get("https://fw-b.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("MASTER")))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    async with SessionLocal() as db:
        problems = await notification_checks.check_carp_health(db)

    assert len(problems) == 1
    row = next(iter(problems.values()))
    assert row["condition"] == "split_brain"
    assert row["host_names"] == ["fw-a", "fw-b"]


@pytest.mark.asyncio
async def test_check_carp_health_detects_no_master(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("BACKUP")))
    respx_mock.get("https://fw-b.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("BACKUP")))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    async with SessionLocal() as db:
        problems = await notification_checks.check_carp_health(db)

    assert len(problems) == 1
    assert next(iter(problems.values()))["condition"] == "no_master"


@pytest.mark.asyncio
async def test_check_carp_health_healthy_reports_nothing(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("MASTER")))
    respx_mock.get("https://fw-b.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("BACKUP")))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    async with SessionLocal() as db:
        problems = await notification_checks.check_carp_health(db)

    assert problems == {}


def _vips(**states: str) -> dict:
    return {"rows": [{"vhid": v, "status": st} for v, st in states.items()],
            "carp": {"allow": "1", "maintenancemode": "0", "demotion": "0"}}


@pytest.mark.asyncio
async def test_check_carp_health_detects_a_vip_that_is_down_on_one_node(client, respx_mock):
    # 2026-10-20: VHID 130 was DISABLED on the primary and taken over by the secondary; per host it looked fine.
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json=_vips(**{"110": "MASTER", "130": "DISABLED"})))
    respx_mock.get("https://fw-b.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json=_vips(**{"110": "BACKUP", "130": "BACKUP"})))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    async with SessionLocal() as db:
        problems = await notification_checks.check_carp_health(db)

    assert {p["condition"] for p in problems.values()} == {"vip_down"}
    assert next(iter(problems.values()))["vhids"] == ["130"]


@pytest.mark.asyncio
async def test_check_carp_health_detects_a_partial_failover(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json=_vips(**{"110": "MASTER", "130": "BACKUP"})))
    respx_mock.get("https://fw-b.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json=_vips(**{"110": "BACKUP", "130": "BACKUP"})))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    async with SessionLocal() as db:
        problems = await notification_checks.check_carp_health(db)

    assert [p["condition"] for p in problems.values()] == ["role_mismatch"]


@pytest.mark.asyncio
async def test_check_carp_health_ignores_single_reachable_host(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("MASTER")))
    respx_mock.get("https://fw-b.test/api/diagnostics/interface/getVipStatus").mock(side_effect=httpx.ConnectError("down"))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    async with SessionLocal() as db:
        problems = await notification_checks.check_carp_health(db)

    assert problems == {}


# ── carp_event_service.detect_and_record ─────────────────────────────────

@pytest.mark.asyncio
async def test_detect_and_record_creates_event_on_first_observation(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("MASTER")))
    host = await _create_host(client, "fw-a", "https://fw-a.test", "ha")

    async with SessionLocal() as db:
        created = await carp_event_service.detect_and_record(db)

    assert len(created) == 1
    assert created[0].host_id == uuid.UUID(host["id"])
    assert created[0].from_status is None
    assert created[0].to_status == "MASTER"


@pytest.mark.asyncio
async def test_detect_and_record_only_fires_on_transition(client, respx_mock):
    route = respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus")
    route.mock(return_value=httpx.Response(200, json=_vip("MASTER")))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")

    async with SessionLocal() as db:
        first = await carp_event_service.detect_and_record(db)
        assert len(first) == 1
        second = await carp_event_service.detect_and_record(db)
        assert second == []

    route.mock(return_value=httpx.Response(200, json=_vip("BACKUP")))
    async with SessionLocal() as db:
        third = await carp_event_service.detect_and_record(db)
        assert len(third) == 1
        assert third[0].from_status == "MASTER"
        assert third[0].to_status == "BACKUP"

        all_events = (await db.execute(select(CarpEvent))).scalars().all()
        assert len(all_events) == 2


@pytest.mark.asyncio
async def test_detect_and_record_skips_unreachable_hosts(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(side_effect=httpx.ConnectError("down"))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")

    async with SessionLocal() as db:
        created = await carp_event_service.detect_and_record(db)

    assert created == []


# ── API ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_carp_events_endpoint_filters_by_cluster(client, respx_mock):
    respx_mock.get("https://fw-a.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("MASTER")))
    respx_mock.get("https://fw-c.test/api/diagnostics/interface/getVipStatus").mock(return_value=httpx.Response(200, json=_vip("BACKUP")))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-c", "https://fw-c.test", "other")

    async with SessionLocal() as db:
        await carp_event_service.detect_and_record(db)

    resp = await client.get("/api/cluster/carp-events?cluster_name=ha")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["host_name"] == "fw-a"

    resp_all = await client.get("/api/cluster/carp-events")
    assert len(resp_all.json()) == 2
