import httpx
import pytest

from httpx import AsyncClient

from tests.test_rules import _create_host


def _nat_payload(target_host_ids: list[str], **overrides) -> dict:
    payload = {
        "description": "Web to internal server",
        "interface": "wan",
        "source_net": "any",
        "destination_net": "wan_address",
        "external": "10.0.1.20",
        "target_host_ids": target_host_ids,
    }
    payload.update(overrides)
    return payload


def _mock_nat_push(respx_mock, base_url: str, uuid_: str = "pf-uuid-1"):
    respx_mock.post(f"{base_url}/api/firewall/one_to_one/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": uuid_})
    )
    respx_mock.post(f"{base_url}/api/firewall/one_to_one/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))


@pytest.mark.asyncio
async def test_create_one_to_one_rule(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["external"] == "10.0.1.20"
    assert body["target_host_ids"] == [host["id"]]
    assert body["deployments"] == []
    assert body["workflow_state"] == "draft"


@pytest.mark.asyncio
async def test_deploy_one_to_one_success_and_confirm(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_nat_push(respx_mock, "https://fw1.test")

    resp = await client.post(f"/api/one-to-one/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "pending"
    assert dep["revert_deadline"] is not None

    confirmed = await client.post(f"/api/one-to-one/{rule_id}/confirm")
    assert confirmed.json()["deployments"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_payload_fields(client, respx_mock):
    import json
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/one-to-one", json=_nat_payload([h["id"]], enabled=False, source_not=True))).json()
    _mock_nat_push(respx_mock, "https://fw1.test")
    assert (await client.post(f"/api/one-to-one/{r['id']}/deploy")).status_code == 200
    sent = json.loads(next(c.request.content for c in respx_mock.calls if c.request.url.path.endswith("addRule")))["rule"]
    assert sent["enabled"] == "0"
    assert sent["source_not"] == "1"
    assert sent["source_net"] == "any"
    assert sent["description"] == "Web to internal server"
    assert "descr" not in sent and "disabled" not in sent and "source" not in sent


@pytest.mark.asyncio
async def test_deploy_one_to_one_mixed_host_failure(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/firewall/filter/getRule").mock(return_value=httpx.Response(200, json={"rule": {"interface": {"wan": {"value": "WAN"}}}}))
    respx_mock.get(url__regex=r".*/firewall/alias/searchItem").mock(return_value=httpx.Response(200, json={"rows": []}))
    host_ok = await _create_host(client, "fw-ok", "https://fw-ok.test")
    host_down = await _create_host(client, "fw-down", "https://fw-down.test")

    resp = await client.post("/api/one-to-one", json=_nat_payload([host_ok["id"], host_down["id"]]))
    rule_id = resp.json()["id"]

    _mock_nat_push(respx_mock, "https://fw-ok.test")
    respx_mock.post("https://fw-down.test/api/firewall/one_to_one/addRule").mock(side_effect=httpx.ConnectError("connection refused"))

    resp = await client.post(f"/api/one-to-one/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    deployments = {d["host_id"]: d for d in resp.json()["deployments"]}
    assert deployments[host_ok["id"]]["status"] == "pending"
    assert deployments[host_down["id"]]["status"] == "error"
    assert deployments[host_down["id"]]["message"]


@pytest.mark.asyncio
async def test_one_to_one_edit_creates_revision_history(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))).json()

    updated = await client.put(
        f"/api/one-to-one/{r['id']}",
        json=_nat_payload([host["id"]], external="10.0.1.99", expected_updated_at=r["updated_at"]),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["external"] == "10.0.1.99"

    history = (await client.get(f"/api/one-to-one/{r['id']}/revisions")).json()
    assert len(history) == 2
    assert history[1]["snapshot"]["external"] == "10.0.1.20"


@pytest.mark.asyncio
async def test_stale_one_to_one_editor_rejected(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))).json()
    stale = await client.put(f"/api/one-to-one/{r['id']}", json=_nat_payload([host["id"]], expected_updated_at=r["updated_at"]))
    assert stale.status_code == 200
    conflict = await client.put(f"/api/one-to-one/{r['id']}", json=_nat_payload([host["id"]], expected_updated_at=r["updated_at"]))
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_delete_one_to_one_rule(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))).json()

    resp = await client.delete(f"/api/one-to-one/{r['id']}")
    assert resp.status_code == 204
    assert (await client.get("/api/one-to-one")).json() == []


@pytest.mark.asyncio
async def test_deploy_one_to_one_without_targets_rejected(client: AsyncClient):
    r = (await client.post("/api/one-to-one", json=_nat_payload([]))).json()
    resp = await client.post(f"/api/one-to-one/{r['id']}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_one_to_one_stale_preview_token_rejected(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))).json()

    resp = await client.post(f"/api/one-to-one/{r['id']}/deploy", json={"preview_token": "0" * 64})
    assert resp.status_code == 409
