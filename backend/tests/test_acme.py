import json

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


def _account_payload(host_id: str, **overrides) -> dict:
    payload = {
        "host_id": host_id,
        "name": "Primary LE account",
        "description": "",
        "email": "admin@example.com",
        "ca": "letsencrypt_test",
        "custom_ca": "",
        "eab_kid": "",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _cert_request_payload(host_id: str, account_id: str, **overrides) -> dict:
    payload = {
        "host_id": host_id,
        "name": "www.example.com",
        "alt_names": "example.com,mail.example.com",
        "account_id": account_id,
        "validation_ref": "11111111-1111-1111-1111-111111111111",
        "key_length": "key_4096",
        "ocsp": False,
        "auto_renewal": True,
        "renew_interval": 60,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


# ── Accounts ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_acme_account(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["eab_hmac_set"] is False
    assert "eab_hmac" not in body


@pytest.mark.asyncio
async def test_custom_ca_requires_url(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"], ca="custom"))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_push_acme_account_with_eab(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]), )
    account_id = resp.json()["id"]

    add_route = respx_mock.post(url__regex=r".*/acmeclient/accounts/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-1"})
    )
    resp = await client.post(f"/api/acme/accounts/{account_id}/push")
    assert resp.status_code == 200, resp.text
    assert resp.json()["opnsense_uuid"] == "acct-uuid-1"
    sent = json.loads(add_route.calls.last.request.content)
    # eab_hmac defaults to "" when never set — never a placeholder/None,
    # since it's a plain TextField on the wire.
    assert sent["account"]["eab_hmac"] == ""


@pytest.mark.asyncio
async def test_register_account_regression_response_shape(client, respx_mock):
    """Locks in the plan's verified {"result":"failed"}/{"response":"..."}
    ACME action-trigger shape — a THIRD distinct shape after IPsec's
    {"status":"ok"} (ipsec/service/reconfigure) and OpenVPN/WireGuard's
    {"result":"ok"} (openvpn|wireguard/service/reconfigure)."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    account_id = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/accounts/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-1"})
    )
    await client.post(f"/api/acme/accounts/{account_id}/push")

    respx_mock.post(url__regex=r".*/acmeclient/accounts/register/.*").mock(
        return_value=httpx.Response(200, json={"response": "Registered! Account ID: https://acme-v02.api.letsencrypt.org/acme/acct/12345"})
    )
    resp = await client.post(f"/api/acme/accounts/{account_id}/register")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["register_status"] == "ok"
    assert "Registered" in body["register_message"]


@pytest.mark.asyncio
async def test_register_account_failed_precheck_shape(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    account_id = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/accounts/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-1"})
    )
    await client.post(f"/api/acme/accounts/{account_id}/push")

    respx_mock.post(url__regex=r".*/acmeclient/accounts/register/.*").mock(return_value=httpx.Response(200, json={"result": "failed"}))
    resp = await client.post(f"/api/acme/accounts/{account_id}/register")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_register_requires_push_first(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    account_id = resp.json()["id"]

    resp = await client.post(f"/api/acme/accounts/{account_id}/register")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_account_blocked_while_referenced(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    account_id = resp.json()["id"]
    resp = await client.post("/api/acme/certificates", json=_cert_request_payload(host["id"], account_id))
    assert resp.status_code == 201, resp.text

    resp = await client.delete(f"/api/acme/accounts/{account_id}")
    assert resp.status_code == 409


# ── Certificate requests ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_certificate_request_requires_matching_account_host(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host1 = await _create_host(client, "fw1", "https://fw1.test")
    host2 = await _create_host(client, "fw2", "https://fw2.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host1["id"]))
    account_id = resp.json()["id"]

    resp = await client.post("/api/acme/certificates", json=_cert_request_payload(host2["id"], account_id))
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_push_certificate_request_injects_account_uuid(client, respx_mock):
    """UUID-injection correctness — mirrors ipsec_deploy_service.py's
    `connection` UUID pattern: the account's OPNsense-side uuid must
    appear on the wire even though it's never part of the OPNexus-level
    payload the user edits."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    account_id = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/accounts/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-1"})
    )
    await client.post(f"/api/acme/accounts/{account_id}/push")

    resp = await client.post("/api/acme/certificates", json=_cert_request_payload(host["id"], account_id))
    request_id = resp.json()["id"]

    add_route = respx_mock.post(url__regex=r".*/acmeclient/certificates/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-req-uuid-1"})
    )
    resp = await client.post(f"/api/acme/certificates/{request_id}/push")
    assert resp.status_code == 200, resp.text
    sent = json.loads(add_route.calls.last.request.content)
    assert sent["certificate"]["account"] == "acct-uuid-1"
    assert sent["certificate"]["validationMethod"] == "11111111-1111-1111-1111-111111111111"
    assert sent["certificate"]["keyLength"] == "key_4096"
    assert sent["certificate"]["altNames"] == "example.com,mail.example.com"
    assert "_account_id" not in sent["certificate"]


@pytest.mark.asyncio
async def test_push_certificate_request_is_noop_when_unchanged(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    account_id = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/accounts/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-1"})
    )
    await client.post(f"/api/acme/accounts/{account_id}/push")

    resp = await client.post("/api/acme/certificates", json=_cert_request_payload(host["id"], account_id))
    request_id = resp.json()["id"]
    add_route = respx_mock.post(url__regex=r".*/acmeclient/certificates/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-req-uuid-1"})
    )
    resp1 = await client.post(f"/api/acme/certificates/{request_id}/push")
    assert resp1.json()["status"] == "success"
    resp2 = await client.post(f"/api/acme/certificates/{request_id}/push")
    assert resp2.json()["status"] == "success"
    assert add_route.call_count == 1


@pytest.mark.asyncio
async def test_push_certificate_request_repushes_after_account_switch(client, respx_mock):
    """Regression for the exact no-op-redeploy pitfall the module docstring
    warns about: switching account_id alone (every other visible field
    unchanged) must still trigger a real re-push with the NEW account's
    uuid on the wire."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"], name="Account A"))
    account_a = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/accounts/add$").mock(
        side_effect=[
            httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-a"}),
            httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-b"}),
        ]
    )
    await client.post(f"/api/acme/accounts/{account_a}/push")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"], name="Account B"))
    account_b = resp.json()["id"]
    await client.post(f"/api/acme/accounts/{account_b}/push")

    resp = await client.post("/api/acme/certificates", json=_cert_request_payload(host["id"], account_a))
    request = resp.json()
    add_route = respx_mock.post(url__regex=r".*/acmeclient/certificates/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-req-uuid-1"})
    )
    push_resp = await client.post(f"/api/acme/certificates/{request['id']}/push")
    assert json.loads(add_route.calls.last.request.content)["certificate"]["account"] == "acct-uuid-a"

    edit_payload = _cert_request_payload(host["id"], account_b)
    # updated_at moves on the push above too (onupdate fires on any row
    # UPDATE, not just user-edited fields) — use the post-push value, not
    # the one captured right after create.
    edit_payload["expected_updated_at"] = push_resp.json()["updated_at"]
    resp = await client.put(f"/api/acme/certificates/{request['id']}", json=edit_payload)
    assert resp.status_code == 200, resp.text

    set_route = respx_mock.post(url__regex=r".*/acmeclient/certificates/update/.*").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-req-uuid-1"})
    )
    resp = await client.post(f"/api/acme/certificates/{request['id']}/push")
    assert resp.status_code == 200, resp.text
    assert set_route.call_count == 1
    assert json.loads(set_route.calls.last.request.content)["certificate"]["account"] == "acct-uuid-b"


@pytest.mark.asyncio
async def test_sign_is_async_trigger_not_a_success_claim(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    account_id = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/accounts/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-1"})
    )
    await client.post(f"/api/acme/accounts/{account_id}/push")

    resp = await client.post("/api/acme/certificates", json=_cert_request_payload(host["id"], account_id))
    request_id = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/certificates/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-req-uuid-1"})
    )
    await client.post(f"/api/acme/certificates/{request_id}/push")

    respx_mock.post(url__regex=r".*/acmeclient/certificates/sign/.*").mock(
        return_value=httpx.Response(200, json={"response": "Certificate issuance for domain 'www.example.com' started."})
    )
    resp = await client.post(f"/api/acme/certificates/{request_id}/sign")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Deliberately "triggered", never "success" — the trigger returning
    # is proof-of-acceptance only, see acme_service.py's module docstring.
    assert body["sign_status"] == "triggered"
    assert body["sign_status"] != "success"


@pytest.mark.asyncio
async def test_poll_certificate_status_updates_remote_fields(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/acme/accounts", json=_account_payload(host["id"]))
    account_id = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/accounts/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "acct-uuid-1"})
    )
    await client.post(f"/api/acme/accounts/{account_id}/push")

    resp = await client.post("/api/acme/certificates", json=_cert_request_payload(host["id"], account_id))
    request_id = resp.json()["id"]
    respx_mock.post(url__regex=r".*/acmeclient/certificates/add$").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "cert-req-uuid-1"})
    )
    await client.post(f"/api/acme/certificates/{request_id}/push")

    respx_mock.get(url__regex=r".*/acmeclient/certificates/get/.*").mock(
        return_value=httpx.Response(
            200,
            json={"certificate": {"statusCode": "200", "statusLastUpdate": "1700000000", "lastUpdate": "1699990000"}},
        )
    )
    resp = await client.post(f"/api/acme/certificates/{request_id}/poll")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["remote_status_code"] == 200
    assert body["remote_status_last_update"] is not None
    assert body["remote_last_update"] is not None
