import uuid
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from app.database import SessionLocal
from app.models.audit import AuditEvent
from tests.test_rules import _create_host
from tests.test_port_forward import _pf_payload, _mock_pf_push
from tests.helpers import delete_host

@pytest.mark.asyncio
async def test_audit_retains_payload_actor_after_rule_and_host_deletion(client,respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").respond(200,json={"rows":[]})
    h=await _create_host(client,'fw1','https://fw1.test')
    r=(await client.post('/api/port-forward',json=_pf_payload([h['id']]))).json()
    _mock_pf_push(respx_mock,'https://fw1.test')
    assert (await client.post(f"/api/port-forward/{r['id']}/deploy")).status_code==200
    assert (await client.post(f"/api/port-forward/{r['id']}/confirm")).status_code==200
    respx_mock.post('https://fw1.test/api/firewall/d_nat/delRule/pf-uuid-1').respond(200,json={'result':'deleted'})
    assert (await client.delete(f"/api/port-forward/{r['id']}")).status_code==204
    assert (await delete_host(client, h['id'])).status_code==204
    data=(await client.get('/api/audit',params={'entity_id':r['id'],'limit':200})).json()
    state=[e for e in data['items'] if e['host_id']==h['id']]
    assert {e['status'] for e in state} == {'pending','success','removed'}
    assert all(e['actor']=='admin' and e['host_name']=='fw1' for e in state)
    pushed=next(e for e in state if e['status']=='pending')
    assert pushed['details']['after_payload']['source']=={'network':'any','port':'','not':'0'}
    assert pushed['details']['after_payload']['pass']==''
    assert pushed['details']['remote_uuid']=='pf-uuid-1'
    assert any(e['status']=='requested' for e in data['items'])
    assert all('api_secret' not in str(e) for e in data['items'])

@pytest.mark.asyncio
async def test_audit_database_rejects_rewriting_and_api_is_read_only(client):
    # A rejected operation is also an auditable request.
    identifier=str(uuid.uuid4())
    assert (await client.post(f'/api/port-forward/{identifier}/deploy')).status_code==404
    rows=(await client.get('/api/audit',params={'entity_id':identifier})).json()['items']
    assert {e['status'] for e in rows}=={'requested','rejected'}
    async with SessionLocal() as db:
        with pytest.raises(DBAPIError,match='append-only'):
            await db.execute(text("UPDATE audit_events SET actor='changed'"))
        await db.rollback()
        with pytest.raises(DBAPIError,match='append-only'):
            await db.execute(text('DELETE FROM audit_events'))
        await db.rollback()
    assert (await client.delete('/api/audit')).status_code==405

@pytest.mark.asyncio
async def test_audit_filters_and_auth(client,anon_client):
    assert (await anon_client.get('/api/audit')).status_code==401
    await client.post(f'/api/one-to-one/{uuid.uuid4()}/deploy')
    page=(await client.get('/api/audit',params={'kind':'one_to_one','status':'rejected','limit':1})).json()
    assert page['total']==1 and len(page['items'])==1
    assert (await client.get('/api/audit',params={'limit':201})).status_code==422

@pytest.mark.asyncio
async def test_background_rollback_has_attempt_actor_and_correlation(client,respx_mock):
    from datetime import datetime,timedelta,timezone
    from app.models import PortForwardDeployment
    from app.services.port_forward_deploy_service import sweep_expired_port_forward_deployments
    respx_mock.get(url__regex=r'.*/getVipStatus').respond(200,json={'rows':[]})
    h=await _create_host(client,'fw1','https://fw1.test')
    r=(await client.post('/api/port-forward',json=_pf_payload([h['id']]))).json()
    _mock_pf_push(respx_mock,'https://fw1.test')
    await client.post(f"/api/port-forward/{r['id']}/deploy")
    respx_mock.post('https://fw1.test/api/firewall/d_nat/delRule/pf-uuid-1').respond(200,json={'result':'deleted'})
    async with SessionLocal() as db:
        d=await db.scalar(select(PortForwardDeployment));d.revert_deadline=datetime.now(timezone.utc)-timedelta(seconds=1);await db.commit()
        await sweep_expired_port_forward_deployments(db)
    rows=(await client.get('/api/audit',params={'entity_id':r['id'],'actor':'system:rollback'})).json()['items']
    removed=next(e for e in rows if e['status']=='removed')
    attempts=[e for e in rows if e['status']=='requested']
    assert len(attempts)==1
    assert attempts[0]['correlation_id']==removed['correlation_id']
