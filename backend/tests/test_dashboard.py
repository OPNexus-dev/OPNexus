import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models.alias import FirewallAlias
from app.models.alias_deployment import AliasDeployment
from app.models.host import OpnsenseHost
from app.models.host_operation import HostOperation
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment


@pytest.mark.asyncio
async def test_dashboard_summary_aggregates_pending_actions_across_kinds(client, respx_mock):
    import httpx

    respx_mock.get("https://fw-dash.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )

    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-dash", "base_url": "https://fw-dash.test", "api_key": "k", "api_secret": "s"},
    )
    assert resp.status_code == 201, resp.text
    host_id = uuid.UUID(resp.json()["id"])

    async with SessionLocal() as db:
        rule = FirewallRule(description="Test-Regel", interface="lan", direction="in")
        db.add(rule)
        await db.flush()
        db.add(RuleDeployment(rule_id=rule.id, host_id=host_id, status="error", message="Zeitüberschreitung"))

        group = PolicyHierarchyGroup(name="Zentrale")
        db.add(group)
        await db.flush()
        hierarchy_rule = PolicyHierarchyRule(
            hierarchy_group_id=group.id, rulebase="pre", description="Hierarchie-Regel",
            interface="wan", direction="in",
        )
        db.add(hierarchy_rule)
        await db.flush()
        db.add(PolicyHierarchyRuleDeployment(policy_hierarchy_id=hierarchy_rule.id, host_id=host_id, status="pending"))

        db.add(HostOperation(
            host_id=host_id, host_name="fw-dash", actor="admin", action="update", status="submitted",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), fingerprint="f", snapshot={},
        ))
        await db.commit()

    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_kind = {item["kind"]: item for item in body["pending_actions"]}
    assert by_kind["rule"]["entity_name"] == "Test-Regel"
    assert by_kind["rule"]["status"] == "error"
    assert by_kind["rule"]["link"] == f"/rules?rule={rule.id}"
    assert by_kind["policy_hierarchy"]["entity_name"] == "Hierarchie-Regel"
    assert by_kind["policy_hierarchy"]["link"] == f"/policy-hierarchy?group={group.id}"
    assert by_kind["host_operation"]["entity_name"] == "Firmware-Update"
    assert by_kind["host_operation"]["host_name"] == "fw-dash"

    host_names = {h["name"] for h in body["hosts"]}
    assert "fw-dash" in host_names


@pytest.mark.asyncio
async def test_dashboard_excludes_settled_deployments(client, respx_mock):
    import httpx

    respx_mock.get("https://fw-settled.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )

    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-settled", "base_url": "https://fw-settled.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = uuid.UUID(resp.json()["id"])

    async with SessionLocal() as db:
        rule = FirewallRule(description="Erledigte Regel", interface="lan", direction="in")
        db.add(rule)
        await db.flush()
        db.add(RuleDeployment(rule_id=rule.id, host_id=host_id, status="success"))
        await db.commit()

    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    names = {item["entity_name"] for item in resp.json()["pending_actions"]}
    assert "Erledigte Regel" not in names


@pytest.mark.asyncio
async def test_dashboard_includes_recent_activity(client):
    from app.services import audit_service

    await audit_service.persist(
        kind="rule", action="deploy", status="success",
        entity_id=str(uuid.uuid4()), entity_name="Audit-Regel", host_id=None, host_name=None, message="ok",
    )

    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    names = {item["entity_name"] for item in resp.json()["recent_activity"]}
    assert "Audit-Regel" in names


@pytest.mark.asyncio
async def test_dashboard_lists_stuck_alias_deployment(client, respx_mock):
    import httpx

    respx_mock.get("https://fw-alias.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-alias", "base_url": "https://fw-alias.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = uuid.UUID(resp.json()["id"])

    async with SessionLocal() as db:
        alias = FirewallAlias(name="Gitea", type="host", content="10.0.0.1", description="")
        db.add(alias)
        await db.flush()
        db.add(AliasDeployment(alias_id=alias.id, host_id=host_id, status="revert_failed", message="Alias in use"))
        await db.commit()

    body = (await client.get("/api/dashboard")).json()
    item = next(i for i in body["pending_actions"] if i["kind"] == "alias")
    assert item["entity_name"] == "Gitea"
    assert item["status"] == "revert_failed"
    assert item["link"] == f"/aliases?alias={alias.id}"
