import httpx
import pytest
from tests.test_rules import _create_host
from tests.test_users import _login_as


def _mock_vip(respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))


@pytest.mark.asyncio
async def test_list_services(client, respx_mock):
    _mock_vip(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    respx_mock.get("https://fw1.test/api/core/service/search").respond(
        200, json={"rows": [{"id": "unbound", "name": "unbound", "description": "Unbound DNS", "locked": 0, "running": 1}]}
    )
    r = await client.get(f"/api/hosts/{h['id']}/services")
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["id"] == "unbound"


@pytest.mark.asyncio
async def test_restart_service_success(client, respx_mock):
    _mock_vip(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    restart = respx_mock.post("https://fw1.test/api/core/service/restart/unbound").respond(200, json={"result": "ok"})
    r = await client.post(f"/api/hosts/{h['id']}/services/unbound/restart")
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "ok"
    assert restart.call_count == 1


@pytest.mark.asyncio
async def test_start_and_stop_service(client, respx_mock):
    _mock_vip(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    start = respx_mock.post("https://fw1.test/api/core/service/start/ntpd").respond(200, json={"result": "ok"})
    stop = respx_mock.post("https://fw1.test/api/core/service/stop/ntpd").respond(200, json={"result": "ok"})
    r1 = await client.post(f"/api/hosts/{h['id']}/services/ntpd/start")
    r2 = await client.post(f"/api/hosts/{h['id']}/services/ntpd/stop")
    assert r1.status_code == 200 and r2.status_code == 200
    assert start.call_count == 1 and stop.call_count == 1


@pytest.mark.asyncio
async def test_restart_service_failure_result_is_rejected(client, respx_mock):
    _mock_vip(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    respx_mock.post("https://fw1.test/api/core/service/restart/unbound").respond(200, json={"result": "failed"})
    r = await client.post(f"/api/hosts/{h['id']}/services/unbound/restart")
    assert r.status_code == 502, r.text


@pytest.mark.asyncio
async def test_read_only_user_can_list_but_not_restart(client, respx_mock):
    _mock_vip(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    respx_mock.get("https://fw1.test/api/core/service/search").respond(200, json={"rows": []})
    await client.post("/api/users", json={"username": "viewer-svc", "password": "password-123456", "role": "read_only"})
    viewer = await _login_as("viewer-svc", "password-123456")
    try:
        listed = await viewer.get(f"/api/hosts/{h['id']}/services")
        assert listed.status_code == 200, listed.text
        blocked = await viewer.post(f"/api/hosts/{h['id']}/services/unbound/restart")
        assert blocked.status_code == 403
    finally:
        await viewer.aclose()
