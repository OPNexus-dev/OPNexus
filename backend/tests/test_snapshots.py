"""ZFS snapshots: list, create, delete (never the active one)."""
import httpx
import pytest

BASE = "https://10.20.30.80"


async def _host(client, respx_mock, name="fw-snap"):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    return (await client.post("/api/hosts", json={"name": name, "base_url": BASE, "api_key": "k", "api_secret": "s"})).json()["id"]


def _snaps(respx_mock, rows):
    respx_mock.get(f"{BASE}/api/core/snapshots/isSupported").mock(return_value=httpx.Response(200, json={"supported": True}))
    return respx_mock.get(f"{BASE}/api/core/snapshots/search").mock(return_value=httpx.Response(200, json={"rows": rows}))


ROW = {"uuid": "s1", "name": "default", "active": "NR", "mountpoint": "/", "size": "1.2G", "created_str": "2026-10-06 13:59"}


@pytest.mark.asyncio
async def test_list_snapshots(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _snaps(respx_mock, [ROW])
    body = (await client.get(f"/api/hosts/{host_id}/snapshots")).json()
    assert body["supported"] is True and body["rows"][0]["name"] == "default" and body["rows"][0]["active"] == "NR"


@pytest.mark.asyncio
async def test_unsupported_firewall_is_reported(client, respx_mock):
    host_id = await _host(client, respx_mock, "fw-nozfs")
    respx_mock.get(f"{BASE}/api/core/snapshots/isSupported").mock(return_value=httpx.Response(200, json={"supported": False}))
    assert (await client.get(f"/api/hosts/{host_id}/snapshots")).json() == {"supported": False, "rows": []}


@pytest.mark.asyncio
async def test_create_is_confirmed_by_listing_again(client, respx_mock):
    host_id = await _host(client, respx_mock, "fw-snap2")
    new = {"uuid": "s2", "name": "before_change", "active": "-", "mountpoint": "-", "size": "0", "created_str": "now"}
    respx_mock.get(f"{BASE}/api/core/snapshots/search").mock(return_value=httpx.Response(200, json={"rows": [ROW, new]}))
    post = respx_mock.post(f"{BASE}/api/core/snapshots/add").mock(return_value=httpx.Response(200, json="OK"))
    resp = await client.post(f"/api/hosts/{host_id}/snapshots", json={"name": "before_change", "note": "x"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["uuid"] == "s2" and post.called


@pytest.mark.asyncio
async def test_create_rejects_bad_names_and_reports_a_snapshot_that_never_appeared(client, respx_mock):
    host_id = await _host(client, respx_mock, "fw-snap3")
    assert (await client.post(f"/api/hosts/{host_id}/snapshots", json={"name": "bad name!"})).status_code == 422
    respx_mock.get(f"{BASE}/api/core/snapshots/search").mock(return_value=httpx.Response(200, json={"rows": [ROW]}))
    respx_mock.post(f"{BASE}/api/core/snapshots/add").mock(return_value=httpx.Response(200, json="OK"))
    assert (await client.post(f"/api/hosts/{host_id}/snapshots", json={"name": "ghost"})).status_code == 502


@pytest.mark.asyncio
async def test_active_snapshot_cannot_be_deleted_others_can(client, respx_mock):
    host_id = await _host(client, respx_mock, "fw-snap4")
    old = {"uuid": "s3", "name": "old", "active": "-", "mountpoint": "-", "size": "1M", "created_str": "x"}
    state = {"rows": [ROW, old]}
    respx_mock.get(f"{BASE}/api/core/snapshots/search").mock(side_effect=lambda r: httpx.Response(200, json=state))
    def drop(request):
        state["rows"] = [ROW]
        return httpx.Response(200, json={"status": "ok"})
    delete = respx_mock.post(f"{BASE}/api/core/snapshots/del/s3").mock(side_effect=drop)
    blocked = await client.delete(f"/api/hosts/{host_id}/snapshots/s1")
    assert blocked.status_code == 409 and blocked.json()["detail"]["params"]["reasons"] == "snapshot_active"
    assert (await client.delete(f"/api/hosts/{host_id}/snapshots/s3")).status_code == 204 and delete.called
    assert (await client.delete(f"/api/hosts/{host_id}/snapshots/nope")).status_code == 404
