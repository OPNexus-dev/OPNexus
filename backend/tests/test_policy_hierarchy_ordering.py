"""Fixes the cross-group ordering direction: root-most group's pre-rule
must always end up ahead of a more specific (leaf) group's own pre-rule on
a shared host/interface, regardless of the order the two rules happen to
be deployed in — and the reverse for post-rules (leaf-most first). Since
every OPNexus-pushed rule sets "quick": "1", getting this backwards would
silently let a more specific rule shadow an org-wide guardrail.
"""
import httpx
import pytest

from tests.test_policy_hierarchy import _create_group, _create_host_in_group, _hrule_payload, mock_status


@pytest.mark.asyncio
async def test_pre_rule_order_is_root_first_regardless_of_deploy_order(client, respx_mock):
    mock_status(respx_mock)
    root = await _create_group(client, "Root")
    leaf = await _create_group(client, "Leaf", root["id"])
    await _create_host_in_group(client, "fw1", "https://fw1.test", leaf["id"])

    root_rule = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(root["id"], description="Root pre"))).json()
    leaf_rule = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(leaf["id"], description="Leaf pre"))).json()

    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(
        side_effect=[
            httpx.Response(200, json={"result": "saved", "uuid": "root-uuid"}),
            httpx.Response(200, json={"result": "saved", "uuid": "leaf-uuid"}),
        ]
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    move = respx_mock.post("https://fw1.test/api/firewall/filter/moveRuleBefore/root-uuid/leaf-uuid").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    # Deploy root first, then leaf.
    r1 = await client.post(f"/api/policy-hierarchy/rules/{root_rule['id']}/deploy")
    assert r1.status_code == 200, r1.text
    assert move.call_count == 0  # only one anchor deployed so far
    r2 = await client.post(f"/api/policy-hierarchy/rules/{leaf_rule['id']}/deploy")
    assert r2.status_code == 200, r2.text
    assert move.call_count == 1  # root-uuid moved to sit right before leaf-uuid


@pytest.mark.asyncio
async def test_pre_rule_order_root_first_even_when_leaf_deployed_first(client, respx_mock):
    mock_status(respx_mock)
    root = await _create_group(client, "Root")
    leaf = await _create_group(client, "Leaf", root["id"])
    await _create_host_in_group(client, "fw1", "https://fw1.test", leaf["id"])

    root_rule = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(root["id"], description="Root pre"))).json()
    leaf_rule = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(leaf["id"], description="Leaf pre"))).json()

    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(
        side_effect=[
            httpx.Response(200, json={"result": "saved", "uuid": "leaf-uuid"}),
            httpx.Response(200, json={"result": "saved", "uuid": "root-uuid"}),
        ]
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    # Regardless of deploy order, the FINAL enforced order is still
    # root-uuid before leaf-uuid — the direction is a property of the
    # tree, not of which rule happened to be pushed last.
    move = respx_mock.post("https://fw1.test/api/firewall/filter/moveRuleBefore/root-uuid/leaf-uuid").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    r1 = await client.post(f"/api/policy-hierarchy/rules/{leaf_rule['id']}/deploy")
    assert r1.status_code == 200, r1.text
    assert move.call_count == 0
    r2 = await client.post(f"/api/policy-hierarchy/rules/{root_rule['id']}/deploy")
    assert r2.status_code == 200, r2.text
    assert move.call_count == 1


@pytest.mark.asyncio
async def test_post_rule_order_is_leaf_first(client, respx_mock):
    mock_status(respx_mock)
    root = await _create_group(client, "Root")
    leaf = await _create_group(client, "Leaf", root["id"])
    await _create_host_in_group(client, "fw1", "https://fw1.test", leaf["id"])

    root_rule = (await client.post(
        "/api/policy-hierarchy/rules", json=_hrule_payload(root["id"], rulebase="post", description="Root post")
    )).json()
    leaf_rule = (await client.post(
        "/api/policy-hierarchy/rules", json=_hrule_payload(leaf["id"], rulebase="post", description="Leaf post")
    )).json()

    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(
        side_effect=[
            httpx.Response(200, json={"result": "saved", "uuid": "root-uuid"}),
            httpx.Response(200, json={"result": "saved", "uuid": "leaf-uuid"}),
        ]
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    # Post-rulebase: leaf-most group wins the "last word" position, i.e.
    # ends up positioned BEFORE the root group's post-rule.
    move = respx_mock.post("https://fw1.test/api/firewall/filter/moveRuleBefore/leaf-uuid/root-uuid").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    await client.post(f"/api/policy-hierarchy/rules/{root_rule['id']}/deploy")
    r2 = await client.post(f"/api/policy-hierarchy/rules/{leaf_rule['id']}/deploy")
    assert r2.status_code == 200, r2.text
    assert move.call_count == 1
