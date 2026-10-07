import httpx
import pytest

from tests.test_policy_hierarchy import (
    _create_group,
    _create_host_in_group,
    _hrule_payload,
    mock_status,
)


RESOURCE_CASES = [
    ("/api/aliases", {"name": "Inherited_Alias", "type": "host", "content": "192.0.2.1"}),
    ("/api/port-forward", {"description": "Inherited PF", "interface": "wan", "target": "192.0.2.10"}),
    ("/api/one-to-one", {"description": "Inherited 1:1", "interface": "wan", "external": "192.0.2.20"}),
    ("/api/outbound-nat", {"description": "Inherited outbound", "interface": "wan", "target": "192.0.2.30"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,payload", RESOURCE_CASES)
async def test_alias_and_nat_targets_follow_hierarchy_descendants_dynamically(
    client, respx_mock, outbound_mode_hybrid, endpoint, payload
):
    mock_status(respx_mock)
    root = await _create_group(client, "Root")
    leaf = await _create_group(client, "Leaf", root["id"])
    first = await _create_host_in_group(client, "fw1", "https://fw1.test", leaf["id"])

    created = await client.post(endpoint, json={
        **payload,
        "target_host_ids": [],
        "target_group_ids": [],
        "policy_hierarchy_group_id": root["id"],
    })
    assert created.status_code == 201, created.text
    assert created.json()["effective_host_ids"] == [first["id"]]

    # A future descendant/host is inherited without editing the resource.
    future_leaf = await _create_group(client, "Future leaf", root["id"])
    second = await _create_host_in_group(client, "fw2", "https://fw2.test", future_leaf["id"])
    listed = await client.get(endpoint)
    assert listed.status_code == 200, listed.text
    current = next(row for row in listed.json() if row["id"] == created.json()["id"])
    assert current["policy_hierarchy_group_id"] == root["id"]
    assert set(current["effective_host_ids"]) == {first["id"], second["id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,payload", RESOURCE_CASES)
async def test_hierarchy_group_with_inherited_resource_cannot_be_deleted(
    client, respx_mock, outbound_mode_hybrid, endpoint, payload
):
    mock_status(respx_mock)
    group = await _create_group(client, "Protected")
    created = await client.post(endpoint, json={
        **payload,
        "policy_hierarchy_group_id": group["id"],
    })
    assert created.status_code == 201, created.text
    blocked = await client.delete(f"/api/policy-hierarchy/groups/{group['id']}")
    assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_atomic_reorder_and_stale_write_protection(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    rules = []
    for description in ("First", "Second", "Third"):
        response = await client.post(
            "/api/policy-hierarchy/rules",
            json=_hrule_payload(group["id"], description=description),
        )
        assert response.status_code == 201, response.text
        rules.append(response.json())

    reversed_rules = list(reversed(rules))
    reordered = await client.put(
        f"/api/policy-hierarchy/groups/{group['id']}/rules/pre/order",
        json={"items": [
            {"id": rule["id"], "expected_updated_at": rule["updated_at"]}
            for rule in reversed_rules
        ]},
    )
    assert reordered.status_code == 200, reordered.text
    assert [row["description"] for row in reordered.json()] == ["Third", "Second", "First"]
    assert [row["sequence"] for row in reordered.json()] == [0, 1, 2]

    stale = await client.put(
        f"/api/policy-hierarchy/groups/{group['id']}/rules/pre/order",
        json={"items": [
            {"id": rule["id"], "expected_updated_at": rule["updated_at"]}
            for rule in rules
        ]},
    )
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_atomic_reorder_rejects_unknown_group(client):
    response = await client.put(
        "/api/policy-hierarchy/groups/00000000-0000-0000-0000-000000000001/rules/pre/order",
        json={"items": [{
            "id": "00000000-0000-0000-0000-000000000002",
            "expected_updated_at": "2026-09-10T00:00:00Z",
        }]},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_normal_rule_deploy_reasserts_inherited_hierarchy_order(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    host = await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])
    hierarchy_rules = []
    for description in ("First", "Second"):
        hierarchy_rules.append((await client.post(
            "/api/policy-hierarchy/rules",
            json=_hrule_payload(group["id"], description=description),
        )).json())

    normal = (await client.post("/api/rules", json={
        "description": "Normal managed rule",
        "interface": "lan",
        "direction": "in",
        "protocol": "tcp",
        "action": "pass",
        "source_net": "any",
        "destination_net": "lan",
        "target_host_ids": [host["id"]],
        "target_group_ids": [],
    })).json()

    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(side_effect=[
        httpx.Response(200, json={"result": "saved", "uuid": "hierarchy-1"}),
        httpx.Response(200, json={"result": "saved", "uuid": "hierarchy-2"}),
        httpx.Response(200, json={"result": "saved", "uuid": "normal-1"}),
    ])
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    move = respx_mock.post(
        "https://fw1.test/api/firewall/filter/moveRuleBefore/hierarchy-1/hierarchy-2"
    ).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    for rule in hierarchy_rules:
        response = await client.post(f"/api/policy-hierarchy/rules/{rule['id']}/deploy")
        assert response.status_code == 200, response.text
    assert move.call_count == 1

    response = await client.post(f"/api/rules/{normal['id']}/deploy")
    assert response.status_code == 200, response.text
    assert response.json()["deployments"][0]["status"] == "pending"
    assert move.call_count == 2


@pytest.mark.asyncio
async def test_reassert_brackets_existing_mvc_rules_with_pre_and_post(client, respx_mock):
    # This test supplies the live searchRule ordering instead of using
    # mock_status's empty compatibility response.
    respx_mock.get(url__regex=r".*/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])
    pre = (await client.post(
        "/api/policy-hierarchy/rules",
        json=_hrule_payload(group["id"], description="Pre"),
    )).json()
    post = (await client.post(
        "/api/policy-hierarchy/rules",
        json=_hrule_payload(group["id"], description="Post", rulebase="post"),
    )).json()

    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(side_effect=[
        httpx.Response(200, json={"result": "saved", "uuid": "pre-uuid"}),
        httpx.Response(200, json={"result": "saved", "uuid": "post-uuid"}),
    ])
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx_mock.get("https://fw1.test/api/firewall/filter/searchRule", params={"rowCount": -1}).mock(
        side_effect=[
            httpx.Response(200, json={"rows": [
                {"uuid": "manual-uuid", "prio_group": "4lan", "sort_order": 10},
                {"uuid": "pre-uuid", "prio_group": "4lan", "sort_order": 20},
            ]}),
            httpx.Response(200, json={"rows": [
                {"uuid": "post-uuid", "prio_group": "4lan", "sort_order": 5},
                {"uuid": "manual-uuid", "prio_group": "4lan", "sort_order": 10},
                {"uuid": "pre-uuid", "prio_group": "4lan", "sort_order": 20},
            ]}),
        ]
    )
    pre_before_manual = respx_mock.post(
        "https://fw1.test/api/firewall/filter/moveRuleBefore/pre-uuid/manual-uuid"
    ).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    manual_before_post = respx_mock.post(
        "https://fw1.test/api/firewall/filter/moveRuleBefore/manual-uuid/post-uuid"
    ).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    first = await client.post(f"/api/policy-hierarchy/rules/{pre['id']}/deploy")
    second = await client.post(f"/api/policy-hierarchy/rules/{post['id']}/deploy")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert pre_before_manual.call_count == 2
    assert manual_before_post.call_count == 1
