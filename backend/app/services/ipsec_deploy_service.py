"""Pushes a centrally-authored site-to-site IPsec tunnel (PSK auth, v1
scope) to its target hosts, the same "OPNexus writes via the API instead
of relying on OPNsense's own HA config sync" model as rule_deploy_service.py.

VERIFIED FIELDS / ENDPOINTS (fetched directly from
github.com/opnsense/core, branch master, 2026-09-08 — see
opnsense_client.py's "IPsec (site-to-site, PSK)" section for the endpoint
list and its own citations):

- Swanctl.xml → Connections.Connection: enabled, proposals
  (IPsecProposalField, Phase=1), unique, aggressive, version (OptionField:
  0=IKEv1+IKEv2, 1=IKEv1, 2=IKEv2), mobike, local_addrs/remote_addrs
  (IKEAddressField — comma-separated IP/subnet/hostname), local_port/
  remote_port, encap, reauth_time, rekey_time, over_time, dpd_delay,
  dpd_timeout, send_certreq, send_cert, keyingtries, description.
- Swanctl.xml → locals.local / remotes.remote (separate top-level
  ArrayFields, NOT nested under Connection): enabled, `connection`
  (ModelRelationField — the parent Connection's UUID, confirmed as a
  plain string equality check in ConnectionsController::connectionFilter()
  and ::delConnectionAction()), round, auth (OptionValues include "psk" —
  v1 only ever sends this), id (StrictTextField, the IKE identity),
  eap_id, certs/pubkeys (unused, v1 is PSK-only), description. `remote`
  additionally has groups/cacerts — unused in v1.
- Swanctl.xml → children.child (separate ArrayField, `connection`
  ModelRelationField back to the same Connection UUID): enabled,
  `connection`, reqid, esp_proposals (IPsecProposalField, Phase=2),
  sha256_96, start_action/close_action/dpd_action, mode (default
  "tunnel" — what this app relies on), policies, local_ts/remote_ts
  (NetworkField, AsList=Y — comma-separated CIDRs), rekey_time,
  description.
- IPsec.xml → preSharedKeys.preSharedKey (SEPARATE model,
  `OPNsense\\IPsec\\IPsec`, NOT part of Swanctl): ident, remote_ident,
  keyType (default "PSK"), Key (capital K), description. Confirmed: this
  is NOT linked to the Connection/Local/Remote by UUID — swanctl resolves
  it by STRING EQUALITY between ident/remote_ident and the Local/Remote
  `id` field, which is exactly why local_identity/remote_identity are
  reused verbatim for both the Local/Remote payload's `id` and the PSK
  payload's `ident`/`remote_ident` below.
- Every OPNsense field NOT listed in the payload builders below is
  deliberately omitted, not forgotten: ApiMutableModelControllerBase::
  addBase()/setBase() only overlay the keys actually posted onto a
  model node that Add() already populated with the model's own XML
  <Default> values (verified directly in ApiMutableModelControllerBase.php)
  — e.g. Child.mode defaults to "tunnel", Connection.mobike defaults to
  enabled, without OPNexus ever having to state so.
- IPsecProposalField (FieldTypes/IPsecProposalField.php): a
  BaseListField (Multiple=Y ⇒ comma-separated on the wire) whose values
  are cipher-suite strings like "aes256-sha256-modp2048", OR the literal
  "default" — which is what both proposals and esp_proposals default to
  in the model XML itself. This app always sends "default" unless a user
  explicitly overrides it in the form: a source-confirmed, conservative
  choice, NOT verified against a live getConnection response (no call was
  ever made against a real host for this feature).
- Endpoints (module "ipsec"): connections/{add,set,del,search,get}
  {Connection,Local,Remote,Child}, preSharedKeys/{add,set,del,search,get}
  Item, service/reconfigure. All confirmed as literal PHP method names in
  ConnectionsController.php / PreSharedKeysController.php / (inherited)
  ApiMutableServiceControllerBase.php, and the URL-segment-to-class-name
  mapping confirmed against Router.php::parsePath() — see
  opnsense_client.py for the full writeup.

DELIBERATELY SIMPLER STATUS MODEL THAN rule_deploy_service.py — no
pending/confirm/auto-revert window (user-approved decision, see plan
"IPsec-VPN-Management"): a firewall rule mistake is a foot-gun; an IPsec
`reconfigure` mistake can be a fatal one, because it can sever the exact
management path OPNexus would need in order to auto-revert it. Pretending
to offer a safety net here would be actively misleading, so this entity
only ever reports "success" or "error" (see IpsecTunnelDeployment's
docstring) — no revert is ever attempted, automatically or otherwise.
Undeploy (delete) is a normal explicit action, same as rule delete's
scope decision to not be lockout-gated either.

Per-host deploy order, mirroring the OPNsense model's own dependency
chain (Local/Remote/Child all reference their Connection's UUID, and
delConnectionAction cascades in the same direction — see
ConnectionsController::delConnectionAction() above): Connection, then
Local, then Remote, then Child, then the PreSharedKey — and undeploy runs
the exact reverse (Child/Local/Remote deleted before their parent
Connection, PreSharedKey has no ordering dependency on the others at all
since it's a separate model linked only by identity-string match). If any
config-write step fails, the deploy stops there: status="error" with the
concrete failure message, and whatever was already written up to that
point on that host is NOT rolled back — there is no savepoint/rollback
primitive for this on the OPNsense side (same as filter rules), and this
entity doesn't build one on top the way rule_deploy_service.py's
pending/confirm window does. A follow-up deploy attempt reuses whatever
UUIDs got created (via last_payload / opnsense_*_uuid) instead of
duplicating them.

The `reconfigure` call is a separate step after all 5 config writes
succeed, per the plan: if it fails, status="error" with the specific
message "Konfiguration wurde geschrieben, Dienst-Reload aber nicht
bestätigt — Erreichbarkeit manuell prüfen" — the config IS live in
swanctl's stored model at that point, it's only the reload confirmation
that's missing. The frontend is responsible for gating the whole
"push" action (config write + reconfigure, both happen in one backend
call) behind an explicit, hard-to-miss warning — see
frontend/components/IpsecTunnelReview.tsx — not for splitting it into two
separate user-facing steps.
"""
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.ipsec_tunnel import IpsecTunnel
from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
from app.services import opnsense_client, ipsec_psk_store
from app.services.opnsense_client import OpnsenseAPIError

_VERSION_MAP = {"ike": "0", "ikev1": "1", "ikev2": "2"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redact_for_storage(payloads: dict) -> dict:
    """SECURITY FIX (2026-09-09): `payloads["psk"]["Key"]` carries the raw
    PSK secret, needed on the wire, but `payloads` itself used to also
    become `deployment.last_payload` verbatim — i.e. the secret was
    persisted in plaintext into Postgres, directly contradicting
    ipsec_psk_store.py's own "never touches the database, disk-only"
    docstring. Fixed by storing/comparing a SHA-256 hash of the secret
    instead of the secret itself: a rotated PSK (with every other field
    unchanged) still correctly counts as a real change to push, but the
    recoverable secret value never reaches the deployments table."""
    redacted = {**payloads, "psk": {**payloads["psk"]}}
    secret = redacted["psk"].get("Key", "")
    redacted["psk"]["Key"] = hashlib.sha256(secret.encode()).hexdigest() if secret else ""
    return redacted


def build_payloads(tunnel: IpsecTunnel) -> dict:
    """The 5 OPNsense-shaped sub-payloads for this tunnel, keyed the same
    way _deploy_to_host/undeploy use them. `connection` is filled in by
    the caller once the parent Connection's UUID is known — see below."""
    secret = ipsec_psk_store.read_secret(tunnel.id)
    return {
        "connection": {
            "enabled": "1" if tunnel.enabled else "0",
            "proposals": tunnel.proposal,
            "version": _VERSION_MAP[tunnel.ike_version],
            "local_addrs": tunnel.local_address,
            "remote_addrs": tunnel.remote_address,
            "description": tunnel.description,
        },
        "local": {
            "enabled": "1" if tunnel.enabled else "0",
            "auth": "psk",
            "id": tunnel.local_identity,
            "description": tunnel.description,
        },
        "remote": {
            "enabled": "1" if tunnel.enabled else "0",
            "auth": "psk",
            "id": tunnel.remote_identity,
            "description": tunnel.description,
        },
        "child": {
            "enabled": "1" if tunnel.enabled else "0",
            "esp_proposals": tunnel.esp_proposal,
            "local_ts": tunnel.local_networks,
            "remote_ts": tunnel.remote_networks,
            "description": tunnel.description,
        },
        "psk": {
            "ident": tunnel.local_identity,
            "remote_ident": tunnel.remote_identity,
            "keyType": "PSK",
            "Key": secret or "",
            "description": tunnel.description,
        },
    }


async def _write_config(host: OpnsenseHost, payloads: dict, deployment: IpsecTunnelDeployment) -> None:
    """Runs the 5 config-write calls in dependency order, persisting each
    UUID onto `deployment` as soon as it's known so a later retry (after a
    partial failure) updates rather than duplicates. Raises
    OpnsenseAPIError uncaught on the first failure — the caller decides
    what that means for `deployment.status`."""
    if deployment.opnsense_connection_uuid:
        await opnsense_client.set_ipsec_connection(host, deployment.opnsense_connection_uuid, payloads["connection"])
    else:
        result = await opnsense_client.add_ipsec_connection(host, payloads["connection"])
        deployment.opnsense_connection_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not deployment.opnsense_connection_uuid:
            raise OpnsenseAPIError("Firewall hat keine Connection-UUID zurückgegeben; Stand manuell prüfen")

    # `payloads` (built by build_payloads()) is also what gets persisted as
    # last_payload for no-op-redeploy comparison — the injected `connection`
    # UUID is deploy-time wiring, not user-editable tunnel state, so it's
    # sent to OPNsense via a shallow copy here rather than mutated into the
    # dict that gets compared/stored (a prior version of this function
    # mutated payloads["local"] etc. directly, which broke no-op detection
    # on the very next deploy: build_payloads() never re-adds `connection`,
    # so the freshly-built dict would never again equal the stored one).
    local_wire = {**payloads["local"], "connection": deployment.opnsense_connection_uuid}
    remote_wire = {**payloads["remote"], "connection": deployment.opnsense_connection_uuid}
    child_wire = {**payloads["child"], "connection": deployment.opnsense_connection_uuid}

    if deployment.opnsense_local_uuid:
        await opnsense_client.set_ipsec_local(host, deployment.opnsense_local_uuid, local_wire)
    else:
        result = await opnsense_client.add_ipsec_local(host, local_wire)
        deployment.opnsense_local_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not deployment.opnsense_local_uuid:
            raise OpnsenseAPIError("Firewall hat keine Local-UUID zurückgegeben; Stand manuell prüfen")

    if deployment.opnsense_remote_uuid:
        await opnsense_client.set_ipsec_remote(host, deployment.opnsense_remote_uuid, remote_wire)
    else:
        result = await opnsense_client.add_ipsec_remote(host, remote_wire)
        deployment.opnsense_remote_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not deployment.opnsense_remote_uuid:
            raise OpnsenseAPIError("Firewall hat keine Remote-UUID zurückgegeben; Stand manuell prüfen")

    if deployment.opnsense_child_uuid:
        await opnsense_client.set_ipsec_child(host, deployment.opnsense_child_uuid, child_wire)
    else:
        result = await opnsense_client.add_ipsec_child(host, child_wire)
        deployment.opnsense_child_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not deployment.opnsense_child_uuid:
            raise OpnsenseAPIError("Firewall hat keine Child-UUID zurückgegeben; Stand manuell prüfen")

    if deployment.opnsense_psk_uuid:
        await opnsense_client.set_ipsec_psk(host, deployment.opnsense_psk_uuid, payloads["psk"])
    else:
        result = await opnsense_client.add_ipsec_psk(host, payloads["psk"])
        deployment.opnsense_psk_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not deployment.opnsense_psk_uuid:
            raise OpnsenseAPIError("Firewall hat keine PSK-UUID zurückgegeben; Stand manuell prüfen")


async def _deploy_to_host(db: AsyncSession, tunnel: IpsecTunnel, host: OpnsenseHost) -> IpsecTunnelDeployment:
    payloads = build_payloads(tunnel)
    comparable = _redact_for_storage(payloads)
    existing = await db.scalar(
        select(IpsecTunnelDeployment).where(
            IpsecTunnelDeployment.tunnel_id == tunnel.id, IpsecTunnelDeployment.host_id == host.id
        )
    )
    if existing and existing.status == "success" and existing.last_payload == comparable:
        # Nothing actually changed on this host — re-affirm without
        # touching the firewall at all (no risk was introduced).
        existing.message = None
        existing.deployed_at = _now()
        return existing

    deployment = existing or IpsecTunnelDeployment(tunnel_id=tunnel.id, host_id=host.id, status="error")

    try:
        await _write_config(host, payloads, deployment)
        # Same trap as WireGuard: with the global IPsec switch off the config
        # is stored but nothing runs, and reconfigure still answers ok.
        await opnsense_client.ensure_ipsec_enabled(host)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        deployment.deployed_at = _now()
        if not existing:
            db.add(deployment)
        return deployment

    try:
        await opnsense_client.reconfigure_ipsec(host)
    except OpnsenseAPIError:
        # Config IS live at this point (all 5 writes above succeeded) —
        # this exact message is specified by the plan, not a generic
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


async def deploy_tunnel(db: AsyncSession, tunnel: IpsecTunnel, hosts: list[OpnsenseHost]) -> list[IpsecTunnelDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    await ensure_no_maintenance(db, hosts)
    deployments = [await _deploy_to_host(db, tunnel, host) for host in hosts]
    await db.commit()
    return deployments


async def undeploy_tunnel(db: AsyncSession, tunnel: IpsecTunnel) -> None:
    """Explicit action, not gated by anything (there is no lockout state
    for this entity to gate on) — deletes Child/Local/Remote before their
    parent Connection (the ModelRelationField dependency direction), then
    the PreSharedKey (independent of the other 4), then reconfigures."""
    failures = []
    result = await db.execute(select(IpsecTunnelDeployment).where(IpsecTunnelDeployment.tunnel_id == tunnel.id))
    for deployment in result.scalars().all():
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host is None:
            await db.delete(deployment)
            continue
        try:
            if deployment.opnsense_child_uuid:
                await opnsense_client.del_ipsec_child(host, deployment.opnsense_child_uuid)
            if deployment.opnsense_local_uuid:
                await opnsense_client.del_ipsec_local(host, deployment.opnsense_local_uuid)
            if deployment.opnsense_remote_uuid:
                await opnsense_client.del_ipsec_remote(host, deployment.opnsense_remote_uuid)
            if deployment.opnsense_connection_uuid:
                await opnsense_client.del_ipsec_connection(host, deployment.opnsense_connection_uuid)
            if deployment.opnsense_psk_uuid:
                await opnsense_client.del_ipsec_psk(host, deployment.opnsense_psk_uuid)
            if any([deployment.opnsense_child_uuid, deployment.opnsense_local_uuid, deployment.opnsense_remote_uuid,
                    deployment.opnsense_connection_uuid, deployment.opnsense_psk_uuid]):
                await opnsense_client.reconfigure_ipsec(host)
        except OpnsenseAPIError as exc:
            deployment.status = "error"
            deployment.message = str(exc)
            failures.append(f"{host.name} ({str(exc)[:200]})")
            continue
        await db.delete(deployment)
    await db.flush()
    if failures:
        await db.commit()
        raise OpnsenseAPIError("Tunnel konnte nicht von allen Hosts entfernt werden: " + ", ".join(failures))
