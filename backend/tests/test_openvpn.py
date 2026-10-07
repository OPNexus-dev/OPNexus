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


def _instance_payload(target_host_ids: list[str], **overrides) -> dict:
    payload = {
        "description": "Road-warrior server",
        "role": "server",
        "proto": "udp",
        "port": "1194",
        "local": "",
        "remote": "",
        "server_network": "10.8.0.0/24",
        # Per-host maps — default to one shared cert/CA string per target
        # host so most tests don't need to think about it; tests that care
        # about multi-host resolution or the missing-selection path override
        # cert_refs/ca_refs explicitly.
        "cert_refs": {hid: "cert-uuid-1" for hid in target_host_ids},
        "ca_refs": {hid: "ca-uuid-1" for hid in target_host_ids},
        "tls_key_enabled": False,
        "tls_key_mode": "crypt",
        "enabled": True,
        "target_host_ids": target_host_ids,
        "target_group_ids": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_instance(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["description"] == "Road-warrior server"
    assert body["target_host_ids"] == [host["id"]]
    assert body["tls_key_material_set"] is False
    assert "tls_key_material" not in body
    assert body["deployments"] == []
    assert body["workflow_state"] == "draft"


@pytest.mark.asyncio
async def test_server_role_requires_server_network(client):
    payload = _instance_payload([], server_network="")
    resp = await client.post("/api/openvpn/instances", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_client_role_requires_remote(client):
    payload = _instance_payload([], role="client", server_network="", remote="")
    resp = await client.post("/api/openvpn/instances", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_client_role_with_remote_accepted(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    payload = _instance_payload([], role="client", server_network="", remote="vpn.example.com:1194")
    resp = await client.post("/api/openvpn/instances", json=payload)
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_preview_shows_rollback_warning_and_create_action(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]]))
    instance_id = resp.json()["id"]

    resp = await client.get(f"/api/openvpn/instances/{instance_id}/preview")
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["hosts"][0]["action"] == "create"
    assert plan["can_deploy"] is True
    assert any("kein automatischer rollback" in w.lower() for w in plan["warnings"])


@pytest.mark.asyncio
async def test_preview_flags_host_missing_cert_selection(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/openvpn/instances",
        json=_instance_payload([host["id"]], cert_refs={}, ca_refs={}),
    )
    instance_id = resp.json()["id"]

    resp = await client.get(f"/api/openvpn/instances/{instance_id}/preview")
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["hosts"][0]["action"] == "missing_cert"
    assert any("zertifikat" in w.lower() for w in plan["warnings"])


@pytest.mark.asyncio
async def test_deploy_without_targets_rejected(client):
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([]))
    instance_id = resp.json()["id"]

    resp = await deploy_approved(client, f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_deploy_blocked_when_tls_key_enabled_without_material(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]], tls_key_enabled=True))
    instance_id = resp.json()["id"]

    resp = await deploy_approved(client, f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_instance(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]]))
    instance_id = resp.json()["id"]

    resp = await client.delete(f"/api/openvpn/instances/{instance_id}")
    assert resp.status_code == 204

    resp = await client.get("/api/openvpn/instances")
    assert resp.status_code == 200
    assert all(i["id"] != instance_id for i in resp.json())


@pytest.mark.asyncio
async def test_edit_can_leave_tls_key_material_unchanged(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/openvpn/instances",
        json=_instance_payload([host["id"]], tls_key_enabled=True, tls_key_material="0123456789abcdef" * 8),
    )
    instance = resp.json()
    assert instance["tls_key_material_set"] is True

    edit_payload = _instance_payload([host["id"]], description="Renamed instance", tls_key_enabled=True)
    edit_payload["expected_updated_at"] = instance["updated_at"]
    resp = await client.put(f"/api/openvpn/instances/{instance['id']}", json=edit_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "Renamed instance"
    assert body["tls_key_material_set"] is True
