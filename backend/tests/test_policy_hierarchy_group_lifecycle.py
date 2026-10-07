"""Cycle rejection and delete protection for PolicyHierarchyGroup — analog
to test_group_lifecycle.py's guardrails for DeviceGroup."""
import pytest

from tests.test_policy_hierarchy import _create_group, _hrule_payload, mock_status


@pytest.mark.asyncio
async def test_self_parent_rejected(client):
    root = await _create_group(client, "Root")
    resp = await client.put(f"/api/policy-hierarchy/groups/{root['id']}", json={"name": "Root", "description": "", "parent_id": root["id"]})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_direct_cycle_rejected(client):
    root = await _create_group(client, "Root")
    leaf = await _create_group(client, "Leaf", root["id"])
    # Root's parent can't become its own child.
    resp = await client.put(f"/api/policy-hierarchy/groups/{root['id']}", json={"name": "Root", "description": "", "parent_id": leaf["id"]})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_indirect_cycle_rejected(client):
    root = await _create_group(client, "Root")
    mid = await _create_group(client, "Mid", root["id"])
    leaf = await _create_group(client, "Leaf", mid["id"])
    # root -> mid -> leaf; making root's parent = leaf would create a
    # 3-level cycle, not just a direct one.
    resp = await client.put(f"/api/policy-hierarchy/groups/{root['id']}", json={"name": "Root", "description": "", "parent_id": leaf["id"]})
    assert resp.status_code == 409
    # Re-parenting leaf under root's existing tree (mid -> leaf staying
    # put, just renamed) is unaffected and must still work.
    ok = await client.put(f"/api/policy-hierarchy/groups/{mid['id']}", json={"name": "Mid", "description": "updated", "parent_id": root["id"]})
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_delete_blocked_while_children_exist(client):
    root = await _create_group(client, "Root")
    leaf = await _create_group(client, "Leaf", root["id"])
    resp = await client.delete(f"/api/policy-hierarchy/groups/{root['id']}")
    assert resp.status_code == 409
    assert (await client.delete(f"/api/policy-hierarchy/groups/{leaf['id']}")).status_code == 204
    assert (await client.delete(f"/api/policy-hierarchy/groups/{root['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_delete_blocked_while_rules_assigned(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    rule = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))).json()

    resp = await client.delete(f"/api/policy-hierarchy/groups/{group['id']}")
    assert resp.status_code == 409

    assert (await client.delete(f"/api/policy-hierarchy/rules/{rule['id']}")).status_code == 204
    assert (await client.delete(f"/api/policy-hierarchy/groups/{group['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_duplicate_group_name_rejected(client):
    await _create_group(client, "Root")
    resp = await client.post("/api/policy-hierarchy/groups", json={"name": "Root", "description": "", "parent_id": None})
    assert resp.status_code == 409
