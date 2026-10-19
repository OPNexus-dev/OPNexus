import asyncio, copy, json, ssl, uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import get_db
from app.routers import auth, users, hosts
from app.services import auth_service, totp_service, host_status_service, opnsense_client, rule_deploy_service, ipsec_service
from app.security_middleware import _origin_allowed
from app.schemas.host import HostCreate

results=[]
def record(name, **details):
 results.append({'probe':name, **details})

class FakeDB:
 def __init__(self, user=None): self.user=user; self.executed=[]; self.commits=0
 async def get(self,*args,**kwargs): return self.user
 async def commit(self): self.commits+=1
 def add(self,obj): pass
 async def scalar(self,q): return None
 async def execute(self,q): self.executed.append(str(q)); return SimpleNamespace(scalar_one_or_none=lambda: self.user)
 async def __aenter__(self): return self
 async def __aexit__(self,*args): pass

async def main():
 user=SimpleNamespace(id=uuid.uuid4(),username='synthetic-reader',role='read_only')
 fake=FakeDB(user)
 async def dep(): yield fake
 app.dependency_overrides[get_db]=dep
 host_id=uuid.uuid4()
 with patch('app.auth_middleware.SessionLocal',return_value=fake),patch.object(auth_service,'resolve_session',AsyncMock(return_value=user)),patch.object(hosts,'_get_host_or_404',AsyncMock(return_value=SimpleNamespace(id=host_id))),patch.object(hosts.backup_store,'read_snapshot',return_value=b'<opnsense><synthetic-secret>MARKER</synthetic-secret></opnsense>'):
  async with AsyncClient(transport=ASGITransport(app=app),base_url='https://synthetic.test',cookies={auth.COOKIE_NAME:'synthetic-token'}) as c:
   r=await c.get(f'/api/hosts/{host_id}/backups/synthetic.xml')
   record('read_only_backup_download',status=r.status_code,synthetic_marker_returned='MARKER' in r.text)
   w=await c.post('/api/hosts',json={})
   record('read_only_write_control',status=w.status_code)
   user.role='admin'
   with patch('app.services.audit_service.persist',AsyncMock()):
    r=await c.post('/api/certificates/managed',json={'action':'internal','private_key':'SYNTHETIC-PRIVATE-KEY-MARKER'})
    record('validation_error_secret_echo',status=r.status_code,synthetic_marker_returned='SYNTHETIC-PRIVATE-KEY-MARKER' in r.text)
 app.dependency_overrides.clear()
 # No real user, session, database or secret is consulted below.
 target=SimpleNamespace(id=uuid.uuid4(),password_hash='synthetic-hash')
 db=FakeDB(target)
 with patch.object(auth_service,'hash_password',return_value='new-synthetic-hash'):
  await users.reset_password(target.id,users.UserPasswordReset(new_password='synthetic-password-123'),db)
 record('password_reset_revoke',sql_statements=db.executed,commits=db.commits)
 secret=totp_service.generate_secret(); code=totp_service.code_at(secret)
 barrier=asyncio.Event(); entered=0
 class ConcurrentDB(FakeDB):
  async def commit(self):
   nonlocal entered
   entered+=1
   if entered==2: barrier.set()
   await barrier.wait()
 u1=SimpleNamespace(id=uuid.uuid4(),username='synthetic-user',totp_last_step=0,totp_recovery_hashes=[])
 u2=copy.copy(u1)
 with patch.object(totp_service,'read_secret',return_value=secret):
  accepted=await asyncio.gather(auth._accept_second_factor(ConcurrentDB(),u1,code),auth._accept_second_factor(ConcurrentDB(),u2,code))
 record('totp_parallel_replay',accepted=accepted)
 code='synthetic-recovery'; recovery=totp_service.hash_recovery(code)
 u1.totp_recovery_hashes=[recovery];u2.totp_recovery_hashes=[recovery];entered=0;barrier=asyncio.Event()
 with patch.object(totp_service,'read_secret',return_value=None):
  accepted=await asyncio.gather(auth._accept_second_factor(ConcurrentDB(),u1,code),auth._accept_second_factor(ConcurrentDB(),u2,code))
 record('recovery_parallel_replay',accepted=accepted)
 with patch.object(host_status_service,'get_vip_status',AsyncMock(side_effect=opnsense_client.OpnsenseAPIError('401 synthetic upstream secret MARKER'))):
  status=await host_status_service._status_for(SimpleNamespace(enabled=True))
 record('status_error_disclosure',synthetic_marker_returned='MARKER' in status['status_detail'])
 ctx=ssl._create_stdlib_context()
 record('smtp_default_tls_context',verify_mode=int(ctx.verify_mode),check_hostname=ctx.check_hostname)
 record('cross_scheme_origin',accepted=_origin_allowed('http://synthetic.test','synthetic.test'))
 h=HostCreate(name='synthetic',base_url='http://synthetic.test',api_key='synthetic',api_secret='synthetic')
 record('plaintext_firewall_url',accepted=h.base_url.startswith('http://'),verify_tls=h.verify_tls)
 throttle=auth_service._LoginThrottle()
 for i in range(100): throttle.record_failure(f'synthetic-{i}',f'user-{i}')
 before=len(throttle._records)
 with patch('app.services.auth_service.time.monotonic',return_value=10**20):
  for i in range(100): throttle.retry_after(f'synthetic-{i}',f'user-{i}')
 record('expired_throttle_records',before=before,after=len(throttle._records),username_counters=len(throttle._failures_since_success))
 rule=SimpleNamespace(id=uuid.uuid4(),enabled=True,action='pass',interface='wan',direction='in',ipprotocol='inet',protocol='any',source_net='any',source_port=None,destination_net='any',destination_port=None,description='synthetic rule')
 with patch.object(opnsense_client,'add_rule',AsyncMock(return_value={'uuid':'synthetic-remote-uuid'})),patch.object(opnsense_client,'apply',AsyncMock(side_effect=opnsense_client.OpnsenseAPIError('synthetic timeout after apply'))):
  d=await rule_deploy_service._deploy_to_host(FakeDB(),rule,SimpleNamespace(id=uuid.uuid4()))
 record('ambiguous_apply_rollback',status=d.status,revert_deadline=d.revert_deadline,previous_payload=d.previous_payload,remote_uuid_known=bool(d.opnsense_rule_uuid))
 tunnel=SimpleNamespace(id=uuid.uuid4(),description='synthetic',local_address='192.0.2.1',remote_address='192.0.2.2',ike_version='ikev2',proposal='aes256-sha256-modp2048',local_identity='synthetic-local',remote_identity='synthetic-remote',local_networks='192.0.2.0/25',remote_networks='198.51.100.0/25',esp_proposal='aes256-sha256',enabled=True,pre_shared_key_set=True,targets=[],groups=[],deployments=[])
 tunnel.targets=[SimpleNamespace(id=uuid.uuid4(),name='synthetic-host',enabled=True,base_url='https://synthetic.test',verify_tls=True)]
 with patch('app.services.ipsec_psk_store.read_secret',return_value='synthetic-old-secret'):
  a=ipsec_service.preview(tunnel)
 with patch('app.services.ipsec_psk_store.read_secret',return_value='synthetic-new-secret'):
  b=ipsec_service.preview(tunnel)
 record('vpn_preview_secret_binding',same_token_after_secret_change=a['token']==b['token'])
 print(json.dumps(results,indent=2))

asyncio.run(main())
