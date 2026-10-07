"""CRUD/preview/edit-conflict for PolicyHierarchyRule — analog to
test_rules.py, adapted for the hierarchy-group-based targeting model
(no target_host_ids/target_group_ids; targeting is entirely via
hierarchy_group_id + the group tree, see policy_hierarchy_service)."""
import httpx
import pytest


async def _create_group(client, name: str, parent_id: str | None = None, **overrides) -> dict:
    payload = {"name": name, "description": "", "parent_id": parent_id}
    payload.update(overrides)
    resp = await client.post("/api/policy-hierarchy/groups", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_host_in_group(client, name: str, base_url: str, group_id: str | None = None) -> dict:
    resp = await client.post(
        "/api/hosts",
        json={
            "name": name, "base_url": base_url, "api_key": "key", "api_secret": "secret",
            "verify_tls": False, "policy_hierarchy_group_id": group_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _hrule_payload(hierarchy_group_id: str, **overrides) -> dict:
    payload = {
        "hierarchy_group_id": hierarchy_group_id,
        "rulebase": "pre",
        "description": "Allow SSH",
        "interface": "lan",
        "direction": "in",
        "protocol": "tcp",
        "action": "pass",
        "source_net": "any",
        "destination_net": "lan",
        "destination_port": "22",
    }
    payload.update(overrides)
    return payload


def mock_status(respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/firewall/filter/searchRule.*").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )


def mock_push(respx_mock, name="fw1", uuid_="rule-1"):
    add = respx_mock.post(f"https://{name}.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": uuid_})
    )
    respx_mock.post(f"https://{name}.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    return add


@pytest.mark.asyncio
async def test_create_rule(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    host = await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])

    resp = await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["description"] == "Allow SSH"
    assert body["hierarchy_group_id"] == group["id"]
    assert body["rulebase"] == "pre"
    assert body["sequence"] == 0
    assert body["effective_host_ids"] == [host["id"]]
    assert body["deployments"] == []
    assert body["workflow_state"] == "draft"


@pytest.mark.asyncio
async def test_effective_hosts_includes_descendant_group_members(client, respx_mock):
    mock_status(respx_mock)
    root = await _create_group(client, "Root")
    leaf = await _create_group(client, "Leaf", root["id"])
    host = await _create_host_in_group(client, "fw1", "https://fw1.test", leaf["id"])

    resp = await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(root["id"]))
    assert resp.status_code == 201, resp.text
    # A rule authored on the root group reaches hosts in its descendant
    # (leaf) group too — that's the entire point of the hierarchy.
    assert resp.json()["effective_host_ids"] == [host["id"]]


@pytest.mark.asyncio
async def test_deploy_rule_success(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])

    resp = await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))
    rule_id = resp.json()["id"]

    mock_push(respx_mock)
    resp = await client.post(f"/api/policy-hierarchy/rules/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["deployments"]) == 1
    dep = body["deployments"][0]
    assert dep["status"] == "pending"
    assert dep["message"] is None
    assert body["workflow_state"] == "pending"


@pytest.mark.asyncio
async def test_deploy_rule_without_targets_rejected(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    resp = await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))
    rule_id = resp.json()["id"]

    resp = await client.post(f"/api/policy-hierarchy/rules/{rule_id}/deploy")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_rule(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    resp = await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))
    rule_id = resp.json()["id"]

    resp = await client.delete(f"/api/policy-hierarchy/rules/{rule_id}")
    assert resp.status_code == 204

    resp = await client.get("/api/policy-hierarchy/rules")
    assert resp.status_code == 200
    assert all(r["id"] != rule_id for r in resp.json())


@pytest.mark.asyncio
async def test_edit_conflict_rejected(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    created = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))).json()

    stale_payload = {**_hrule_payload(group["id"], description="Changed"), "expected_updated_at": created["updated_at"]}
    ok = await client.put(f"/api/policy-hierarchy/rules/{created['id']}", json=stale_payload)
    assert ok.status_code == 200, ok.text
    fresh_updated_at = ok.json()["updated_at"]

    # Reusing the now-stale updated_at must be rejected.
    conflict = await client.put(
        f"/api/policy-hierarchy/rules/{created['id']}",
        json={**_hrule_payload(group["id"], description="Changed again"), "expected_updated_at": created["updated_at"]},
    )
    assert conflict.status_code == 409

    retry = await client.put(
        f"/api/policy-hierarchy/rules/{created['id']}",
        json={**_hrule_payload(group["id"], description="Changed again"), "expected_updated_at": fresh_updated_at},
    )
    assert retry.status_code == 200, retry.text


@pytest.mark.asyncio
async def test_edit_blocked_while_deployment_pending(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])
    created = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))).json()
    mock_push(respx_mock)
    await client.post(f"/api/policy-hierarchy/rules/{created['id']}/deploy")

    resp = await client.put(
        f"/api/policy-hierarchy/rules/{created['id']}",
        json={**_hrule_payload(group["id"], description="x"), "expected_updated_at": created["updated_at"]},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_whitespace_rule_fields_rejected(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    for field in ("description", "interface", "source_net", "destination_net", "protocol"):
        resp = await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"], **{field: "   "}))
        assert resp.status_code == 422
