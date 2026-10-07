import httpx
import pytest

from httpx import AsyncClient

from tests.test_rules import _create_host


def _pf_payload(target_host_ids: list[str], **overrides) -> dict:
    payload = {
        "description": "Web to internal server",
        "interface": "wan",
        "protocol": "tcp",
        "source_net": "any",
        "destination_net": "wan_address",
        "destination_port": "443",
        "target": "10.0.1.20",
        "local_port": "8443",
        "target_host_ids": target_host_ids,
    }
    payload.update(overrides)
    return payload


def _mock_pf_push(respx_mock, base_url: str, uuid_: str = "pf-uuid-1"):
    respx_mock.post(f"{base_url}/api/firewall/d_nat/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": uuid_})
    )
    respx_mock.post(f"{base_url}/api/firewall/d_nat/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))


@pytest.mark.asyncio
async def test_create_port_forward_rule(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/port-forward", json=_pf_payload([host["id"]]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target"] == "10.0.1.20"
    assert body["local_port"] == "8443"
    assert body["target_host_ids"] == [host["id"]]
    assert body["deployments"] == []
    assert body["workflow_state"] == "draft"


@pytest.mark.asyncio
async def test_deploy_port_forward_success_and_confirm(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/port-forward", json=_pf_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_pf_push(respx_mock, "https://fw1.test")

    resp = await client.post(f"/api/port-forward/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "pending"
    assert dep["revert_deadline"] is not None

    confirmed = await client.post(f"/api/port-forward/{rule_id}/confirm")
    assert confirmed.json()["deployments"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_deploy_port_forward_payload_shape_is_nested_and_uses_disabled(client: AsyncClient, respx_mock):
    """Pins the two easy-to-miss wire-format gotchas verified against
    OPNsense's DNat model: nested source/destination objects, and a
    "disabled" field (inverse of our own "enabled") instead of "enabled"."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/port-forward", json=_pf_payload([host["id"]], source_port="1234"))
    rule_id = resp.json()["id"]

    add_route = respx_mock.post("https://fw1.test/api/firewall/d_nat/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "pf-uuid-1"})
    )
    respx_mock.post("https://fw1.test/api/firewall/d_nat/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))

    await client.post(f"/api/port-forward/{rule_id}/deploy")

    import json as _json
    sent = _json.loads(add_route.calls.last.request.content)["rule"]
    assert sent["disabled"] == "0"
    assert "enabled" not in sent
    assert sent["source"] == {"network": "any", "port": "1234", "not": "0"}
    assert sent["destination"] == {"network": "wan_address", "port": "443", "not": "0"}
    assert sent["target"] == "10.0.1.20"
    assert sent["local-port"] == "8443"


@pytest.mark.asyncio
async def test_deploy_port_forward_mixed_host_failure(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/firewall/filter/getRule").mock(return_value=httpx.Response(200, json={"rule": {"interface": {"wan": {"value": "WAN"}}}}))
    respx_mock.get(url__regex=r".*/firewall/alias/searchItem").mock(return_value=httpx.Response(200, json={"rows": []}))
    host_ok = await _create_host(client, "fw-ok", "https://fw-ok.test")
    host_down = await _create_host(client, "fw-down", "https://fw-down.test")

    resp = await client.post("/api/port-forward", json=_pf_payload([host_ok["id"], host_down["id"]]))
    rule_id = resp.json()["id"]

    _mock_pf_push(respx_mock, "https://fw-ok.test")
    respx_mock.post("https://fw-down.test/api/firewall/d_nat/addRule").mock(side_effect=httpx.ConnectError("connection refused"))

    resp = await client.post(f"/api/port-forward/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    deployments = {d["host_id"]: d for d in resp.json()["deployments"]}
    assert deployments[host_ok["id"]]["status"] == "pending"
    assert deployments[host_down["id"]]["status"] == "error"
    assert deployments[host_down["id"]]["message"]


@pytest.mark.asyncio
async def test_port_forward_edit_creates_revision_history(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/port-forward", json=_pf_payload([host["id"]]))).json()

    updated = await client.put(
        f"/api/port-forward/{r['id']}",
        json=_pf_payload([host["id"]], target="10.0.1.99", expected_updated_at=r["updated_at"]),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["target"] == "10.0.1.99"

    history = (await client.get(f"/api/port-forward/{r['id']}/revisions")).json()
    assert len(history) == 2
    assert history[1]["snapshot"]["target"] == "10.0.1.20"


@pytest.mark.asyncio
async def test_stale_port_forward_editor_rejected(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/port-forward", json=_pf_payload([host["id"]]))).json()
    stale = await client.put(f"/api/port-forward/{r['id']}", json=_pf_payload([host["id"]], expected_updated_at=r["updated_at"]))
    assert stale.status_code == 200
    conflict = await client.put(f"/api/port-forward/{r['id']}", json=_pf_payload([host["id"]], expected_updated_at=r["updated_at"]))
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_delete_port_forward_rule(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/port-forward", json=_pf_payload([host["id"]]))).json()

    resp = await client.delete(f"/api/port-forward/{r['id']}")
    assert resp.status_code == 204
    assert (await client.get("/api/port-forward")).json() == []


@pytest.mark.asyncio
async def test_deploy_port_forward_without_targets_rejected(client: AsyncClient):
    r = (await client.post("/api/port-forward", json=_pf_payload([]))).json()
    resp = await client.post(f"/api/port-forward/{r['id']}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_port_forward_stale_preview_token_rejected(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/port-forward", json=_pf_payload([host["id"]]))).json()

    resp = await client.post(f"/api/port-forward/{r['id']}/deploy", json={"preview_token": "0" * 64})
    assert resp.status_code == 409
