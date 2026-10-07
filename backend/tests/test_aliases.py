import httpx
import pytest

from httpx import AsyncClient

from tests.test_rules import _create_host


def _alias_payload(target_host_ids: list[str], **overrides) -> dict:
    payload = {
        "name": "Web_Backends",
        "type": "host",
        "content": "10.0.1.10\n10.0.1.11",
        "description": "Webserver Backends",
        "target_host_ids": target_host_ids,
    }
    payload.update(overrides)
    return payload


def _mock_alias_push(respx_mock, base_url: str, uuid_: str = "alias-uuid-1"):
    respx_mock.post(f"{base_url}/api/firewall/alias/addItem").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": uuid_})
    )
    respx_mock.post(f"{base_url}/api/firewall/alias/reconfigure").mock(return_value=httpx.Response(200, json={"status": "ok"}))


@pytest.mark.asyncio
async def test_create_alias(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/aliases", json=_alias_payload([host["id"]]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Web_Backends"
    assert body["content"] == "10.0.1.10\n10.0.1.11"
    assert body["target_host_ids"] == [host["id"]]
    assert body["deployments"] == []
    assert body["workflow_state"] == "draft"


@pytest.mark.asyncio
async def test_create_alias_duplicate_name_rejected(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    assert (await client.post("/api/aliases", json=_alias_payload([host["id"]]))).status_code == 201
    dupe = await client.post("/api/aliases", json=_alias_payload([host["id"]]))
    assert dupe.status_code == 409


@pytest.mark.asyncio
async def test_deploy_alias_success(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/aliases", json=_alias_payload([host["id"]]))
    alias_id = resp.json()["id"]

    _mock_alias_push(respx_mock, "https://fw1.test")

    resp = await client.post(f"/api/aliases/{alias_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "pending"
    assert dep["revert_deadline"] is not None

    confirmed = await client.post(f"/api/aliases/{alias_id}/confirm")
    assert confirmed.json()["deployments"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_deploy_alias_mixed_host_failure(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host_ok = await _create_host(client, "fw-ok", "https://fw-ok.test")
    host_down = await _create_host(client, "fw-down", "https://fw-down.test")

    resp = await client.post("/api/aliases", json=_alias_payload([host_ok["id"], host_down["id"]]))
    alias_id = resp.json()["id"]

    _mock_alias_push(respx_mock, "https://fw-ok.test")
    respx_mock.post("https://fw-down.test/api/firewall/alias/addItem").mock(side_effect=httpx.ConnectError("connection refused"))

    resp = await client.post(f"/api/aliases/{alias_id}/deploy")
    assert resp.status_code == 200, resp.text
    deployments = {d["host_id"]: d for d in resp.json()["deployments"]}
    assert deployments[host_ok["id"]]["status"] == "pending"
    assert deployments[host_down["id"]]["status"] == "error"
    assert deployments[host_down["id"]]["message"]


@pytest.mark.asyncio
async def test_alias_edit_creates_revision_history(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/aliases", json=_alias_payload([host["id"]]))).json()

    updated = await client.put(
        f"/api/aliases/{r['id']}",
        json=_alias_payload([host["id"]], content="10.0.1.10", expected_updated_at=r["updated_at"]),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["content"] == "10.0.1.10"

    history = (await client.get(f"/api/aliases/{r['id']}/revisions")).json()
    assert len(history) == 2
    assert history[1]["snapshot"]["content"] == "10.0.1.10\n10.0.1.11"


@pytest.mark.asyncio
async def test_stale_alias_editor_rejected(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/aliases", json=_alias_payload([host["id"]]))).json()
    stale = await client.put(f"/api/aliases/{r['id']}", json=_alias_payload([host["id"]], expected_updated_at=r["updated_at"]))
    assert stale.status_code == 200
    conflict = await client.put(f"/api/aliases/{r['id']}", json=_alias_payload([host["id"]], expected_updated_at=r["updated_at"]))
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_delete_alias(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.post("/api/aliases", json=_alias_payload([host["id"]]))).json()

    resp = await client.delete(f"/api/aliases/{r['id']}")
    assert resp.status_code == 204
    assert (await client.get("/api/aliases")).json() == []


@pytest.mark.asyncio
async def test_deploy_alias_without_targets_rejected(client: AsyncClient):
    r = (await client.post("/api/aliases", json=_alias_payload([]))).json()
    resp = await client.post(f"/api/aliases/{r['id']}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_alias_group_target_and_preview(client: AsyncClient, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    group = (await client.post("/api/groups", json={"name": "Branch", "host_ids": [host["id"]]})).json()

    r = (await client.post("/api/aliases", json=_alias_payload([], target_group_ids=[group["id"]]))).json()
    assert r["effective_host_ids"] == [host["id"]]

    plan = (await client.get(f"/api/aliases/{r['id']}/preview")).json()
    assert plan["hosts"][0]["action"] == "create"

    _mock_alias_push(respx_mock, "https://fw1.test")
    deployed = await client.post(f"/api/aliases/{r['id']}/deploy", json={"preview_token": plan["token"]})
    assert deployed.status_code == 200, deployed.text


@pytest.mark.asyncio
async def test_rule_deploy_blocked_while_referenced_alias_unconfirmed(client: AsyncClient, respx_mock):
    # Regression: a rule deployed while its alias was still pending made the
    # alias's auto-rollback fail ("Alias in use") and left it in revert_failed.
    from tests.test_rules import _rule_payload

    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    alias_id = (await client.post("/api/aliases", json=_alias_payload([host["id"]]))).json()["id"]
    _mock_alias_push(respx_mock, "https://fw1.test")
    assert (await client.post(f"/api/aliases/{alias_id}/deploy")).status_code == 200

    rule_id = (await client.post("/api/rules", json=_rule_payload([host["id"]], destination_net="Web_Backends"))).json()["id"]
    add = respx_mock.post(url__regex=r".*/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "r1"}))
    respx_mock.post(url__regex=r".*/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))

    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ALIAS_NOCH_NICHT_BESTAETIGT"
    assert resp.json()["detail"]["params"]["alias"] == "Web_Backends"
    assert add.call_count == 0

    assert (await client.post(f"/api/aliases/{alias_id}/confirm")).status_code == 200
    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    assert add.call_count == 1
