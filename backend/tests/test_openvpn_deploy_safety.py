"""Covers OpenVpnInstance's deliberately simpler status model (draft/
success/error only — see openvpn_deploy_service.py's module docstring) —
same rationale as test_ipsec_deploy_safety.py for IpsecTunnel, adapted to
this entity's 2-object shape (optional StaticKey, then the Instance) and
to OpenVPN's OWN reconfigure response shape ({"result": "ok"}, NOT
IPsec's {"status": "ok"}), which this suite checks explicitly rather than
assuming symmetry."""
import json

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.openvpn_instance_deployment import OpenVpnInstanceDeployment
from tests.test_openvpn import _create_host, _instance_payload


def _mock_full_success(respx_mock, base_url: str, with_statickey: bool = False):
    if with_statickey:
        respx_mock.post(f"{base_url}/api/openvpn/instances/addStaticKey").mock(
            return_value=httpx.Response(200, json={"result": "saved", "uuid": "statickey-1"})
        )
    respx_mock.post(f"{base_url}/api/openvpn/instances/add").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "instance-1"})
    )
    respx_mock.post(f"{base_url}/api/openvpn/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )


@pytest.mark.asyncio
async def test_deploy_success_persists_instance_uuid(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]]))
    instance_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")

    resp = await client.post(f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert dep["message"] is None

    async with SessionLocal() as db:
        row = (await db.execute(select(OpenVpnInstanceDeployment))).scalar_one()
        assert row.opnsense_instance_uuid == "instance-1"
        assert row.opnsense_statickey_uuid is None
        assert row.last_payload is not None


@pytest.mark.asyncio
async def test_deploy_with_tls_key_writes_statickey_before_instance(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/openvpn/instances",
        json=_instance_payload([host["id"]], tls_key_enabled=True, tls_key_material="0123456789abcdef" * 8),
    )
    instance_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test", with_statickey=True)

    resp = await client.post(f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"

    async with SessionLocal() as db:
        row = (await db.execute(select(OpenVpnInstanceDeployment))).scalar_one()
        assert row.opnsense_statickey_uuid == "statickey-1"
        assert row.opnsense_instance_uuid == "instance-1"


@pytest.mark.asyncio
async def test_deploy_config_write_failure_surfaces_as_error_without_rollback(client, respx_mock):
    """The Instance add fails — status must be "error" with a concrete
    message, and reconfigure must never be reached (config write itself
    failed, no rollback exists for this entity)."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]]))
    instance_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/openvpn/instances/add").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    reconfigure_route = respx_mock.post("https://fw1.test/api/openvpn/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )

    resp = await client.post(f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"]
    assert not reconfigure_route.called

    async with SessionLocal() as db:
        row = (await db.execute(select(OpenVpnInstanceDeployment))).scalar_one()
        assert row.opnsense_instance_uuid is None


@pytest.mark.asyncio
async def test_deploy_reconfigure_failure_uses_exact_specified_message(client, respx_mock):
    """The Instance write succeeds, but the follow-up reconfigure call
    fails — the plan specifies the exact same non-generic message IPsec
    uses ("config written, reload unconfirmed" is a materially different
    failure mode from "config write itself failed")."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]]))
    instance_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/openvpn/instances/add").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "instance-1"})
    )
    respx_mock.post("https://fw1.test/api/openvpn/service/reconfigure").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await client.post(f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"] == "Konfiguration wurde geschrieben, Dienst-Reload aber nicht bestätigt — Erreichbarkeit manuell prüfen"

    async with SessionLocal() as db:
        row = (await db.execute(select(OpenVpnInstanceDeployment))).scalar_one()
        assert row.opnsense_instance_uuid == "instance-1"
        assert row.last_payload is not None


@pytest.mark.asyncio
async def test_reconfigure_checks_result_key_explicitly(client, respx_mock):
    """Regression guard for the specific "don't assume symmetry with
    IPsec" finding: OpenVPN's reconfigureAction reports failure via
    {"result": "failed"} (the "result"-based branch of
    opnsense_client._mutation_result, since reconfigure_openvpn() is
    dispatched with results={"ok"}) — NOT via a "status" key at all. A
    mock returning {"result": "failed"} must be treated as an unconfirmed
    reconfigure with the same exact message as a transport-level failure,
    proving this app checks the actual key/value OpenVPN's reconfigure
    reports through, not IPsec's "status" shape."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]]))
    instance_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/openvpn/instances/add").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "instance-1"})
    )
    respx_mock.post("https://fw1.test/api/openvpn/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "failed"})
    )

    resp = await client.post(f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"] == "Konfiguration wurde geschrieben, Dienst-Reload aber nicht bestätigt — Erreichbarkeit manuell prüfen"


@pytest.mark.asyncio
async def test_redeploy_unchanged_skips_opnsense_entirely(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/openvpn/instances", json=_instance_payload([host["id"]]))
    instance_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")
    await client.post(f"/api/openvpn/instances/{instance_id}/deploy")

    add_route = respx_mock.post("https://fw1.test/api/openvpn/instances/add")

    resp = await client.post(f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert add_route.call_count == 1


@pytest.mark.asyncio
async def test_disabling_tls_key_after_enable_deletes_statickey(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/openvpn/instances",
        json=_instance_payload([host["id"]], tls_key_enabled=True, tls_key_material="0123456789abcdef" * 8),
    )
    instance = resp.json()

    _mock_full_success(respx_mock, "https://fw1.test", with_statickey=True)
    await client.post(f"/api/openvpn/instances/{instance['id']}/deploy")

    del_statickey = respx_mock.post("https://fw1.test/api/openvpn/instances/delStaticKey/statickey-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )
    respx_mock.post("https://fw1.test/api/openvpn/instances/set/instance-1").mock(
        return_value=httpx.Response(200, json={"result": "saved"})
    )
    respx_mock.post("https://fw1.test/api/openvpn/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )

    edit_payload = _instance_payload([host["id"]], tls_key_enabled=False)
    edit_payload["expected_updated_at"] = instance["updated_at"]
    resp = await client.put(f"/api/openvpn/instances/{instance['id']}", json=edit_payload)
    assert resp.status_code == 200, resp.text

    resp = await client.post(f"/api/openvpn/instances/{instance['id']}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert del_statickey.called

    async with SessionLocal() as db:
        row = (await db.execute(select(OpenVpnInstanceDeployment))).scalar_one()
        assert row.opnsense_statickey_uuid is None


@pytest.mark.asyncio
async def test_multi_host_deploy_sends_each_host_its_own_cert_and_ca(client, respx_mock):
    """Regression guard for the 2026-09-09 fix: cert_refs/ca_refs are
    per-host maps now, so a two-host instance must push a DIFFERENT
    cert/ca pair to each host, not the same string to both."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host1 = await _create_host(client, "fw1", "https://fw1.test")
    host2 = await _create_host(client, "fw2", "https://fw2.test")
    resp = await client.post(
        "/api/openvpn/instances",
        json=_instance_payload(
            [host1["id"], host2["id"]],
            cert_refs={host1["id"]: "cert-host1", host2["id"]: "cert-host2"},
            ca_refs={host1["id"]: "ca-host1", host2["id"]: "ca-host2"},
        ),
    )
    instance_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")
    respx_mock.post("https://fw2.test/api/openvpn/instances/add").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "instance-2"})
    )
    respx_mock.post("https://fw2.test/api/openvpn/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )

    resp = await client.post(f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 200, resp.text
    for dep in resp.json()["deployments"]:
        assert dep["status"] == "success"

    sent1 = [c for c in respx_mock.calls if str(c.request.url) == "https://fw1.test/api/openvpn/instances/add"][-1]
    sent2 = [c for c in respx_mock.calls if str(c.request.url) == "https://fw2.test/api/openvpn/instances/add"][-1]
    body1 = json.loads(sent1.request.content)["instance"]
    body2 = json.loads(sent2.request.content)["instance"]
    assert body1["cert"] == "cert-host1" and body1["ca"] == "ca-host1"
    assert body2["cert"] == "cert-host2" and body2["ca"] == "ca-host2"


@pytest.mark.asyncio
async def test_host_missing_cert_selection_fails_closed_without_any_call(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/openvpn/instances",
        json=_instance_payload([host["id"]], cert_refs={}, ca_refs={}),
    )
    instance_id = resp.json()["id"]

    add_route = respx_mock.post("https://fw1.test/api/openvpn/instances/add").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "instance-1"})
    )
    reconfigure_route = respx_mock.post("https://fw1.test/api/openvpn/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )

    resp = await client.post(f"/api/openvpn/instances/{instance_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert "Kein Zertifikat/CA" in dep["message"]
    assert not add_route.called
    assert not reconfigure_route.called


@pytest.mark.asyncio
async def test_undeploy_deletes_instance_before_statickey(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/openvpn/instances",
        json=_instance_payload([host["id"]], tls_key_enabled=True, tls_key_material="0123456789abcdef" * 8),
    )
    instance_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test", with_statickey=True)
    await client.post(f"/api/openvpn/instances/{instance_id}/deploy")

    del_instance = respx_mock.post("https://fw1.test/api/openvpn/instances/del/instance-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )
    del_statickey = respx_mock.post("https://fw1.test/api/openvpn/instances/delStaticKey/statickey-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )

    resp = await client.delete(f"/api/openvpn/instances/{instance_id}")
    assert resp.status_code == 204, resp.text
    assert del_instance.called and del_statickey.called
