import httpx
import pytest

from httpx import AsyncClient


async def _create_host(client: AsyncClient, name: str, base_url: str) -> dict:
    resp = await client.post(
        "/api/hosts",
        json={
            "name": name,
            "base_url": base_url,
            "api_key": "key",
            "api_secret": "secret",
            "verify_tls": False,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _tunnel_payload(target_host_ids: list[str], **overrides) -> dict:
    payload = {
        "description": "S2S to branch office",
        "local_address": "203.0.113.1",
        "remote_address": "198.51.100.1",
        "ike_version": "ikev2",
        "proposal": "default",
        "local_identity": "203.0.113.1",
        "remote_identity": "198.51.100.1",
        "local_networks": "10.10.0.0/24",
        "remote_networks": "10.20.0.0/24",
        "esp_proposal": "default",
        "enabled": True,
        "target_host_ids": target_host_ids,
        "target_group_ids": [],
        "pre_shared_key": "correct-horse-battery-staple",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_tunnel(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["description"] == "S2S to branch office"
    assert body["target_host_ids"] == [host["id"]]
    assert body["pre_shared_key_set"] is True
    assert "pre_shared_key" not in body
    assert body["deployments"] == []
    assert body["workflow_state"] == "draft"


@pytest.mark.asyncio
async def test_create_tunnel_requires_pre_shared_key(client):
    payload = _tunnel_payload([])
    del payload["pre_shared_key"]
    resp = await client.post("/api/ipsec/tunnels", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_preview_shows_rollback_warning_and_create_action(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    resp = await client.get(f"/api/ipsec/tunnels/{tunnel_id}/preview")
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["hosts"][0]["action"] == "create"
    assert plan["can_deploy"] is True
    assert any("KEIN automatischer Rollback".lower() in w.lower() or "kein automatischer rollback" in w.lower() for w in plan["warnings"])


@pytest.mark.asyncio
async def test_deploy_without_targets_rejected(client):
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([]))
    tunnel_id = resp.json()["id"]

    resp = await client.post(f"/api/ipsec/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_tunnel(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    resp = await client.delete(f"/api/ipsec/tunnels/{tunnel_id}")
    assert resp.status_code == 204

    resp = await client.get("/api/ipsec/tunnels")
    assert resp.status_code == 200
    assert all(t["id"] != tunnel_id for t in resp.json())


@pytest.mark.asyncio
async def test_edit_can_leave_pre_shared_key_unchanged(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel = resp.json()

    edit_payload = _tunnel_payload([host["id"]], description="Renamed tunnel")
    del edit_payload["pre_shared_key"]
    edit_payload["expected_updated_at"] = tunnel["updated_at"]
    resp = await client.put(f"/api/ipsec/tunnels/{tunnel['id']}", json=edit_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "Renamed tunnel"
    assert body["pre_shared_key_set"] is True
