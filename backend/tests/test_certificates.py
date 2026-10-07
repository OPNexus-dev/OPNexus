import httpx
import pytest

from httpx import AsyncClient


async def _create_host(client: AsyncClient, name: str, base_url: str) -> dict:
    resp = await client.post(
        "/api/hosts",
        json={"name": name, "base_url": base_url, "api_key": "key", "api_secret": "secret", "verify_tls": False},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _cert_payload(host_id: str, **overrides) -> dict:
    payload = {
        "host_id": host_id,
        "action": "import",
        "description": "Imported web cert",
        "commonname": "",
        "country": "DE",
        "state": "",
        "city": "",
        "organization": "",
        "email": "",
        "key_type": "2048",
        "digest": "sha256",
        "cert_type": "server_cert",
        "lifetime": 397,
        "caref": None,
        "csr_payload": None,
        "crt_payload": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_managed_certificate(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/certificates/managed", json=_cert_payload(host["id"], private_key="FAKE-PRIVATE-KEY"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["private_key_set"] is True
    assert "private_key" not in body


@pytest.mark.asyncio
async def test_import_requires_crt_payload(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/certificates/managed", json=_cert_payload(host["id"], crt_payload=None))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_action_requires_commonname(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/certificates/managed", json=_cert_payload(host["id"], action="internal", crt_payload=None, commonname="")
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_action_with_commonname_accepted(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/certificates/managed",
        json=_cert_payload(host["id"], action="internal", crt_payload=None, commonname="fw1.example.com"),
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_private_key_only_allowed_for_import_action(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post(
        "/api/certificates/managed", json=_cert_payload(host["id"], action="manual", private_key="not-allowed-here")
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_push_managed_certificate(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/certificates/managed", json=_cert_payload(host["id"]))
    cert_id = resp.json()["id"]

    respx_mock.post(url__regex=r".*/trust/cert/addAction$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-uuid-1"})
    )
    resp = await client.post(f"/api/certificates/managed/{cert_id}/push")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["opnsense_uuid"] == "cert-uuid-1"


@pytest.mark.asyncio
async def test_push_is_noop_when_nothing_changed(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/certificates/managed", json=_cert_payload(host["id"]))
    cert_id = resp.json()["id"]

    add_route = respx_mock.post(url__regex=r".*/trust/cert/addAction$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-uuid-1"})
    )
    resp1 = await client.post(f"/api/certificates/managed/{cert_id}/push")
    assert resp1.json()["status"] == "success"
    resp2 = await client.post(f"/api/certificates/managed/{cert_id}/push")
    assert resp2.json()["status"] == "success"
    # Second push touched nothing on the firewall — no-op re-push, same
    # rule ipsec_deploy_service.py/openvpn_deploy_service.py apply.
    assert add_route.call_count == 1


@pytest.mark.asyncio
async def test_delete_before_ever_pushed_skips_firewall_call(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/certificates/managed", json=_cert_payload(host["id"]))
    cert_id = resp.json()["id"]

    resp = await client.delete(f"/api/certificates/managed/{cert_id}")
    assert resp.status_code == 204

    resp = await client.get("/api/certificates/managed")
    assert all(c["id"] != cert_id for c in resp.json())


@pytest.mark.asyncio
async def test_delete_in_use_certificate_returns_stable_error_code(client, respx_mock):
    """OPNsense itself blocks deleting an in-use cert (checkAndThrowValueInUse
    inside CertController::delAction()) — this app does not pre-check that,
    it must pass the firewall's own error text straight through."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/certificates/managed", json=_cert_payload(host["id"]))
    cert_id = resp.json()["id"]

    respx_mock.post(url__regex=r".*/trust/cert/addAction$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-uuid-1"})
    )
    await client.post(f"/api/certificates/managed/{cert_id}/push")

    respx_mock.post(url__regex=r".*/trust/cert/delAction/.*").mock(
        return_value=httpx.Response(500, json={"message": "OpenVPN - roadwarrior {openvpn.instance.abc}"})
    )
    resp = await client.delete(f"/api/certificates/managed/{cert_id}")
    assert resp.status_code == 502
    assert resp.json()["detail"] == {"code": "UPSTREAM_REQUEST_FAILED", "params": {}}
