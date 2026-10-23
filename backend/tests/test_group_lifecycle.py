"""Device groups must lead to actionable, race-checked rule management."""
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models.rule_deployment import RuleDeployment
from app.services.rule_deploy_service import sweep_expired_deployments
from tests.test_policy_workflow import group, rule, mock_status, mock_push
from tests.test_rules import _create_host, _rule_payload


async def plan(client, rule_id):
    response = await client.get(f"/api/rules/{rule_id}/preview")
    assert response.status_code == 200, response.text
    return response.json()


async def test_workflow_states_follow_deployment_and_draft(client, respx_mock):
    r = await rule(client)
    assert r["workflow_state"] == "unassigned"
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = (await client.put(f"/api/rules/{r['id']}", json=_rule_payload([h['id']], expected_updated_at=r['updated_at']))).json()
    assert r["workflow_state"] == "draft"
    assert r["workflow_counts"]["changes"] == 1
    mock_push(respx_mock)
    pushed = (await client.post(f"/api/rules/{r['id']}/deploy")).json()
    assert pushed["workflow_state"] == "pending"
    confirmed = (await client.post(f"/api/rules/{r['id']}/confirm")).json()
    assert confirmed["workflow_state"] == "synced"
    changed = (await client.put(f"/api/rules/{r['id']}", json=_rule_payload([h['id']], action="block", expected_updated_at=r['updated_at']))).json()
    assert changed["workflow_state"] == "draft"
    await client.patch(f"/api/hosts/{h['id']}", json={"enabled": False})
    assert (await client.get('/api/rules')).json()[0]["workflow_state"] == "disabled"


async def test_group_impact_accounts_for_other_groups_and_direct_targets(client, respx_mock):
    mock_status(respx_mock)
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    g1 = await group(client, [h1['id']], "Group A")
    g2 = await group(client, [h1['id']], "Group B")
    r1 = await rule(client, target_group_ids=[g1['id'], g2['id']], description="Overlapping")
    r2 = await rule(client, [h1['id']], target_group_ids=[g1['id']], description="Direct")
    r3 = await rule(client, target_group_ids=[g1['id']], description="Only Group A")
    mock_push(respx_mock)
    await client.post(f"/api/rules/{r3['id']}/deploy")
    await client.post(f"/api/rules/{r3['id']}/confirm")
    response = await client.post(f"/api/groups/{g1['id']}/preview", json={"name": g1['name'], "host_ids": [h2['id']]})
    assert response.status_code == 200
    impact = response.json()
    entries = {e['rule_id']: e for e in impact['rules']}
    for r in (r1, r2):
        assert entries[r['id']]['removed_host_ids'] == []
        assert entries[r['id']]['added_host_ids'] == [h2['id']]
    assert entries[r3['id']]['retained_host_ids'] == [h1['id']]
    assert impact['removed_members'][0]['id'] == h1['id']
    # Preview did not persist membership changes.
    assert next(g for g in (await client.get('/api/groups')).json() if g['id'] == g1['id'])['host_ids'] == [h1['id']]


async def test_group_impact_token_rejects_changed_assignments(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    g = await group(client, [h['id']])
    proposed = {"name": g['name'], "host_ids": []}
    impact = (await client.post(f"/api/groups/{g['id']}/preview", json=proposed)).json()
    await rule(client, target_group_ids=[g['id']])
    stale = await client.put(f"/api/groups/{g['id']}", json={**proposed, "preview_token": impact['token']})
    assert stale.status_code == 409
    assert (await client.get('/api/groups')).json()[0]['host_ids'] == [h['id']]
    fresh = (await client.post(f"/api/groups/{g['id']}/preview", json=proposed)).json()
    assert (await client.put(f"/api/groups/{g['id']}", json={**proposed, "preview_token": fresh['token']})).status_code == 200


async def test_remove_previous_target_keeps_current_target_and_rule(client, respx_mock):
    mock_status(respx_mock)
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    g = await group(client, [h1['id'], h2['id']])
    r = await rule(client, target_group_ids=[g['id']])
    mock_push(respx_mock, "fw1"); mock_push(respx_mock, "fw2")
    await client.post(f"/api/rules/{r['id']}/deploy")
    await client.post(f"/api/rules/{r['id']}/confirm")
    await client.put(f"/api/groups/{g['id']}", json={"name": g['name'], "host_ids": [h2['id']]})
    assert (await client.get('/api/rules')).json()[0]['workflow_state'] == 'cleanup'
    preview = await plan(client, r['id'])
    delete = respx_mock.post('https://fw1.test/api/firewall/filter/delRule/fw1-rule').mock(return_value=httpx.Response(200, json={'status':'ok'}))
    result = await client.post(f"/api/rules/{r['id']}/deployments/{h1['id']}/remove", json={'preview_token':preview['token']})
    assert result.status_code == 204, result.text
    remaining = (await client.get('/api/rules')).json()[0]
    assert remaining['workflow_state'] == 'synced'
    assert [d['host_id'] for d in remaining['deployments']] == [h2['id']]
    assert delete.call_count == 1


async def test_current_or_readded_target_cannot_be_removed(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    g = await group(client, [h['id']])
    r = await rule(client, target_group_ids=[g['id']])
    mock_push(respx_mock)
    await client.post(f"/api/rules/{r['id']}/deploy")
    await client.post(f"/api/rules/{r['id']}/confirm")
    endpoint = f"/api/rules/{r['id']}/deployments/{h['id']}/remove"
    initial = await plan(client, r['id'])
    assert (await client.post(endpoint, json={'preview_token':initial['token']})).status_code == 409
    await client.put(f"/api/groups/{g['id']}", json={"name":g['name'],"host_ids":[]})
    old = await plan(client, r['id'])
    await client.put(f"/api/groups/{g['id']}", json={"name":g['name'],"host_ids":[h['id']]})
    assert (await client.post(endpoint, json={'preview_token':old['token']})).status_code == 409
    assert not any('/delRule/' in str(c.request.url) for c in respx_mock.calls)


async def test_cleanup_failure_is_retryable(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    g = await group(client, [h['id']])
    r = await rule(client, target_group_ids=[g['id']])
    mock_push(respx_mock)
    await client.post(f"/api/rules/{r['id']}/deploy")
    await client.post(f"/api/rules/{r['id']}/confirm")
    await client.put(f"/api/groups/{g['id']}", json={"name":g['name'],"host_ids":[]})
    delete = respx_mock.post('https://fw1.test/api/firewall/filter/delRule/fw1-rule').mock(side_effect=httpx.ConnectError('offline'))
    endpoint = f"/api/rules/{r['id']}/deployments/{h['id']}/remove"
    first = await plan(client, r['id'])
    assert (await client.post(endpoint, json={'preview_token':first['token']})).status_code == 502
    second = await plan(client, r['id'])
    assert second['hosts'][0]['can_remove']
    assert second['hosts'][0]['status'] == 'error'
    delete.mock(return_value=httpx.Response(200, json={'status':'ok'}))
    assert (await client.post(endpoint, json={'preview_token':second['token']})).status_code == 204
    assert (await client.get('/api/rules')).json()[0]['deployments'] == []


async def test_failed_rollback_can_be_retried_without_losing_tracking(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = await rule(client, [h['id']])
    mock_push(respx_mock)
    await client.post(f"/api/rules/{r['id']}/deploy")
    delete = respx_mock.post('https://fw1.test/api/firewall/filter/delRule/fw1-rule').mock(side_effect=httpx.ConnectError('offline'))
    async with SessionLocal() as db:
        deployment = (await db.scalars(select(RuleDeployment))).one()
        deployment.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        await sweep_expired_deployments(db)
    first = await plan(client, r['id'])
    assert first['hosts'][0]['can_retry_rollback']
    endpoint = f"/api/rules/{r['id']}/deployments/{h['id']}/retry-rollback"
    assert (await client.post(endpoint, json={'preview_token':first['token']})).status_code == 502
    assert (await client.get('/api/rules')).json()[0]['workflow_state'] == 'attention'
    delete.mock(return_value=httpx.Response(200, json={'status':'ok'}))
    second = await plan(client, r['id'])
    assert (await client.post(endpoint, json={'preview_token':second['token']})).status_code == 204
    assert (await client.get('/api/rules')).json()[0]['workflow_state'] == 'draft'


async def test_whitespace_rule_fields_rejected(client):
    for field in ('description','interface','source_net','destination_net','protocol'):
        assert (await client.post('/api/rules', json=_rule_payload([], **{field:'   '}))).status_code == 422


async def test_http_success_with_rejected_delete_does_not_lose_tracking(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    g = await group(client, [h['id']])
    r = await rule(client, target_group_ids=[g['id']])
    mock_push(respx_mock)
    await client.post(f"/api/rules/{r['id']}/deploy")
    await client.post(f"/api/rules/{r['id']}/confirm")
    await client.put(f"/api/groups/{g['id']}", json={"name":g['name'],"host_ids":[]})
    respx_mock.post('https://fw1.test/api/firewall/filter/delRule/fw1-rule').mock(return_value=httpx.Response(200, json={'result':'failed'}))
    preview = await plan(client, r['id'])
    response = await client.post(f"/api/rules/{r['id']}/deployments/{h['id']}/remove", json={'preview_token':preview['token']})
    assert response.status_code == 502
    remaining = (await client.get('/api/rules')).json()[0]
    assert remaining['deployments'][0]['status'] == 'error'


async def test_rejected_apply_is_not_reported_as_pending_success(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = await rule(client, [h['id']])
    mock_push(respx_mock)
    respx_mock.post('https://fw1.test/api/firewall/filter/apply').mock(return_value=httpx.Response(200, json={'status':'error'}))
    result = await client.post(f"/api/rules/{r['id']}/deploy")
    assert result.status_code == 200
    assert result.json()['workflow_state'] == 'attention'
    assert result.json()['deployments'][0]['status'] == 'error'


async def test_native_deleted_and_not_found_acknowledgements(client, respx_mock):
    from app.services.opnsense_client import del_rule
    from app.models.host import OpnsenseHost
    import uuid
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, uuid.UUID(h['id']))
        for result in ('deleted', 'not found'):
            respx_mock.post('https://fw1.test/api/firewall/filter/delRule/fw1-rule').mock(return_value=httpx.Response(200, json={'result':result}))
            assert (await del_rule(host, 'fw1-rule'))['result'] == result


async def test_rejected_draft_can_be_corrected_then_pushed(client, respx_mock):
    mock_status(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    r = await rule(client, [h['id']], interface='badif')
    add = respx_mock.post('https://fw1.test/api/firewall/filter/addRule').mock(return_value=httpx.Response(200, json={'result':'failed','validations':{'rule.interface':'Unknown interface'}}))
    rejected = await client.post(f"/api/rules/{r['id']}/deploy")
    assert rejected.json()['deployments'][0]['status'] == 'error'
    corrected = await client.put(f"/api/rules/{r['id']}", json=_rule_payload([h['id']], interface='lan', expected_updated_at=r['updated_at']))
    assert corrected.status_code == 200, corrected.text
    add.mock(return_value=httpx.Response(200, json={'result':'saved','uuid':'fw1-rule'}))
    respx_mock.post('https://fw1.test/api/firewall/filter/apply').mock(return_value=httpx.Response(200, json={'status':'OK\n'}))
    pushed = await client.post(f"/api/rules/{r['id']}/deploy")
    assert pushed.json()['deployments'][0]['status'] == 'pending'


async def test_a_group_that_targets_a_vpn_object_cannot_be_deleted(client, respx_mock):
    """B15: the VPN link tables cascade, so the delete used to succeed and the VPN silently lost its targets."""
    from tests.test_ipsec import _tunnel_payload

    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw-vpn", "https://fw-vpn.test")
    target = await group(client, [host["id"]], name="VPN-Ziel")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([], target_group_ids=[target["id"]]))
    assert resp.status_code == 201, resp.text
    refused = await client.delete(f"/api/groups/{target['id']}")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "GRUPPE_WIRD_NOCH_VON_VPN_VERWENDET"
    assert (await client.delete(f"/api/ipsec/tunnels/{resp.json()['id']}")).status_code == 204
    assert (await client.delete(f"/api/groups/{target['id']}")).status_code == 204
