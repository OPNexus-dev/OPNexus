import httpx
import pytest
from tests.test_rules import _create_host
from tests.helpers import delete_host

@pytest.mark.asyncio
@pytest.mark.parametrize('kind,remote,fields',[('port-forward','d_nat',{'target':'192.0.2.20'}),('one-to-one','one_to_one',{'external':'192.0.2.20'}),('outbound-nat','source_nat',{'target':'192.0.2.20'})])
async def test_nat_groups_and_deletion_guards(client,respx_mock,outbound_mode_hybrid,kind,remote,fields):
    respx_mock.get(url__regex=r'.*/getVipStatus').respond(200,json={'rows':[]})
    h=await _create_host(client,'fw1','https://fw1.test')
    g=(await client.post('/api/groups',json={'name':'NAT group','description':'','host_ids':[h['id']]})).json()
    r=(await client.post('/api/'+kind,json={'description':'NAT group rule','interface':'wan','target_group_ids':[g['id']],**fields})).json()
    assert r['effective_host_ids']==[h['id']]
    assert (await client.delete('/api/groups/'+g['id'])).status_code==409
    respx_mock.post(f'https://fw1.test/api/firewall/{remote}/addRule').respond(200,json={'result':'saved','uuid':'nat1'})
    respx_mock.post(f'https://fw1.test/api/firewall/{remote}/apply').respond(200,json={'status':'ok'})
    assert (await client.post(f"/api/{kind}/{r['id']}/deploy")).status_code==200
    assert (await delete_host(client, h['id'])).status_code==409
    impact=(await client.post(f"/api/groups/{g['id']}/preview",json={'name':'NAT group','description':'','host_ids':[]})).json()
    assert impact['nat'][0]['retained_host_ids']==[h['id']]
    assert impact['nat'][0]['kind']==kind.replace('-','_')
    assert (await client.post(f"/api/{kind}/{r['id']}/confirm")).status_code==200
    assert (await client.put(f"/api/groups/{g['id']}",json={'name':'NAT group','description':'','host_ids':[],'preview_token':impact['token']})).status_code==200
    current=(await client.get('/api/'+kind)).json()[0]
    assert current['workflow_state']=='cleanup'
    respx_mock.post(f'https://fw1.test/api/firewall/{remote}/delRule/nat1').respond(200,json={'result':'deleted'})
    p=(await client.get(f"/api/{kind}/{r['id']}/preview")).json()
    assert (await client.post(f"/api/{kind}/{r['id']}/deployments/{h['id']}/remove",json={'preview_token':p['token']})).status_code==204
    assert (await delete_host(client, h['id'])).status_code==204

@pytest.mark.asyncio
async def test_pf_disabled_polarity_and_readonly_automatic_rows(client,respx_mock):
    import json
    from tests.test_port_forward import _pf_payload,_mock_pf_push
    respx_mock.get(url__regex=r'.*/getVipStatus').respond(200,json={'rows':[]})
    h=await _create_host(client,'fw1','https://fw1.test')
    r=(await client.post('/api/port-forward',json=_pf_payload([h['id']],enabled=False))).json()
    _mock_pf_push(respx_mock,'https://fw1.test')
    await client.post(f"/api/port-forward/{r['id']}/deploy")
    sent=json.loads(next(c.request.content for c in respx_mock.calls if c.request.url.path.endswith('/addRule')))['rule']
    assert sent['disabled']=='1' and sent['pass']==''
    respx_mock.get('https://fw1.test/api/firewall/d_nat/searchRule',params={'rowCount':-1}).respond(200,json={'rows':[{'uuid':'automatic_wan_1','is_automatic':'1'},{'uuid':'lockout_1'},{'uuid':'manual1','is_automatic':'0'}]})
    live=(await client.get(f"/api/port-forward/hosts/{h['id']}/live")).json()
    assert len(live['rules'])==1 and len(live['automatic_rules'])==2
