import httpx
import pytest


@pytest.mark.asyncio
async def test_status_mixed_online_offline(client, respx_mock):
    respx_mock.get("https://fw-up.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": [{"interface": "wan", "vhid": "1", "status": "BACKUP"}]})
    )
    respx_mock.get("https://fw-down.test/api/diagnostics/interface/getVipStatus").mock(
        side_effect=httpx.ConnectError("no route to host")
    )

    resp_up = await client.post(
        "/api/hosts",
        json={"name": "fw-up", "base_url": "https://fw-up.test", "api_key": "k", "api_secret": "s"},
    )
    assert resp_up.status_code == 201, resp_up.text

    resp_down = await client.post(
        "/api/hosts",
        json={"name": "fw-down", "base_url": "https://fw-down.test", "api_key": "k", "api_secret": "s"},
    )
    assert resp_down.status_code == 201, resp_down.text
    assert resp_down.json()["status"] == "offline"

    resp = await client.get("/api/hosts")
    assert resp.status_code == 200
    by_name = {h["name"]: h for h in resp.json()}
    assert by_name["fw-up"]["status"] == "online"
    assert by_name["fw-up"]["carp_status"] == "BACKUP"
    assert by_name["fw-down"]["status"] == "offline"
    assert by_name["fw-down"]["status_detail"]
