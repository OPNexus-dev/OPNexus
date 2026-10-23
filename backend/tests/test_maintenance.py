import uuid
from datetime import datetime,timedelta,timezone
import httpx
import pytest
from sqlalchemy import select
from app.database import SessionLocal
from app.models import HostOperation
from app.services import backup_store
from tests.test_rules import _create_host


def mock_reads(mock,base='https://fw1.test',carp=None,status='none',running='ready',version='26.7.3'):
    mock.get(f'{base}/api/diagnostics/interface/getVipStatus').respond(200,json={'rows':[{'status':carp}] if carp else []})
    mock.get(f'{base}/api/core/firmware/info').respond(200,json={'product_version':version})
    mock.get(f'{base}/api/core/firmware/status').respond(200,json={'status':status,'all_packages':{},'status_msg':'cached result'})
    mock.get(f'{base}/api/core/firmware/running').respond(200,json={'status':running})
    mock.get(f'{base}/api/core/snapshots/isSupported').respond(200,json={'supported':False})  # UFS: no rollback point (1.32)

async def preview(client,h,action='reboot'):
    return await client.post(f"/api/hosts/{h['id']}/maintenance/preview",json={'action':action})

async def execute(client,h,p,**kwargs):
    return await client.post(f"/api/hosts/{h['id']}/maintenance/execute",json={'preview_id':p['preview_id'],'confirm_name':h['name'],'acknowledge_downtime':True,**kwargs})

@pytest.mark.asyncio
async def test_reboot_single_use_and_explicit_completion(client,respx_mock):
    mock_reads(respx_mock)
    h=await _create_host(client,'fw1','https://fw1.test')
    p=await preview(client,h);assert p.status_code==200,p.text
    reboot=respx_mock.post('https://fw1.test/api/core/system/reboot').respond(200,json={'status':'ok'})
    r=await execute(client,h,p.json());assert r.status_code==200,r.text
    assert r.json()['status']=='submitted'
    assert (await execute(client,h,p.json())).status_code==409
    assert reboot.call_count==1
    # A running/unknown operation prevents a second preview as well.
    assert (await preview(client,h)).status_code==409
    op_id=r.json()['id']
    async with SessionLocal() as db:
        op=await db.get(HostOperation,uuid.UUID(op_id));op.started_at=datetime.now(timezone.utc)-timedelta(minutes=1);await db.commit()
    done=await client.post(f"/api/hosts/{h['id']}/maintenance/{op_id}/acknowledge",json={'confirm_name':'fw1'})
    assert done.status_code==200 and done.json()['status']=='acknowledged'
    assert (await preview(client,h)).status_code==200

@pytest.mark.asyncio
async def test_update_requires_backup_and_sends_only_stable_update(client,respx_mock):
    mock_reads(respx_mock,status='update')
    h=await _create_host(client,'fw1','https://fw1.test')
    assert (await preview(client,h,'update')).status_code==409
    backup_store.save_snapshot(uuid.UUID(h['id']),b'<opnsense/>')
    p=await preview(client,h,'update');assert p.status_code==200,p.text
    update=respx_mock.post('https://fw1.test/api/core/firmware/update').respond(200,json={'status':'ok','msg_uuid':'job1'})
    assert (await execute(client,h,p.json())).json()['status']=='submitted'
    assert update.call_count==1
    assert not any('upgrade' in str(c.request.url) for c in respx_mock.calls)

@pytest.mark.asyncio
async def test_major_upgrade_requires_backup_and_reported_upgrade_status(client,respx_mock):
    mock_reads(respx_mock,status='upgrade')
    h=await _create_host(client,'fw1','https://fw1.test')
    # Available major upgrade alone isn't enough - still needs a fresh backup,
    # same gate as the stable-update path.
    blocked=await preview(client,h,'major_update')
    assert blocked.status_code==409 and 'Konfigurationsbackup' in blocked.text
    backup_store.save_snapshot(uuid.UUID(h['id']),b'<opnsense/>')
    # A reported "upgrade" status means no stable "update" is pending, so the
    # ordinary update action must now be the one that's blocked.
    assert (await preview(client,h,'update')).status_code==409
    p=await preview(client,h,'major_update');assert p.status_code==200,p.text
    upgrade=respx_mock.post('https://fw1.test/api/core/firmware/upgrade').respond(200,json={'status':'ok','msg_uuid':'job1'})
    r=await execute(client,h,p.json());assert r.status_code==200,r.text
    assert r.json()['status']=='submitted'
    assert upgrade.call_count==1


@pytest.mark.asyncio
async def test_major_upgrade_blocked_while_stable_update_pending(client,respx_mock):
    mock_reads(respx_mock,status='update')
    h=await _create_host(client,'fw1','https://fw1.test')
    backup_store.save_snapshot(uuid.UUID(h['id']),b'<opnsense/>')
    response=await preview(client,h,'major_update')
    assert response.status_code==409 and 'Kein Major-Upgrade' in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['name','version','expired','acknowledgement'])
async def test_stale_or_unapproved_maintenance_does_not_write(client,respx_mock,change):
    mock_reads(respx_mock)
    h=await _create_host(client,'fw1','https://fw1.test')
    p=(await preview(client,h)).json()
    kwargs={}
    if change=='name':kwargs={'confirm_name':'wrong'}
    if change=='acknowledgement':kwargs={'acknowledge_downtime':False}
    if change=='version':mock_reads(respx_mock,version='26.7.4')
    if change=='expired':
        async with SessionLocal() as db:
            op=await db.get(HostOperation,uuid.UUID(p['preview_id']));op.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);await db.commit()
    assert (await execute(client,h,p,**kwargs)).status_code in (409,422)
    assert not any(c.request.method=='POST' for c in respx_mock.calls)

@pytest.mark.asyncio
async def test_timeout_is_unknown_and_not_replayed(client,respx_mock):
    mock_reads(respx_mock)
    h=await _create_host(client,'fw1','https://fw1.test');p=(await preview(client,h)).json()
    reboot=respx_mock.post('https://fw1.test/api/core/system/reboot').mock(side_effect=httpx.ReadTimeout('timeout'))
    r=await execute(client,h,p);assert r.json()['status']=='unknown'
    assert (await execute(client,h,p)).status_code==409 and reboot.call_count==1
    state=(await client.get(f"/api/hosts/{h['id']}/maintenance")).json()
    assert state['operations'][0]['status']=='unknown'

@pytest.mark.asyncio
@pytest.mark.parametrize('carp,running',[('MASTER','ready'),('BACKUP','running'),(None,'ready')])
async def test_cluster_and_busy_gates(client,respx_mock,carp,running):
    mock_reads(respx_mock,carp=carp,running=running)
    h=await _create_host(client,'fw1','https://fw1.test')
    await client.patch(f"/api/hosts/{h['id']}",json={'cluster_name':'ha'})
    assert (await preview(client,h)).status_code==409
    assert not any(c.request.method=='POST' for c in respx_mock.calls)

@pytest.mark.asyncio
async def test_pending_deploy_blocks_maintenance(client,respx_mock):
    from tests.test_port_forward import _pf_payload,_mock_pf_push
    mock_reads(respx_mock)
    h=await _create_host(client,'fw1','https://fw1.test')
    r=(await client.post('/api/port-forward',json=_pf_payload([h['id']]))).json()
    _mock_pf_push(respx_mock,'https://fw1.test')
    await client.post(f"/api/port-forward/{r['id']}/deploy")
    response=await preview(client,h)
    assert response.status_code==409 and 'Bestätigung' in response.text

@pytest.mark.asyncio
async def test_update_check_and_progress(client,respx_mock):
    mock_reads(respx_mock)
    h=await _create_host(client,'fw1','https://fw1.test')
    route=respx_mock.post('https://fw1.test/api/core/firmware/check').respond(200,json={'status':'ok','msg_uuid':'check1'})
    respx_mock.get('https://fw1.test/api/core/firmware/upgradestatus').respond(200,json={'status':'done','log':'***DONE***'})
    assert (await client.post(f"/api/hosts/{h['id']}/maintenance/check-updates")).status_code==202
    assert route.call_count==1
    assert (await client.get(f"/api/hosts/{h['id']}/maintenance/progress")).json()['status']=='done'


@pytest.mark.asyncio
async def test_carp_handover_and_return_are_preview_bound_and_verified(client,respx_mock):
    roles={
        'https://fw-primary.test': {'role':'MASTER','maintenance':False},
        'https://fw-secondary.test': {'role':'BACKUP','maintenance':False},
    }
    for base in roles:
        def vip_response(request, base=base):
            current=roles[base]
            return httpx.Response(200,json={'rows':[{'vhid':'1','status':current['role']}],
                # maintenancemode is a native JSON bool on real OPNsense hosts, unlike
                # the other carp.* flags below which are "1"/"0" strings — keep this a
                # real bool so a regression to the old str(x)=="1" bug fails the test.
                'carp':{'allow':'1','maintenancemode':current['maintenance'],'demotion':'240' if current['maintenance'] else '0','status_msg':''}})
        respx_mock.get(f'{base}/api/diagnostics/interface/getVipStatus').mock(side_effect=vip_response)
        respx_mock.get(f'{base}/api/core/firmware/running').respond(200,json={'status':'ready'})
    primary=await _create_host(client,'fw-primary','https://fw-primary.test')
    secondary=await _create_host(client,'fw-secondary','https://fw-secondary.test')
    await client.patch(f"/api/hosts/{primary['id']}",json={'cluster_name':'ha'})
    await client.patch(f"/api/hosts/{secondary['id']}",json={'cluster_name':'ha'})

    def toggle(request):
        current=roles['https://fw-primary.test']
        current['maintenance']=not current['maintenance']
        if current['maintenance']:
            current['role']='BACKUP';roles['https://fw-secondary.test']['role']='MASTER';action='enter_maintenance'
        else:
            current['role']='MASTER';roles['https://fw-secondary.test']['role']='BACKUP';action='leave_maintenance'
        return httpx.Response(200,json={'status':'ok','action':action})
    switch=respx_mock.post('https://fw-primary.test/api/diagnostics/interface/carp_status/maintenance').mock(side_effect=toggle)

    blocked=await client.post(f"/api/hosts/{secondary['id']}/maintenance/carp-preview")
    assert blocked.status_code==409 and 'MASTER' in blocked.text
    plan=(await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    assert plan['action']=='carp_enter'
    wrong=await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-execute",json={'preview_id':plan['preview_id'],'confirm_name':'wrong','acknowledge_downtime':True})
    assert wrong.status_code==409 and switch.call_count==0
    entered=await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-execute",json={'preview_id':plan['preview_id'],'confirm_name':'fw-primary','acknowledge_downtime':True})
    assert entered.status_code==200 and entered.json()['status']=='submitted' and switch.call_count==1
    async with SessionLocal() as db:
        op=await db.get(HostOperation,uuid.UUID(entered.json()['id']));op.started_at=datetime.now(timezone.utc)-timedelta(seconds=6);await db.commit()
    acknowledged=await client.post(f"/api/hosts/{primary['id']}/maintenance/{entered.json()['id']}/acknowledge",json={'confirm_name':'fw-primary'})
    assert acknowledged.status_code==200,acknowledged.text

    # fw-secondary is now a legitimate MASTER, so the plain role checks alone
    # would let its own "enter maintenance" through — but fw-primary is still
    # demoted, and demoting a second node on top of that is exactly the bug
    # that left both nodes stuck (twice, live). Must be blocked here too.
    double_demote=await client.post(f"/api/hosts/{secondary['id']}/maintenance/carp-preview")
    assert double_demote.status_code==409 and 'bereits im CARP-Wartungsmodus' in double_demote.text

    leave=(await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-preview")).json()
    assert leave['action']=='carp_leave' and leave['state']['carp_maintenance_mode'] is True
    restored=await client.post(f"/api/hosts/{primary['id']}/maintenance/carp-execute",json={'preview_id':leave['preview_id'],'confirm_name':'fw-primary','acknowledge_downtime':True})
    assert restored.status_code==200 and restored.json()['status']=='submitted' and switch.call_count==2
    async with SessionLocal() as db:
        op=await db.get(HostOperation,uuid.UUID(restored.json()['id']));op.started_at=datetime.now(timezone.utc)-timedelta(seconds=6);await db.commit()
    acknowledged=await client.post(f"/api/hosts/{primary['id']}/maintenance/{restored.json()['id']}/acknowledge",json={'confirm_name':'fw-primary'})
    assert acknowledged.status_code==200,acknowledged.text


@pytest.mark.asyncio
async def test_carp_timeout_is_unknown_and_toggle_is_not_replayed(client,respx_mock):
    for base,role in [('https://fw-a.test','MASTER'),('https://fw-b.test','BACKUP')]:
        respx_mock.get(f'{base}/api/diagnostics/interface/getVipStatus').respond(200,json={'rows':[{'vhid':'1','status':role}],'carp':{'allow':'1','maintenancemode':'0','demotion':'0'}})
        respx_mock.get(f'{base}/api/core/firmware/running').respond(200,json={'status':'ready'})
    a=await _create_host(client,'fw-a','https://fw-a.test');b=await _create_host(client,'fw-b','https://fw-b.test')
    await client.patch(f"/api/hosts/{a['id']}",json={'cluster_name':'ha'});await client.patch(f"/api/hosts/{b['id']}",json={'cluster_name':'ha'})
    route=respx_mock.post('https://fw-a.test/api/diagnostics/interface/carp_status/maintenance').mock(side_effect=httpx.ReadTimeout('timeout'))
    plan=(await client.post(f"/api/hosts/{a['id']}/maintenance/carp-preview")).json()
    first=await client.post(f"/api/hosts/{a['id']}/maintenance/carp-execute",json={'preview_id':plan['preview_id'],'confirm_name':'fw-a','acknowledge_downtime':True})
    assert first.json()['status']=='unknown'
    assert (await client.post(f"/api/hosts/{a['id']}/maintenance/carp-execute",json={'preview_id':plan['preview_id'],'confirm_name':'fw-a','acknowledge_downtime':True})).status_code==409
    assert route.call_count==1

    # A stuck operation blocks the whole cluster and can't be acknowledged
    # (its target state never became true) — cancel is the escape hatch.
    blocked=await client.post(f"/api/hosts/{b['id']}/maintenance/carp-preview")
    assert blocked.status_code==409 and 'Wartungsaktion' in blocked.text
    op_id=first.json()['id']
    wrong_name=await client.post(f"/api/hosts/{a['id']}/maintenance/{op_id}/cancel",json={'confirm_name':'wrong','reason':'toggle timed out and never converged'})
    assert wrong_name.status_code==409
    too_short_reason=await client.post(f"/api/hosts/{a['id']}/maintenance/{op_id}/cancel",json={'confirm_name':'fw-a','reason':'x'})
    assert too_short_reason.status_code==422
    async with SessionLocal() as db:
        op=await db.get(HostOperation,uuid.UUID(op_id));op.started_at=datetime.now(timezone.utc);await db.commit()
    too_early=await client.post(f"/api/hosts/{a['id']}/maintenance/{op_id}/cancel",json={'confirm_name':'fw-a','reason':'toggle timed out and never converged'})
    assert too_early.status_code == 409
    assert too_early.json()["detail"] == {"code": "MAINTENANCE_WAIT_REQUIRED", "params": {"seconds": 5}}
    async with SessionLocal() as db:
        op=await db.get(HostOperation,uuid.UUID(op_id));op.started_at=datetime.now(timezone.utc)-timedelta(seconds=6);await db.commit()
    cancelled=await client.post(f"/api/hosts/{a['id']}/maintenance/{op_id}/cancel",json={'confirm_name':'fw-a','reason':'toggle timed out and never converged'})
    assert cancelled.status_code==200 and cancelled.json()['status']=='cancelled',cancelled.text
    again=await client.post(f"/api/hosts/{a['id']}/maintenance/{op_id}/cancel",json={'confirm_name':'fw-a','reason':'toggle timed out and never converged'})
    assert again.status_code==409
    unblocked=await client.post(f"/api/hosts/{a['id']}/maintenance/carp-preview")
    assert unblocked.status_code==200,unblocked.text
