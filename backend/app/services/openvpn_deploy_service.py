"""Pushes a centrally-authored OpenVPN instance (v1 scope, see below) to
its target hosts, the same "OPNexus writes via the API instead of relying
on OPNsense's own HA config sync" model as ipsec_deploy_service.py /
rule_deploy_service.py.

VERIFIED FIELDS / ENDPOINTS (fetched directly from
raw.githubusercontent.com/opnsense/core/master/..., branch master,
2026-09-09 — downloaded the raw XML/PHP files directly rather than relying
on a summarized fetch, specifically to catch exact field names and the
exact reconfigure response shape without risking paraphrase error; see
opnsense_client.py's "OpenVPN" section for the endpoint list and its own
citations):

- OpenVPN.xml (mount `//OPNsense/OpenVPN`, version 1.0.1) ->
  Instances.Instance: vpnid (VPNIdField, Required, MinimumValue 1, NO
  <Default> — never sent by this app; OPNsense's own UI never prompts for
  one either, so its field-type presumably auto-assigns one the same way
  on addAction — VERIFIED LIVE 2026-10-09 on 26.7.5: it is NOT auto-assigned, add() fails
  with "instance.vpnid: A value is required." — see _next_vpnid),
  enabled (BooleanField, Default 1), dev_type (OptionValues
  tun/tap/ovpn(DCO), Default "tun" — never sent, relies on the model
  default exactly like IPsec relies on Child.mode defaulting to "tunnel"),
  proto (OptionValues udp/udp4/udp6/tcp/tcp4/tcp6, Default "udp" — v1
  exposes only "udp"/"tcp"), port (PortField), local (NetworkField, no
  netmask — server-role bind address), topology (Default "subnet", never
  sent), remote (RemoteHostField — client-role peer), role (OptionValues
  ONLY "client"/"server", Default "server" — see the scope-decision
  section below, this is the key finding that reshaped v1), server
  (NetworkField, NetMaskRequired Y — server-role tunnel network), cert /
  ca (CertificateField, i.e. ModelRelationField into OPNsense's own
  Trust\\Cert store; values are the Trust-Store REFIDs, not row UUIDs —
  verified live 2026-10-09), tls_key (ModelRelationField -> this SAME model's
  StaticKeys.StaticKey, display "description,mode"), auth/data-ciphers
  (cipher suite OptionFields — v1 always omits these, relying on
  OPNsense's own defaults, exactly like IPsec's proposals default to
  "default"), description (DescriptionField).
- OpenVPN.xml -> StaticKeys.StaticKey (separate top-level ArrayField, NOT
  nested under Instance): mode (OptionValues auth/crypt/crypt-v2, Default
  "crypt"), key (TextField, Required, masked to the base64-ish charset
  OpenVPN static keys use), description (DescriptionField, Required).
  Referenced from an Instance by UUID via `tls_key` — this app creates/
  updates the StaticKey FIRST, then writes that UUID into the Instance's
  `tls_key` field, mirroring exactly how IPsec's Local/Remote/Child
  reference their parent Connection's UUID.
- InstancesController.php (module "openvpn", controller "instances",
  `class InstancesController extends ApiMutableModelControllerBase`,
  internalModelName 'instance', internalModelClass
  'OPNsense\\OpenVPN\\OpenVPN'): searchAction/getAction/addAction/
  setAction/delAction/toggleAction (Instances.Instance) and
  searchStaticKeyAction/getStaticKeyAction/addStaticKeyAction/
  setStaticKeyAction/delStaticKeyAction (StaticKeys.StaticKey,
  internalModelName 'statickey') — all confirmed as literal PHP method
  names read directly from the controller source. genKeyAction (backend
  key generation) exists but is deliberately unused — see
  opnsense_client.py.
- ServiceController.php (module "openvpn", controller "service"):
  reconfigureAction's exact body, read directly from source:
  `(new Backend())->configdRun('openvpn configure');
   $this->runInterfaceRegistration(); return ['result' => 'ok'];` — a
  hand-rolled ApiControllerBase method, NOT inherited from
  ApiMutableServiceControllerBase like IPsec's reconfigureAction is. This
  matters concretely: its success response key is "result", NOT "status"
  like IPsec's ({"status": "ok"}) — checked explicitly rather than assumed
  symmetric, per the plan's own warning not to assume this. Also present
  in this controller but unused by this app: startServiceAction/
  stopServiceAction/restartServiceAction (per-instance start/stop/restart
  via `configdpRun('service {start,stop,restart}', ['openvpn', $id])`) —
  this app always uses the global reconfigureAction as its one apply step,
  the same choice IPsec made.
- Router.php::parsePath() URL-segment convention: identical to the one
  already documented in opnsense_client.py's IPsec section (each URL
  segment + "Action" = the PHP method name) — e.g. "addStaticKey" ->
  addStaticKeyAction(). Re-confirmed here, not re-derived independently.

SCOPE DECISION — why this ISN'T "static-key-only auth" the way IPsec is
"PSK-only auth", and what v1 actually does instead:

The plan anticipated that if certificate-based auth needed "deep
integration" with the existing read-only certificate feature, v1 could
fall back to a simpler "static-key (pre-shared) auth only" mode, the
same way IPsec deferred certs/EAP in favor of PSK-only. Reading
OpenVPN.xml directly shows this fallback doesn't exist as such: `role`'s
OptionValues are ONLY "client"/"server" — the legacy (pre-24.1)
openvpn-server/openvpn-client sections' third `mode`, "p2p_shared_key"
(a real, certificate-free "secret" directive), has NO equivalent in the
modern unified Instance model. Every Instance, regardless of role, still
needs a working TLS handshake — i.e. `cert` and `ca` populated with a
certificate/CA that already exists on the target host — to actually pass
traffic. `tls_key` (this app's StaticKey support) is only OpenVPN's
--tls-auth/--tls-crypt control-channel hardening, layered ON TOP of that
TLS handshake, not a replacement for it — so it cannot fill the role IPsec
PSK filled (the sole, sufficient auth material).

Given that, the decision made here (revised 2026-09-09, see
models/openvpn_instance.py's class docstring for the original v1 rationale
this replaces): `cert_ref`/`ca_ref` are per-host maps (`cert_refs`/
`ca_refs`, `{host_id_str: uuid}`), populated via a frontend picker backed
by this project's read-only `GET /api/hosts/{id}/certificates` and its
sibling `GET /api/hosts/{id}/cas`, rather than one hand-typed string
assumed valid on every target host — a real, not hypothetical, gap, since
instances already support genuine multi-host targeting and a cert's UUID
is host-local. `build_payloads()` below resolves the pair for one specific
host_id and raises `MissingCertError` (handled as a fail-closed, no-write
deployment error) if that host has no selection yet — OPNexus still never
validates the UUID's *content* against anything (OPNsense's own API
remains the source of truth and will reject a bad value with a concrete
error, same trust model IPsec uses for local_address/remote_address), it
just guarantees the UUID sent for a given host was actually chosen FOR
that host. tls_key/StaticKey support IS fully built end-to-end (this app
CAN manage that object completely, the same way it
fully manages IPsec's PSK), as an OPTIONAL hardening layer — the closest
available analogue to "PSK-only", just not a drop-in substitute for it.
Deferred to a later pass, matching how IPsec deferred certs/EAP: an actual
certificate-management/picker integration (creating CAs/certs, or at
least a per-host cert dropdown backed by the read-only certs endpoint).

DELIBERATELY SIMPLER STATUS MODEL THAN rule_deploy_service.py — identical
rationale to IpsecTunnel (see ipsec_deploy_service.py's module docstring
for the full argument): an OpenVPN `reconfigure` can equally sever the
exact management path OPNexus needs to auto-revert it, so this entity
also only ever reports "success" or "error" — no pending/confirm/
auto-revert window, no revert attempted automatically or otherwise.
Undeploy (delete) is a normal explicit action, same scope decision as
IPsec/rule delete.

Per-host deploy order (mirrors the OPNsense model's own dependency
direction — Instance.tls_key references the StaticKey's UUID, so the
StaticKey must exist first): StaticKey (only if tls_key_enabled), then
Instance. If tls_key_enabled is turned OFF after previously being on, the
now-orphaned StaticKey is explicitly deleted as part of the same config
write (not left behind as clutter) and the Instance's `tls_key` is
explicitly overlaid to "" to clear the relation — ApiMutableModelController
Base::setBase() only overlays the keys actually posted (same behavior
already relied on for IPsec), so clearing a previously-set
ModelRelationField requires explicitly posting an empty value for it,
not just omitting the key. Undeploy runs the reverse (Instance deleted
before StaticKey, respecting the reference direction), then reconfigures.
If any config-write step fails, the deploy stops there: status="error"
with the concrete failure message, and whatever was already written up to
that point on that host is NOT rolled back (no savepoint/rollback
primitive exists on the OPNsense side, same as every other entity here).
A follow-up deploy attempt reuses whatever UUIDs got created instead of
duplicating them.

The `reconfigure` call is a separate step after all config writes
succeed, per the same pattern IPsec uses: if it fails, status="error"
with the specific message "Konfiguration wurde geschrieben, Dienst-Reload
aber nicht bestätigt — Erreichbarkeit manuell prüfen" (byte-for-byte the
same message IPsec uses, deliberately — it's the exact same failure mode:
config IS live in the OPNsense model at that point, only the reload
confirmation is missing). The frontend gates the whole "push" action
(config write + reconfigure, one backend call) behind an explicit,
hard-to-miss warning — see frontend/components/OpenVpnInstanceReview.tsx —
not two separate user-facing steps.
"""
import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.openvpn_instance import OpenVpnInstance
from app.models.openvpn_instance_deployment import OpenVpnInstanceDeployment
from app.services import opnsense_client, openvpn_key_store
from app.services.opnsense_client import OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redact_for_storage(payloads: dict) -> dict:
    """SECURITY FIX (2026-09-09): `payloads["statickey"]["key"]` carries
    the raw tls-auth/tls-crypt secret, needed on the wire, but `payloads`
    itself used to also become `deployment.last_payload` verbatim — i.e.
    the secret was persisted in plaintext into Postgres, directly
    contradicting openvpn_key_store.py's own "never touches the database,
    disk-only" docstring. Fixed the same way IPsec's build_payloads()/
    _redact_for_storage() now is: store/compare a SHA-256 hash of the
    secret instead of the secret itself, so a rotated key (with every
    other field unchanged) still counts as a real change to push, without
    the recoverable secret ever reaching the deployments table."""
    redacted = dict(payloads)
    statickey = payloads.get("statickey")
    if statickey is not None:
        redacted_statickey = {**statickey}
        secret = redacted_statickey.get("key", "")
        redacted_statickey["key"] = hashlib.sha256(secret.encode()).hexdigest() if secret else ""
        redacted["statickey"] = redacted_statickey
    return redacted


class MissingCertError(ValueError):
    """No certificate/CA selected for this specific target host — the
    caller must fail closed (no config write, no reconfigure) rather than
    send a blank or wrong-host UUID."""


def build_payloads(instance: OpenVpnInstance, host_id: uuid.UUID) -> dict:
    """The (up to) 2 OPNsense-shaped sub-payloads for this instance/host
    pair, keyed the way _write_config uses them. `statickey` is None when
    tls_key hardening is disabled — see module docstring. `instance["tls_key"]`
    is deliberately NOT included here (it's the deploy-time-injected
    StaticKey UUID or ""), the same "don't persist deploy-time wiring into
    the dict used for no-op comparison" rule IPsec's build_payloads()
    documents for its own `connection` field.

    cert/ca are resolved per host_id — see models/openvpn_instance.py's
    class docstring for why a single shared UUID can't work across hosts.
    Raises MissingCertError if this host has no selection yet."""
    cert = instance.cert_refs.get(str(host_id))
    ca = instance.ca_refs.get(str(host_id))
    if not cert or not ca:
        raise MissingCertError(f"Kein Zertifikat/CA für Host {host_id} ausgewählt")

    statickey_payload = None
    if instance.tls_key_enabled:
        key_material = openvpn_key_store.read_secret(instance.id) or ""
        statickey_payload = {
            "mode": instance.tls_key_mode,
            "key": key_material,
            "description": instance.description,
        }
    return {
        "statickey": statickey_payload,
        "instance": {
            "enabled": "1" if instance.enabled else "0",
            "role": instance.role,
            "proto": instance.proto,
            "port": instance.port,
            "local": instance.local,
            "remote": instance.remote,
            "server": instance.server_network,
            "cert": cert,
            "ca": ca,
            "description": instance.description,
        },
    }


async def _next_vpnid(host: OpnsenseHost) -> str:
    data = await opnsense_client.search_openvpn_instances(host)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    used = []
    for row in rows:
        try:
            used.append(int(row.get("vpnid")))
        except (TypeError, ValueError):
            continue
    return str(max(used, default=0) + 1)


async def _write_config(host: OpnsenseHost, payloads: dict, deployment: OpenVpnInstanceDeployment) -> None:
    """Writes the StaticKey (if enabled) then the Instance, in that
    dependency order, persisting each UUID onto `deployment` as soon as
    it's known so a later retry (after a partial failure) updates rather
    than duplicates. Raises OpnsenseAPIError uncaught on the first
    failure — the caller decides what that means for `deployment.status`."""
    statickey_payload = payloads["statickey"]
    if statickey_payload is not None:
        if deployment.opnsense_statickey_uuid:
            await opnsense_client.set_openvpn_static_key(host, deployment.opnsense_statickey_uuid, statickey_payload)
        else:
            result = await opnsense_client.add_openvpn_static_key(host, statickey_payload)
            deployment.opnsense_statickey_uuid = result.get("uuid") if isinstance(result, dict) else None
            if not deployment.opnsense_statickey_uuid:
                raise OpnsenseAPIError("Firewall hat keine StaticKey-UUID zurückgegeben; Stand manuell prüfen")
    elif deployment.opnsense_statickey_uuid:
        # tls_key hardening was switched off after previously being on —
        # remove the now-orphaned StaticKey rather than leaving it behind.
        await opnsense_client.del_openvpn_static_key(host, deployment.opnsense_statickey_uuid)
        deployment.opnsense_statickey_uuid = None

    # Overlay the deploy-time-known tls_key UUID (or "" to explicitly
    # clear a previously-set relation — see module docstring) onto a
    # shallow copy, never mutating `payloads["instance"]` itself: the same
    # reasoning ipsec_deploy_service.py documents for its own
    # local_wire/remote_wire/child_wire, to avoid breaking no-op-redeploy
    # detection on the next deploy.
    instance_wire = {**payloads["instance"], "tls_key": deployment.opnsense_statickey_uuid or ""}

    if deployment.opnsense_instance_uuid:
        await opnsense_client.set_openvpn_instance(host, deployment.opnsense_instance_uuid, instance_wire)
    else:
        # vpnid is Required and the firewall does NOT assign it (verified live
        # on 26.7: "instance.vpnid: A value is required."), so pick the next
        # free one. It stays untouched on later set() calls.
        instance_wire["vpnid"] = await _next_vpnid(host)
        result = await opnsense_client.add_openvpn_instance(host, instance_wire)
        deployment.opnsense_instance_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not deployment.opnsense_instance_uuid:
            raise OpnsenseAPIError("Firewall hat keine Instance-UUID zurückgegeben; Stand manuell prüfen")


async def _deploy_to_host(db: AsyncSession, instance: OpenVpnInstance, host: OpnsenseHost) -> OpenVpnInstanceDeployment:
    existing = await db.scalar(
        select(OpenVpnInstanceDeployment).where(
            OpenVpnInstanceDeployment.instance_id == instance.id, OpenVpnInstanceDeployment.host_id == host.id
        )
    )
    try:
        payloads = build_payloads(instance, host.id)
    except MissingCertError as exc:
        # Fail closed: no config write, no reconfigure call — never push a
        # blank or wrong-host cert/CA reference.
        deployment = existing or OpenVpnInstanceDeployment(instance_id=instance.id, host_id=host.id, status="error")
        deployment.status = "error"
        deployment.message = str(exc)
        deployment.deployed_at = _now()
        if not existing:
            db.add(deployment)
        return deployment

    comparable = _redact_for_storage(payloads)
    if existing and existing.status == "success" and existing.last_payload == comparable:
        # Nothing actually changed on this host — re-affirm without
        # touching the firewall at all (no risk was introduced).
        existing.message = None
        existing.deployed_at = _now()
        return existing

    deployment = existing or OpenVpnInstanceDeployment(instance_id=instance.id, host_id=host.id, status="error")

    try:
        await _write_config(host, payloads, deployment)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        deployment.deployed_at = _now()
        if not existing:
            db.add(deployment)
        return deployment

    try:
        await opnsense_client.reconfigure_openvpn(host)
    except OpnsenseAPIError:
        # Config IS live at this point — this exact message is specified
        # by the plan (byte-for-byte the same as IPsec's), not a generic
        # str(exc), because "the config write failed" and "the config
        # wrote fine but the reload wasn't confirmed" are different
        # failure modes an operator needs to react to differently.
        deployment.status = "error"
        deployment.message = "Konfiguration wurde geschrieben, Dienst-Reload aber nicht bestätigt — Erreichbarkeit manuell prüfen"
        deployment.last_payload = comparable
        deployment.deployed_at = _now()
        if not existing:
            db.add(deployment)
        return deployment

    deployment.status = "success"
    deployment.message = None
    deployment.last_payload = comparable
    deployment.deployed_at = _now()
    if not existing:
        db.add(deployment)
    return deployment


async def deploy_instance(db: AsyncSession, instance: OpenVpnInstance, hosts: list[OpnsenseHost]) -> list[OpenVpnInstanceDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    await ensure_no_maintenance(db, hosts)
    deployments = [await _deploy_to_host(db, instance, host) for host in hosts]
    await db.commit()
    return deployments


async def undeploy_instance(db: AsyncSession, instance: OpenVpnInstance) -> None:
    """Explicit action, not gated by anything (there is no lockout state
    for this entity to gate on) — deletes the Instance before its
    referenced StaticKey (the ModelRelationField dependency direction),
    then reconfigures."""
    failures = []
    result = await db.execute(select(OpenVpnInstanceDeployment).where(OpenVpnInstanceDeployment.instance_id == instance.id))
    for deployment in result.scalars().all():
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host is None:
            await db.delete(deployment)
            continue
        try:
            if deployment.opnsense_instance_uuid:
                await opnsense_client.del_openvpn_instance(host, deployment.opnsense_instance_uuid)
            if deployment.opnsense_statickey_uuid:
                await opnsense_client.del_openvpn_static_key(host, deployment.opnsense_statickey_uuid)
            if deployment.opnsense_instance_uuid or deployment.opnsense_statickey_uuid:
                await opnsense_client.reconfigure_openvpn(host)
        except OpnsenseAPIError as exc:
            deployment.status = "error"
            deployment.message = str(exc)
            failures.append(f"{host.name} ({str(exc)[:200]})")
            continue
        await db.delete(deployment)
    await db.flush()
    if failures:
        await db.commit()
        raise OpnsenseAPIError("Instanz konnte nicht von allen Hosts entfernt werden: " + ", ".join(failures))
