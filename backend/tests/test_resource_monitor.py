import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.resource_sample import HostResourceSample
from app.services import resource_monitor_service
from tests.test_rules import _create_host


def _mock_vip(respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))


def _mock_resource_endpoints(respx_mock, base="https://fw1.test"):
    respx_mock.get(f"{base}/api/diagnostics/system/systemResources").respond(
        200, json={"memory": {"total": "1000000000", "used": 250000000}}
    )
    respx_mock.get(f"{base}/api/diagnostics/system/systemTime").respond(
        200, json={"uptime": "1 day", "loadavg": "0.42, 0.30, 0.20"}
    )
    respx_mock.get(f"{base}/api/diagnostics/system/systemDisk").respond(
        200, json={"devices": [{"mountpoint": "/", "used_pct": 12}, {"mountpoint": "/boot", "used_pct": 5}]}
    )
    respx_mock.get(f"{base}/api/diagnostics/interface/getInterfaceStatistics").respond(
        200, json={"statistics": {"[igb0]": {"name": "igb0", "received-bytes": 100, "sent-bytes": 200}}}
    )


@pytest.mark.asyncio
async def test_poll_all_hosts_stores_parsed_sample(client, respx_mock):
    _mock_vip(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)

    async with SessionLocal() as db:
        result = await resource_monitor_service.poll_all_hosts(db)
        assert result == {"hosts_polled": 1, "hosts_failed": 0, "sampled": 1}
        rows = (await db.scalars(select(HostResourceSample))).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.mem_used_pct == 25.0
        assert row.load_avg_1m == 0.42
        assert row.disk_used_pct == 12.0
        assert row.raw["interfaces"]["[igb0]"]["received-bytes"] == 100


@pytest.mark.asyncio
async def test_poll_all_hosts_isolates_per_host_failure(client, respx_mock):
    _mock_vip(respx_mock)
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    _mock_resource_endpoints(respx_mock, "https://fw1.test")
    respx_mock.get("https://fw2.test/api/diagnostics/system/systemResources").respond(500)

    async with SessionLocal() as db:
        result = await resource_monitor_service.poll_all_hosts(db)
        assert result == {"hosts_polled": 2, "hosts_failed": 1, "sampled": 1}


@pytest.mark.asyncio
async def test_list_samples_endpoint(client, respx_mock):
    _mock_vip(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)

    async with SessionLocal() as db:
        await resource_monitor_service.poll_all_hosts(db)

    r = await client.get(f"/api/hosts/{h['id']}/resource-samples")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["mem_used_pct"] == 25.0


@pytest.mark.asyncio
async def test_prune_old_samples(client, respx_mock):
    _mock_vip(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    _mock_resource_endpoints(respx_mock)

    async with SessionLocal() as db:
        await resource_monitor_service.poll_all_hosts(db)
        deleted = await resource_monitor_service.prune_old_samples(db, retention_days=30)
        assert deleted == 0
        deleted = await resource_monitor_service.prune_old_samples(db, retention_days=0)
        assert deleted == 1
