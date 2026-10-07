import httpx
import pytest

from httpx import AsyncClient
from tests.helpers import deploy_approved


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
        "server_name": "fw1-wg0",
        "server_public_key": "s" * 43 + "=",
        "server_private_key": "p" * 43 + "=",
        "listen_port": "51820",
        "server_tunnel_address": "10.9.0.1/24",
        "dns": "",
        "mtu": "",
        "peer_name": "fw2-peer",
        "peer_public_key": "c" * 43 + "=",
        "peer_psk_enabled": False,
        "peer_allowed_ips": "10.9.0.2/32",
        "peer_endpoint_address": "",
        "peer_endpoint_port": "",
        "peer_keepalive": "",
        "enabled": True,
        "target_host_ids": target_host_ids,
        "target_group_ids": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_tunnel(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["server_name"] == "fw1-wg0"
    assert body["target_host_ids"] == [host["id"]]
    assert body["server_private_key_set"] is True
    assert body["peer_psk_set"] is False
    assert "server_private_key" not in body
    assert "peer_psk_material" not in body
    assert body["deployments"] == []
    assert body["workflow_state"] == "draft"


@pytest.mark.asyncio
async def test_invalid_name_rejected(client):
    payload = _tunnel_payload([], server_name="bad name with spaces")
    resp = await client.post("/api/wireguard/tunnels", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_address_requires_port(client):
    payload = _tunnel_payload([], peer_endpoint_address="peer.example.com", peer_endpoint_port="")
    resp = await client.post("/api/wireguard/tunnels", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_address_with_port_accepted(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    payload = _tunnel_payload([], peer_endpoint_address="peer.example.com", peer_endpoint_port="51820")
    resp = await client.post("/api/wireguard/tunnels", json=payload)
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_preview_shows_rollback_warning_and_create_action(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    resp = await client.get(f"/api/wireguard/tunnels/{tunnel_id}/preview")
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["hosts"][0]["action"] == "create"
    assert plan["can_deploy"] is True
    assert any("kein automatischer rollback" in w.lower() for w in plan["warnings"])


@pytest.mark.asyncio
async def test_deploy_without_targets_rejected(client):
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([]))
    tunnel_id = resp.json()["id"]

    resp = await deploy_approved(client, f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_deploy_blocked_when_psk_enabled_without_material(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]], peer_psk_enabled=True))
    tunnel_id = resp.json()["id"]

    resp = await deploy_approved(client, f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_tunnel(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    resp = await client.delete(f"/api/wireguard/tunnels/{tunnel_id}")
    assert resp.status_code == 204

    resp = await client.get("/api/wireguard/tunnels")
    assert resp.status_code == 200
    assert all(t["id"] != tunnel_id for t in resp.json())


@pytest.mark.asyncio
async def test_edit_can_leave_secrets_unchanged(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/wireguard/tunnels",
        json=_tunnel_payload([host["id"]], peer_psk_enabled=True, peer_psk_material="q" * 43 + "="),
    )
    tunnel = resp.json()
    assert tunnel["server_private_key_set"] is True
    assert tunnel["peer_psk_set"] is True

    edit_payload = _tunnel_payload([host["id"]], server_name="fw1-wg0-renamed", peer_psk_enabled=True)
    edit_payload["expected_updated_at"] = tunnel["updated_at"]
    edit_payload.pop("server_private_key")
    resp = await client.put(f"/api/wireguard/tunnels/{tunnel['id']}", json=edit_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["server_name"] == "fw1-wg0-renamed"
    assert body["server_private_key_set"] is True
    assert body["peer_psk_set"] is True
