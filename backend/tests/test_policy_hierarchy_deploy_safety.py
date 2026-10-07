"""Deploy-lockout safety net for PolicyHierarchyRule — analog to
test_deploy_safety.py, plus assertions that the moveRuleBefore ordering
step actually runs when more than one hierarchy rule is deployed onto the
same host/interface/rulebase."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.services.policy_hierarchy_deploy_service import sweep_expired_hierarchy_deployments
from tests.test_policy_hierarchy import _create_group, _create_host_in_group, _hrule_payload, mock_push, mock_status


@pytest.mark.asyncio
async def test_deploy_starts_in_pending_state(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])
    rule_id = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))).json()["id"]

    mock_push(respx_mock)
    resp = await client.post(f"/api/policy-hierarchy/rules/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "pending"
    assert dep["revert_deadline"] is not None


@pytest.mark.asyncio
async def test_confirm_makes_deployment_permanent(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])
    rule_id = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))).json()["id"]

    mock_push(respx_mock)
    await client.post(f"/api/policy-hierarchy/rules/{rule_id}/deploy")

    resp = await client.post(f"/api/policy-hierarchy/rules/{rule_id}/confirm")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert dep["revert_deadline"] is None


@pytest.mark.asyncio
async def test_second_pre_rule_triggers_move_rule_before(client, respx_mock):
    """Two pre-rules on the SAME group/host/interface: deploying the
    second one must reposition it after the first via moveRuleBefore —
    this is the "quick"-safety-critical ordering step plain FirewallRule
    deploys don't have at all."""
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])

    r1 = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"], description="First"))).json()
    r2 = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"], description="Second"))).json()
    assert r1["sequence"] < r2["sequence"]

    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(
        side_effect=[
            httpx.Response(200, json={"result": "saved", "uuid": "uuid-1"}),
            httpx.Response(200, json={"result": "saved", "uuid": "uuid-2"}),
        ]
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    move = respx_mock.post("https://fw1.test/api/firewall/filter/moveRuleBefore/uuid-1/uuid-2").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    resp1 = await client.post(f"/api/policy-hierarchy/rules/{r1['id']}/deploy")
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["deployments"][0]["status"] == "pending"
    assert move.call_count == 0  # only one anchor so far — nothing to reorder yet

    resp2 = await client.post(f"/api/policy-hierarchy/rules/{r2['id']}/deploy")
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["deployments"][0]["status"] == "pending"
    assert move.call_count == 1


@pytest.mark.asyncio
async def test_repositioning_failure_marks_deployment_as_error_not_pending(client, respx_mock):
    """If the rule content itself lands (addRule/apply succeed) but the
    follow-up moveRuleBefore call fails, the deployment must go straight
    to "error" — a live-but-mispositioned "quick" rule is a
    security-relevant fact, not a soft "unconfirmed" pending state."""
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])

    r1 = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"], description="First"))).json()
    r2 = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"], description="Second"))).json()

    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(
        side_effect=[
            httpx.Response(200, json={"result": "saved", "uuid": "uuid-1"}),
            httpx.Response(200, json={"result": "saved", "uuid": "uuid-2"}),
        ]
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx_mock.post("https://fw1.test/api/firewall/filter/moveRuleBefore/uuid-1/uuid-2").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    await client.post(f"/api/policy-hierarchy/rules/{r1['id']}/deploy")
    resp2 = await client.post(f"/api/policy-hierarchy/rules/{r2['id']}/deploy")
    assert resp2.status_code == 200, resp2.text
    dep = resp2.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"]


@pytest.mark.asyncio
async def test_sweep_reverts_fresh_deploy_by_deleting_it(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])
    rule_id = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))).json()["id"]

    mock_push(respx_mock, uuid_="abc-123")
    await client.post(f"/api/policy-hierarchy/rules/{rule_id}/deploy")

    del_route = respx_mock.post("https://fw1.test/api/firewall/filter/delRule/abc-123").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    async with SessionLocal() as db:
        deployment = (await db.execute(select(PolicyHierarchyRuleDeployment))).scalar_one()
        deployment.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        await sweep_expired_hierarchy_deployments(db)

        remaining = (await db.execute(select(PolicyHierarchyRuleDeployment))).scalar_one_or_none()
        assert remaining is None

    assert del_route.called


@pytest.mark.asyncio
async def test_sweep_reverts_redeploy_by_restoring_previous_payload(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    host = await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])
    rule_id = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))).json()["id"]

    old_payload = {"description": "old", "action": "pass"}
    new_payload = {"description": "new", "action": "block"}

    set_route = respx_mock.post("https://fw1.test/api/firewall/filter/setRule/existing-uuid").mock(
        return_value=httpx.Response(200, json={"result": "saved"})
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))

    async with SessionLocal() as db:
        deployment = PolicyHierarchyRuleDeployment(
            policy_hierarchy_id=uuid.UUID(rule_id),
            host_id=uuid.UUID(host["id"]),
            status="pending",
            opnsense_policy_hierarchy_uuid="existing-uuid",
            previous_payload=old_payload,
            last_payload=new_payload,
            revert_deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        db.add(deployment)
        await db.commit()

        reverted = await sweep_expired_hierarchy_deployments(db)
        assert len(reverted) == 1
        assert reverted[0].status == "reverted"
        assert reverted[0].last_payload == old_payload

    assert set_route.called
    sent = json.loads(set_route.calls.last.request.content)
    assert sent["rule"] == old_payload


@pytest.mark.asyncio
async def test_sweep_marks_revert_failed_when_host_unreachable(client, respx_mock):
    mock_status(respx_mock)
    group = await _create_group(client, "Root")
    await _create_host_in_group(client, "fw1", "https://fw1.test", group["id"])
    rule_id = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"]))).json()["id"]

    mock_push(respx_mock, uuid_="abc-123")
    await client.post(f"/api/policy-hierarchy/rules/{rule_id}/deploy")

    respx_mock.post("https://fw1.test/api/firewall/filter/delRule/abc-123").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with SessionLocal() as db:
        deployment = (await db.execute(select(PolicyHierarchyRuleDeployment))).scalar_one()
        deployment.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        reverted = await sweep_expired_hierarchy_deployments(db)
        assert len(reverted) == 1
        assert reverted[0].status == "revert_failed"
        assert reverted[0].message

        still_there = (await db.execute(select(PolicyHierarchyRuleDeployment))).scalar_one_or_none()
        assert still_there is not None
        assert still_there.status == "revert_failed"
