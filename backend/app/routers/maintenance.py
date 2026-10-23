import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models import OpnsenseHost
from app.models.host_operation import HostOperation
from app.services import opnsense_client as client, audit_service, backup_store, carp_maintenance_window_service, snapshot_service
from app.services.host_maintenance_service import inspect_host, inspect_carp_switch, fingerprint, peers, ACTIVE
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/hosts/{host_id}/maintenance", tags=["host-maintenance"])

class Preview(BaseModel):
    action: Literal['reboot','update','major_update','firmware_rollback']

class Execute(BaseModel):
    preview_id: uuid.UUID
    confirm_name: str = Field(min_length=1, max_length=120)
    acknowledge_downtime: Literal[True]
    # carp_enter only: optional auto-revert timer (see
    # carp_maintenance_window_service) — ignored for every other action.
    auto_revert_minutes: int | None = Field(default=None, ge=1, le=1440)

class Acknowledge(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=120)

class Cancel(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=500)

async def host_or_404(db, host_id):
    h=await db.get(OpnsenseHost,host_id)
    if not h: raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return h

def read_operation(op):
    return {"id":op.id,"host_id":op.host_id,"action":op.action,"status":op.status,"created_at":op.created_at,"expires_at":op.expires_at,"actor":op.actor,"message":op.message}

async def snapshot_or_502(db, host, action=None):
    try:
        state=await inspect_host(db,host)
        if action=='firmware_rollback':
            # Read only for this action, so every other preview/status keeps its exact set of firewall calls.
            rollback=await snapshot_service.rollback_state(host)
            rollback['changes_since']=await _changes_since(db,host,rollback.get('target'))
            state['rollback']=rollback
            state['actions']['firmware_rollback']=state['actions']['reboot']+(
                [] if rollback['supported'] else ['Die Firewall hat keine ZFS-Snapshots (Boot-Environments).'])+(
                [] if rollback.get('target') else ['Kein Rückweg-Punkt vorhanden: OPNexus legt ihn vor jedem Firmware-Update an.'])
        return state
    except client.OpnsenseAPIError as exc: raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc


async def _changes_since(db, host, target):
    """OPNexus' own pushes to this host after the rollback point: going back removes them from the firewall."""
    if not target:
        return []
    since=_point_time(target)
    if since is None:
        return []
    out=[]
    for kind,model in _DEPLOYMENT_MODELS():
        rows=(await db.scalars(select(model).where(model.host_id==host.id, model.deployed_at>since))).all()
        out+=[{'kind':kind,'status':r.status,'deployed_at':r.deployed_at.isoformat()} for r in rows]
    return sorted(out,key=lambda r:r['deployed_at'])[:50]


def _point_time(target):
    try:
        return datetime.strptime(target['stamp'],'%Y%m%d-%H%M%S').replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None


def _DEPLOYMENT_MODELS():
    from app.models import (AliasDeployment, OneToOneDeployment, OutboundNatDeployment, PolicyHierarchyRuleDeployment,
                            PortForwardDeployment, RuleDeployment)
    from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
    from app.models.openvpn_instance_deployment import OpenVpnInstanceDeployment
    from app.models.wireguard_tunnel_deployment import WireguardTunnelDeployment
    return (('rule',RuleDeployment),('alias',AliasDeployment),('port_forward',PortForwardDeployment),
            ('one_to_one',OneToOneDeployment),('outbound_nat',OutboundNatDeployment),('policy_hierarchy',PolicyHierarchyRuleDeployment),
            ('ipsec_tunnel',IpsecTunnelDeployment),('openvpn_instance',OpenVpnInstanceDeployment),('wireguard_tunnel',WireguardTunnelDeployment))


async def _mark_rolled_back(db, host, target):
    """After going back, what OPNexus pushed since the point is no longer on the firewall. A redeploy with an unchanged
    payload only re-affirms (writes nothing), so the remembered payload is cleared and the row marked: the next deploy
    really writes, and the UI shows the object as not in sync."""
    since=_point_time(target)
    if since is None:
        return 0
    count=0
    note=f"Firmware-Rückweg auf {target['name']}: Stand auf der Firewall unklar. Live-Stand prüfen und neu ausrollen."
    for _kind,model in _DEPLOYMENT_MODELS():
        for row in (await db.scalars(select(model).where(model.host_id==host.id, model.deployed_at>since))).all():
            row.status='error'; row.message=note[:500]; row.last_payload=None
            if hasattr(model,'revert_deadline'): row.revert_deadline=None
            count+=1
    return count

async def carp_snapshot_or_502(db, host):
    try: return await inspect_carp_switch(db,host)
    except client.OpnsenseAPIError as exc: raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc

@router.get('')
async def status(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host=await host_or_404(db,host_id)
    operations=(await db.scalars(select(HostOperation).where(HostOperation.host_id==host.id, HostOperation.status!='preview').order_by(HostOperation.created_at.desc()).limit(20))).all()
    # Keep operation records visible while the firewall is rebooting/offline.
    try: state=await inspect_host(db,host); error=None
    except client.OpnsenseAPIError as exc: state=None; error=str(exc)
    return {"state":state,"error":error,"operations":[read_operation(o) for o in operations]}

@router.post('/preview')
async def preview(host_id: uuid.UUID, payload: Preview, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host=await host_or_404(db,host_id)
    state=await snapshot_or_502(db,host,payload.action)
    if state['actions'][payload.action]: raise api_error(409, ErrorCode.ACTION_NOT_ALLOWED, reason=' '.join(state['actions'][payload.action]))
    op=HostOperation(host_id=host.id,host_name=host.name,actor=request.state.user.username,action=payload.action,
                     expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),fingerprint=fingerprint(state),snapshot=state)
    db.add(op);await db.commit()
    return {"preview_id":op.id,"expires_at":op.expires_at,"action":op.action,"state":state}

@router.post('/execute')
async def execute(host_id: uuid.UUID, payload: Execute, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host=await host_or_404(db,host_id)
    op=await db.get(HostOperation,payload.preview_id)
    if not op or op.host_id!=host_id or op.actor!=request.state.user.username: raise api_error(404, ErrorCode.WARTUNGSVORSCHAU_NICHT_GEFUNDEN)
    if op.status!='preview' or op.expires_at <= datetime.now(timezone.utc): raise api_error(409, ErrorCode.VORSCHAU_ABGELAUFEN_ODER_BEREITS_VERWENDET)
    if payload.confirm_name != host.name: raise api_error(409, ErrorCode.HOSTNAMEN_EXAKT_EINGEBEN)
    if op.action not in ('reboot','update','major_update','firmware_rollback'): raise api_error(404, ErrorCode.WARTUNGSVORSCHAU_NICHT_GEFUNDEN)
    state=await snapshot_or_502(db,host,op.action)
    if state['actions'][op.action]: raise api_error(409, ErrorCode.ACTION_NOT_ALLOWED, reason=' '.join(state['actions'][op.action]))
    if fingerprint(state)!=op.fingerprint: raise api_error(409, ErrorCode.HOSTZUSTAND_WURDE_VERAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    # Consume durably BEFORE the remote request. A timeout is ambiguous, and
    # replaying a reboot/update automatically could interrupt recovery.
    op.status='starting'; op.started_at=datetime.now(timezone.utc); await db.commit()
    await audit_service.persist(kind='host',action=op.action,status='requested',host_id=str(host.id),host_name=host.name,entity_id=str(op.id),details={'snapshot':state})
    if op.action in ('update','major_update'):
        # Way back first (1.32, B5): a boot environment of the running system. Fail closed: no point, no update.
        try:
            point=await snapshot_service.create_rollback_point(host,state.get('version'))
        except client.OpnsenseAPIError as exc:
            op.status='cancelled';op.message='Rückweg-Punkt (Boot-Environment) nicht angelegt: '+str(exc)[:500]+'. Update nicht gestartet.'
            await db.commit()
            await audit_service.persist(kind='host',action=op.action,status='cancelled',host_id=str(host.id),host_name=host.name,entity_id=str(op.id),message=op.message,details={})
            return read_operation(op)
        op.snapshot={**op.snapshot,'rollback_point':point}
        await db.commit()
    if op.action=='firmware_rollback':
        # Going back discards every change on the firewall since the point: keep a copy of the current config first.
        # Fail closed, like the backup before an update.
        try:
            backup_store.save_snapshot(host.id, await client.download_backup(host))
        except client.OpnsenseAPIError as exc:
            op.status='cancelled';op.message='Sicherung der aktuellen Konfiguration fehlgeschlagen: '+str(exc)[:500]+'. Rückweg nicht gestartet.'
            await db.commit()
            await audit_service.persist(kind='host',action=op.action,status='cancelled',host_id=str(host.id),host_name=host.name,entity_id=str(op.id),message=op.message,details={})
            return read_operation(op)
        rollback=state['rollback']
        action_fn=lambda h: snapshot_service.activate_and_reboot(h,rollback)
        done=(f"Boot-Environment {rollback['target']['name']} aktiviert, Neustart übergeben. Nach dem Start Version prüfen "
              f"(erwartet {rollback['target']['version']}) und Wartung abschließen. Alles, was seit dem Rückweg-Punkt auf der "
              "Firewall geändert wurde, ist danach nicht mehr vorhanden: zentrale Objekte über die Vorschau neu ausrollen.")
    else:
        action_fn={'reboot':client.reboot_host,'update':client.update_firmware,'major_update':client.upgrade_firmware}[op.action]
        done='An Firewall übergeben. Abschluss und Erreichbarkeit prüfen; anschließend Wartung abschließen.'
        if op.action in ('update','major_update') and not (op.snapshot or {}).get('rollback_point'):
            done+=' Kein Rückweg-Punkt: die Firewall hat keine ZFS-Snapshots.'
    try:
        result=await action_fn(host)
        op.status='submitted';op.message=done
        if op.action=='firmware_rollback':
            marked=await _mark_rolled_back(db,host,rollback['target'])
            if marked:
                op.message+=f' {marked} Deployment(s) als unklar markiert.'
    except client.OpnsenseAPIError as exc:
        op.status='unknown';op.message='Ausgang unklar: '+str(exc)[:800]+'. Nicht automatisch wiederholen.'
        result={}
    await db.commit()
    await audit_service.persist(kind='host',action=op.action,status=op.status,host_id=str(host.id),host_name=host.name,entity_id=str(op.id),message=op.message,details={'response':result})
    return read_operation(op)

@router.post('/check-updates', status_code=202)
async def check_updates(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    from app.services.host_maintenance_service import ensure_no_maintenance
    await policy_lock(db)
    host=await host_or_404(db,host_id)
    if not host.enabled: raise api_error(409, ErrorCode.HOST_IST_DEAKTIVIERT)
    await ensure_no_maintenance(db,[host])
    try:
        running=await client.get_firmware_running(host)
        if running.get('status')!='ready': raise api_error(409, ErrorCode.FIRMWARE_AUFTRAG_LAEUFT_BEREITS_ODER_ZUSTAND_IST_UNBEKANNT)
        return await client.check_firmware_updates(host)
    except client.OpnsenseAPIError as exc: raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc

@router.get('/progress')
async def progress(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host=await host_or_404(db,host_id)
    try: return await client.get_firmware_update_progress(host)
    except client.OpnsenseAPIError as exc: raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc

@router.post('/carp-preview')
async def carp_preview(host_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host=await host_or_404(db,host_id)
    state=await carp_snapshot_or_502(db,host)
    if state['actions']['carp']: raise api_error(409, ErrorCode.ACTION_NOT_ALLOWED, reason=' '.join(state['actions']['carp']))
    op=HostOperation(host_id=host.id,host_name=host.name,actor=request.state.user.username,action=state['action'],
                     expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),fingerprint=fingerprint(state),snapshot=state)
    db.add(op);await db.commit()
    return {"preview_id":op.id,"expires_at":op.expires_at,"action":op.action,"state":state}

@router.post('/carp-execute')
async def carp_execute(host_id: uuid.UUID, payload: Execute, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host=await host_or_404(db,host_id)
    op=await db.get(HostOperation,payload.preview_id)
    if not op or op.host_id!=host_id or op.actor!=request.state.user.username or op.action not in ('carp_enter','carp_leave'):
        raise api_error(404, ErrorCode.CARP_VORSCHAU_NICHT_GEFUNDEN)
    if op.status!='preview' or op.expires_at <= datetime.now(timezone.utc): raise api_error(409, ErrorCode.VORSCHAU_ABGELAUFEN_ODER_BEREITS_VERWENDET)
    if payload.confirm_name != host.name: raise api_error(409, ErrorCode.HOSTNAMEN_EXAKT_EINGEBEN)
    state=await carp_snapshot_or_502(db,host)
    if state['actions']['carp']: raise api_error(409, ErrorCode.ACTION_NOT_ALLOWED, reason=' '.join(state['actions']['carp']))
    if state['action']!=op.action or fingerprint(state)!=op.fingerprint: raise api_error(409, ErrorCode.CARP_ZUSTAND_WURDE_VERAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    # The OPNsense endpoint is a toggle. Consume the preview before calling it
    # and never replay after a timeout, otherwise a retry could undo the switch.
    op.status='starting';op.started_at=datetime.now(timezone.utc);await db.commit()
    await audit_service.persist(kind='host',action=op.action,status='requested',host_id=str(host.id),host_name=host.name,entity_id=str(op.id),details={'snapshot':state})
    try:
        result=await client.toggle_carp_maintenance(host)
        expected='enter_maintenance' if op.action=='carp_enter' else 'leave_maintenance'
        if result.get('action')!=expected:
            raise client.OpnsenseAPIError('CARP-Antwort passt nicht zur angeforderten Richtung')
        op.status='submitted'
        op.message=('CARP-Übergabe angefordert. Prüfen, dass der Partner MASTER und dieser Host BACKUP ist.' if op.action=='carp_enter'
                    else 'Normalen CARP-Betrieb angefordert. Rollen und Erreichbarkeit beider Hosts prüfen.')
        if op.action=='carp_enter':
            await carp_maintenance_window_service.open_window(db,host,payload.auto_revert_minutes)
        else:
            await carp_maintenance_window_service.close_windows_for_manual_leave(db,host)
    except client.OpnsenseAPIError as exc:
        op.status='unknown';op.message='CARP-Ausgang unklar: '+str(exc)[:760]+'. Nicht erneut klicken; Rollen zuerst prüfen.';result={}
    await db.commit()
    await audit_service.persist(kind='host',action=op.action,status=op.status,host_id=str(host.id),host_name=host.name,entity_id=str(op.id),message=op.message,details={'response':result})
    return read_operation(op)

@router.post('/{operation_id}/acknowledge')
async def acknowledge(host_id: uuid.UUID, operation_id: uuid.UUID, payload: Acknowledge, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host=await host_or_404(db,host_id)
    op=await db.get(HostOperation,operation_id)
    if not op or op.host_id!=host.id: raise api_error(404, ErrorCode.WARTUNGSAKTION_NICHT_GEFUNDEN)
    if payload.confirm_name!=host.name: raise api_error(409, ErrorCode.HOSTNAMEN_EXAKT_EINGEBEN)
    if op.status not in ACTIVE: raise api_error(409, ErrorCode.KEINE_OFFENE_WARTUNGSAKTION)
    minimum_wait=5 if op.action in ('carp_enter','carp_leave') else 30
    if op.started_at and (datetime.now(timezone.utc)-op.started_at).total_seconds() < minimum_wait:
        raise api_error(409, ErrorCode.MAINTENANCE_WAIT_REQUIRED, seconds=minimum_wait)
    if op.action in ('carp_enter','carp_leave'):
        state=await carp_snapshot_or_502(db,host)
        current=next((p for p in state['peers'] if p['id']==str(host.id)),{})
        others=[p for p in state['peers'] if p['id']!=str(host.id)]
        healthy_roles=any(p['status']=='online' and p['carp_status']=='MASTER' for p in state['peers']) and any(p['status']=='online' and p['carp_status']=='BACKUP' for p in state['peers'])
        if op.action=='carp_enter' and not (state['carp_maintenance_mode'] and current.get('carp_status')=='BACKUP' and any(p['status']=='online' and p['carp_status']=='MASTER' for p in others)):
            raise api_error(409, ErrorCode.ROLLENWECHSEL_NOCH_NICHT_BESTAETIGT_DIESER_HOST_MUSS_IM_4676309B)
        if op.action=='carp_leave' and not (not state['carp_maintenance_mode'] and healthy_roles):
            raise api_error(409, ErrorCode.NORMALBETRIEB_NOCH_NICHT_BESTAETIGT_WARTUNGSMODUS_MUSS_AUS_UND_8AF9290B)
    else:
        state=await snapshot_or_502(db,host)
        if state['running']!='ready' or not any(p['id']==str(host.id) and p['status']=='online' for p in state['peers']):
            raise api_error(409, ErrorCode.HOST_MUSS_ERREICHBAR_SEIN_UND_FIRMWARE_AUFTRAG_BEENDET_BCE1390B)
    op.status='acknowledged';op.message='Abschluss manuell geprüft und bestätigt.'
    await db.commit()
    details={'observed_roles':state['peers']} if op.action in ('carp_enter','carp_leave') else {'observed_version':state['version']}
    await audit_service.persist(kind='host',action=op.action,status='acknowledged',host_id=str(host.id),host_name=host.name,entity_id=str(op.id),message=op.message,details=details)
    return read_operation(op)

@router.post('/{operation_id}/cancel')
async def cancel(host_id: uuid.UUID, operation_id: uuid.UUID, payload: Cancel, db: AsyncSession = Depends(get_db)):
    """Escape hatch for an operation whose claimed target state never became
    true (e.g. OPNsense's CARP maintenance toggle accepted the request but the
    role switch itself never completed). Unlike acknowledge, this does NOT
    assert success — it only closes the record so the host/cluster stops being
    blocked, and requires a human-written reason for the audit trail."""
    await policy_lock(db)
    host=await host_or_404(db,host_id)
    op=await db.get(HostOperation,operation_id)
    if not op or op.host_id!=host.id: raise api_error(404, ErrorCode.WARTUNGSAKTION_NICHT_GEFUNDEN)
    if payload.confirm_name!=host.name: raise api_error(409, ErrorCode.HOSTNAMEN_EXAKT_EINGEBEN)
    if op.status not in ACTIVE: raise api_error(409, ErrorCode.KEINE_OFFENE_WARTUNGSAKTION)
    minimum_wait=5 if op.action in ('carp_enter','carp_leave') else 30
    if op.started_at and (datetime.now(timezone.utc)-op.started_at).total_seconds() < minimum_wait:
        raise api_error(409, ErrorCode.MAINTENANCE_WAIT_REQUIRED, seconds=minimum_wait)
    op.status='cancelled';op.message='Manuell abgebrochen, kein bestätigter Abschluss. Grund: '+payload.reason.strip()[:400]
    await db.commit()
    await audit_service.persist(kind='host',action=op.action,status='cancelled',host_id=str(host.id),host_name=host.name,entity_id=str(op.id),message=op.message,details={'reason':payload.reason})
    return read_operation(op)
