"""Management changes must stay local until an explicit push."""
import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models.rule_deployment import RuleDeployment
from app.services.rule_deploy_service import sweep_expired_deployments
from tests.test_rules import _create_host, _rule_payload


def mock_status(respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))


def mock_push(respx_mock, name="fw1"):
    add = respx_mock.post(f"https://{name}.test/api/firewall/filter/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": f"{name}-rule"}))
    respx_mock.post(f"https://{name}.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    # Only actually hit for multi-host deploys (the interface/alias
    # consistency checks short-circuit for a single target) — same label
    # on every host and no matching aliases here, since these tests aren't
    # exercising the opt6/opt7-style mismatch.
    respx_mock.get(f"https://{name}.test/api/firewall/filter/getRule").mock(
        return_value=httpx.Response(200, json={"rule": {"interface": {"lan": {"value": "LAN", "selected": 0}, "wan": {"value": "WAN", "selected": 0}}}})
    )
    respx_mock.get(f"https://{name}.test/api/firewall/alias/searchItem").mock(
        return_value=httpx.Response(200, json={"total": 0, "rowCount": 0, "current": 1, "rows": []})
    )
    return add


async def rule(client, hosts=None, **extra):
    response = await client.post("/api/rules", json=_rule_payload(hosts or [], **extra))
    assert response.status_code == 201, response.text
    return response.json()


async def group(client, hosts, name="Branch"):
    response = await client.post("/api/groups", json={"name": name, "host_ids": hosts})
    assert response.status_code == 201, response.text
    return response.json()


async def test_group_targets_deduplicated_and_disabled_hosts_skipped(client, respx_mock):
    mock_status(respx_mock)
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    await client.patch(f"/api/hosts/{h2['id']}", json={"enabled": False})
    g = await group(client, [h1["id"], h2["id"]])
    r = await rule(client, [h1["id"]], target_group_ids=[g["id"]])
    assert set(r["effective_host_ids"]) == {h1["id"], h2["id"]}
    plan = (await client.get(f"/api/rules/{r['id']}/preview")).json()
    assert [h["action"] for h in plan["hosts"]] == ["create", "disabled"]
    add = mock_push(respx_mock)
    result = await client.post(f"/api/rules/{r['id']}/deploy", json={"preview_token": plan["token"]})
    assert result.status_code == 200, result.text
    assert len(result.json()["deployments"]) == 1
    assert add.call_count == 1


async def test_group_edits_invalidate_push_preview_without_firewall_writes(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    g = await group(client, [h["id"]])
    r = await rule(client, target_group_ids=[g["id"]])
    plan = (await client.get(f"/api/rules/{r['id']}/preview")).json()
    response = await client.put(f"/api/groups/{g['id']}", json={"name": g["name"], "host_ids": []})
    assert response.status_code == 200
    response = await client.post(f"/api/rules/{r['id']}/deploy", json={"preview_token": plan["token"]})
    assert response.status_code == 409
    assert not any(c.request.method == "POST" for c in respx_mock.calls)


async def test_edit_history_restore_and_stale_editor_rejected(client):
    r = await rule(client)
    payload = _rule_payload([], description="Block SSH", action="block", expected_updated_at=r["updated_at"])
    updated = await client.put(f"/api/rules/{r['id']}", json=payload)
    assert updated.status_code == 200, updated.text
    assert (await client.put(f"/api/rules/{r['id']}", json=payload)).status_code == 409
    history = (await client.get(f"/api/rules/{r['id']}/revisions")).json()
    assert len(history) == 2
    assert history[0]["snapshot"]["action"] == "block"
    restored = await client.put(f"/api/rules/{r['id']}", json={**history[1]["snapshot"], "expected_updated_at": updated.json()["updated_at"]})
    assert restored.status_code == 200
    assert restored.json()["action"] == "pass"
    assert len((await client.get(f"/api/rules/{r['id']}/revisions")).json()) == 3


async def test_concurrent_edits_only_one_wins(client):
    r = await rule(client)
    a, b = await asyncio.gather(*[client.put(f"/api/rules/{r['id']}", json=_rule_payload([], description=name, expected_updated_at=r["updated_at"])) for name in ("A", "B")])
    assert sorted([a.status_code, b.status_code]) == [200, 409]


async def test_pending_cannot_be_edited_or_confirmed_by_redeploy(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = await rule(client, [h["id"]])
    add = mock_push(respx_mock)
    deployed = (await client.post(f"/api/rules/{r['id']}/deploy")).json()
    deadline = deployed["deployments"][0]["revert_deadline"]
    assert (await client.post(f"/api/rules/{r['id']}/deploy")).status_code == 409
    assert (await client.put(f"/api/rules/{r['id']}", json=_rule_payload([h["id"]], expected_updated_at=r["updated_at"]))).status_code == 409
    current = (await client.get("/api/rules")).json()[0]
    assert current["deployments"][0]["status"] == "pending"
    assert current["deployments"][0]["revert_deadline"] == deadline
    assert add.call_count == 1


async def test_edited_rule_push_and_rollback_restore_previous_payload(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = await rule(client, [h["id"]])
    mock_push(respx_mock)
    await client.post(f"/api/rules/{r['id']}/deploy")
    await client.post(f"/api/rules/{r['id']}/confirm")
    updated = await client.put(f"/api/rules/{r['id']}", json=_rule_payload([h["id"]], action="block", expected_updated_at=r["updated_at"]))
    assert updated.status_code == 200, updated.text
    plan = (await client.get(f"/api/rules/{r['id']}/preview")).json()
    assert {"field": "action", "before": "pass", "after": "block"} in plan["hosts"][0]["changes"]
    set_route = respx_mock.post("https://fw1.test/api/firewall/filter/setRule/fw1-rule").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    response = await client.post(f"/api/rules/{r['id']}/deploy", json={"preview_token": plan["token"]})
    assert response.status_code == 200, response.text
    async with SessionLocal() as db:
        d = (await db.scalars(select(RuleDeployment))).one()
        assert d.previous_payload["action"] == "pass"
        assert d.last_payload["action"] == "block"
        d.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        await sweep_expired_deployments(db)
        assert d.last_payload["action"] == "pass"
    assert set_route.call_count == 2


async def test_removed_targets_remain_visible_and_group_in_use_cannot_be_deleted(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    g = await group(client, [h["id"]])
    r = await rule(client, target_group_ids=[g["id"]])
    mock_push(respx_mock)
    await client.post(f"/api/rules/{r['id']}/deploy")
    await client.post(f"/api/rules/{r['id']}/confirm")
    assert (await client.delete(f"/api/groups/{g['id']}")).status_code == 409
    await client.put(f"/api/groups/{g['id']}", json={"name": g["name"], "host_ids": []})
    plan = (await client.get(f"/api/rules/{r['id']}/preview")).json()
    assert plan["hosts"][0]["action"] == "retained"
    assert plan["hosts"][0]["host_name"] == "fw1"
    assert not plan["can_deploy"]
    assert (await client.get("/api/rules")).json()[0]["deployments"][0]["host_name"] == "fw1"


async def test_invalid_group_and_missing_members_rejected(client):
    assert (await client.post("/api/groups", json={"name": "   "})).status_code == 422
    assert (await client.post("/api/groups", json={"name": "Bad", "host_ids": [str(uuid.uuid4())]})).status_code == 400
    await group(client, [], "Unique")
    assert (await client.post("/api/groups", json={"name": "Unique"})).status_code == 409
    assert (await client.post("/api/rules", json=_rule_payload([], target_group_ids=[str(uuid.uuid4())]))).status_code == 400


async def test_new_endpoints_require_authentication(anon_client):
    for path in ("/api/groups", f"/api/rules/{uuid.uuid4()}/revisions", f"/api/rules/{uuid.uuid4()}/preview"):
        assert (await anon_client.get(path)).status_code == 401


async def test_apply_failure_preserves_uuid_and_retry_updates_existing_rule(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = await rule(client, [h["id"]])
    add = mock_push(respx_mock)
    apply = respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(side_effect=httpx.ConnectError("lost connection"))
    response = await client.post(f"/api/rules/{r['id']}/deploy")
    assert response.status_code == 200
    assert response.json()["deployments"][0]["status"] == "error"
    apply.mock(return_value=httpx.Response(200, json={"status": "ok"}))
    update = respx_mock.post("https://fw1.test/api/firewall/filter/setRule/fw1-rule").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    response = await client.post(f"/api/rules/{r['id']}/deploy")
    assert response.status_code == 200, response.text
    assert response.json()["deployments"][0]["status"] == "pending"
    assert add.call_count == 1
    assert update.call_count == 1


async def test_failed_delete_retains_tracking_for_retry(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = await rule(client, [h["id"]])
    mock_push(respx_mock)
    await client.post(f"/api/rules/{r['id']}/deploy")
    await client.post(f"/api/rules/{r['id']}/confirm")
    delete = respx_mock.post("https://fw1.test/api/firewall/filter/delRule/fw1-rule").mock(side_effect=httpx.ConnectError("lost connection"))
    response = await client.delete(f"/api/rules/{r['id']}")
    assert response.status_code == 502, response.text
    remaining = (await client.get("/api/rules")).json()
    assert remaining[0]["deployments"][0]["status"] == "error"
    delete.mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert (await client.delete(f"/api/rules/{r['id']}")).status_code == 204
    assert (await client.get("/api/rules")).json() == []


async def test_additive_schema_upgrade_keeps_existing_hosts_and_rules(client, respx_mock):
    from app.database import Base, engine
    from app.models.policy import DeviceGroup, RuleRevision, AliasRevision, group_members, rule_groups, alias_groups
    from app.models.alias import FirewallAlias
    from app.models.alias_deployment import AliasDeployment
    from app.models.alias_target import alias_targets
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = await rule(client, [h["id"]])
    # Simulate the previous schema: the new tables do not exist yet.
    # policy_hierarchy_groups is kept alongside opnsense_hosts (unlike the
    # other newer tables dropped below) because opnsense_hosts.
    # policy_hierarchy_group_id is a real FK to it in the current models —
    # in production that FK column only exists after the documented manual
    # ALTER TABLE step (see README), so it can never actually predate its
    # own referenced table the way this drop/recreate simulation would
    # otherwise require.
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=[
            table for table in Base.metadata.sorted_tables
            if table.name not in {"opnsense_hosts", "firewall_rules", "rule_targets", "rule_deployments",
                                   "admin_users", "sessions", "policy_hierarchy_groups"}
        ]))
        await conn.run_sync(Base.metadata.create_all)
    current = (await client.get("/api/rules")).json()[0]
    assert current["id"] == r["id"]
    assert current["target_host_ids"] == [h["id"]]
    assert (await client.get("/api/hosts")).json()[0]["id"] == h["id"]
    assert (await client.get(f"/api/rules/{r['id']}/revisions")).json() == []
    response = await client.put(f"/api/rules/{r['id']}", json=_rule_payload([h["id"]], description="Edited legacy rule", expected_updated_at=r["updated_at"]))
    assert response.status_code == 200, response.text
    history = (await client.get(f"/api/rules/{r['id']}/revisions")).json()
    assert len(history) == 2
    assert history[1]["snapshot"]["description"] == r["description"]
