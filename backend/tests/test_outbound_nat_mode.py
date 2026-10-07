import httpx
import pytest
from app.services.outbound_nat_mode import parse_mode
from tests.test_rules import _create_host
from tests.test_outbound_nat import _nat_payload

@pytest.mark.parametrize("shape", [
    {"filter": {"general": {"snat_mode": {"hybrid": {"selected": 1}, "automatic": {"selected": 0}}}}},
    {"filter": {"settings": {"nat": {"snat_mode": "hybrid"}}}},
])
def test_mode_release_shapes(shape):
    assert parse_mode(shape) == "hybrid"

@pytest.mark.parametrize("mode", ["automatic", "disabled", "unknown", None, {"hybrid": {"selected": 1}, "advanced": {"selected": 1}}])
@pytest.mark.asyncio
async def test_block_entire_fleet_before_writing(client, respx_mock, mode):
    respx_mock.get(url__regex=r".*/getVipStatus").respond(200, json={"rows": []})
    a = await _create_host(client, "a", "https://a.test")
    b = await _create_host(client, "b", "https://b.test")
    for host, value in [(a, "hybrid"), (b, mode)]:
        respx_mock.get(f"{host['base_url']}/api/firewall/source_nat/get").respond(200, json={"filter": {"general": {"snat_mode": value}}})
    r=(await client.post('/api/outbound-nat', json=_nat_payload([a['id'],b['id']]))).json()
    response=await client.post(f"/api/outbound-nat/{r['id']}/deploy")
    assert response.status_code == 409
    assert not any(c.request.method == 'POST' for c in respx_mock.calls)

@pytest.mark.asyncio
async def test_unreachable_mode_blocks_before_mutation(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").respond(200,json={"rows": []})
    h=await _create_host(client,'a','https://a.test')
    respx_mock.get('https://a.test/api/firewall/source_nat/get').mock(side_effect=httpx.ConnectError('offline'))
    r=(await client.post('/api/outbound-nat',json=_nat_payload([h['id']]))).json()
    assert (await client.post(f"/api/outbound-nat/{r['id']}/deploy")).status_code==502
    assert not any(c.request.method == 'POST' for c in respx_mock.calls)

@pytest.mark.asyncio
async def test_mode_change_mid_fleet_preserves_earlier_rollback_deadline(client,respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").respond(200,json={"rows":[]})
    hosts=[await _create_host(client,n,f'https://{n}.test') for n in ('a','b')]
    hosts.sort(key=lambda h:(h['name'],h['id']))
    first,last=hosts
    for h in hosts:
        url=h['base_url']
        respx_mock.get(f'{url}/api/firewall/filter/getRule').respond(200,json={'rule':{'interface':{'wan':{'value':'WAN'}}}})
        respx_mock.get(f'{url}/api/firewall/alias/searchItem').respond(200,json={'rows':[]})
    def reply(mode):return httpx.Response(200,json={'filter':{'general':{'snat_mode':mode}}})
    respx_mock.get(first['base_url']+'/api/firewall/source_nat/get').mock(return_value=reply('hybrid'))
    respx_mock.get(last['base_url']+'/api/firewall/source_nat/get').mock(side_effect=[reply('hybrid'),reply('automatic')])
    respx_mock.post(first['base_url']+'/api/firewall/source_nat/addRule').respond(200,json={'result':'saved','uuid':'first-nat'})
    respx_mock.post(first['base_url']+'/api/firewall/source_nat/apply').respond(200,json={'status':'ok'})
    r=(await client.post('/api/outbound-nat',json=_nat_payload([h['id'] for h in hosts]))).json()
    response=await client.post(f"/api/outbound-nat/{r['id']}/deploy")
    assert response.status_code==409,response.text
    current=(await client.get('/api/outbound-nat')).json()[0]
    assert current['deployments'][0]['status']=='pending'
    assert current['deployments'][0]['revert_deadline']
    assert len(current['deployments'])==1
