"""Pushes a centrally-authored WireGuard interconnect (one Server + one
Peer, v1 scope, see below) to its target hosts, the same "OPNexus writes
via the API instead of relying on OPNsense's own HA config sync" model as
ipsec_deploy_service.py / openvpn_deploy_service.py.

This is the third and — for now — final VPN-management feature this
session shipped (IPsec, then OpenVPN, now WireGuard), matching their exact
rigor and safety discipline. Earlier pre-implementation research this
session (see the "already-confirmed feasibility" framing handed into this
task) established that WireGuard is core, not a plugin, as of OPNsense
24.1+ (moved from the `os-wireguard` plugin into `opnsense/core` itself);
the fleet's real hosts run 26.7.3_11, well past this cutover. That
research also flagged the peer<->server linkage as denormalized at the
controller level — confirmed true below, but the EXACT mechanics (which
endpoint writes what, in what order) were verified fresh from source for
this implementation, not assumed from that summary.

VERIFIED FIELDS / ENDPOINTS (fetched directly from
raw.githubusercontent.com/opnsense/core/master/..., branch master,
2026-09-09 — downloaded the raw XML/PHP files directly via `api.github.com`
+ `raw.githubusercontent.com`, the same "don't trust a summarized fetch"
discipline OpenVPN's own docstring documents, specifically to catch exact
field names and the exact reconfigure response shape without risking
paraphrase error; see opnsense_client.py's "WireGuard" section for the
full endpoint list and its own citations):

- Server.xml (mount `//OPNsense/wireguard/server`, version 1.0.1) ->
  servers.server (custom field type `ServerField`, an ArrayField):
  enabled (BooleanField, Default 1), name (TextField, Required, Mask
  `/^([0-9a-zA-Z._\\-]){1,64}$/u` — same mask this app enforces again in
  schemas/wireguard.py), instance (AutoNumberField, Required,
  MinimumValue 0 — auto-assigned via AutoNumberField::applyDefault() when
  omitted, confirmed by reading AutoNumberField.php directly: it scans its
  siblings for the first unused integer >= its minimum and assigns that
  as the default; this app NEVER sends it, same "never send it, rely on
  the model's own auto-assignment" precedent OpenVPN already established
  for `vpnid` — not independently re-verified against a live add
  response, flagged rather than guessed, same as that precedent), pubkey
  (Base64Field, NO <Required> tag), privkey (Base64Field, Required), port
  (PortField), mtu (IntegerField, 1-9300), dns (NetworkField, AsList=Y),
  tunneladdress (NetworkField, NetMaskRequired=Y, AsList=Y),
  disableroutes (BooleanField, Default 0, has a DependConstraint requiring
  `gateway` when enabled — v1 never exposes this, always sends "0"),
  gateway (NetworkField, v1 unused), carp_depend_on (VirtualIPField type
  "carp", v1 unused — HA/CARP interface dependency), peers
  (ModelRelationField -> OPNsense.Wireguard.Client, items
  `clients.client`, display "name", Multiple=Y — see the
  "denormalized linkage" section below, this app NEVER writes this field
  directly), debug (BooleanField, Default 0, v1 never exposes this,
  always sends "0"), cnfFilename/statFilename/interface (all
  `volatile="true"` — computed by ServerField::actionPostLoadingEvent()
  as `/usr/local/etc/wireguard/wg{instance}.conf` etc., never sent),
  endpoint/peer_dns/allowed_ips (plain TextField, comment "Peer generator
  storage [default values]" in source — only consumed by
  ClientController::getClientBuilderAction/addClientBuilderAction's
  config-builder wizard, which this app does not implement; NOT the same
  thing as a Client's own `tunneladdress`/`serveraddress` fields below).
- Client.xml (mount `//OPNsense/wireguard/client`, version 1.0.0) ->
  clients.client (custom field type `ClientField`, an ArrayField):
  enabled (BooleanField, Default 1), name (TextField, Required, same Mask
  as Server.name), pubkey (Base64Field, Required, UniqueConstraint against
  serveraddress+serverport), psk (Base64Field, optional — WireGuard's own
  per-peer pre-shared key, unrelated to IPsec's PSK despite the shared
  name), tunneladdress (NetworkField, NetMaskRequired=Y, Required=Y,
  AsList=Y — this peer's Allowed-IPs entry), serveraddress (HostnameField,
  optional) / serverport (PortField, optional), endpoint (IPPortField,
  `volatile="true"`), keepalive (IntegerField, 1-65535, optional), servers
  (ModelRelationField -> OPNsense.Wireguard.Server, items
  `servers.server`, display "name", Multiple=Y, ALSO `volatile="true"` —
  see below), privkey (Base64Field, comment "Peer generator storage
  [client specific]" — only used by the config-builder wizard to generate
  a full downloadable peer .conf; this app never populates or reads a
  peer's OWN private key, since a peer's private key belongs to the
  REMOTE side, exactly like a real WireGuard peer entry only ever needs
  the remote's PUBLIC key).
  NOT INDEPENDENTLY VERIFIED beyond field placement/naming/adjacent
  comments in the source: whether `serveraddress`/`serverport` (which
  this app treats as the real, persisted Endpoint this OPNsense box
  should dial to reach the peer) are actually consumed that way by
  configd's wg-quick config generation, as opposed to being config-builder
  wizard inputs too (only `privkey`, immediately above them in the XML,
  carries an explicit "Peer generator storage" comment — serveraddress/
  serverport do not) — this app's verification standard covers the PHP
  controller/model layer, not configd's own template/script content, same
  boundary IPsec/OpenVPN already draw. Flagged rather than guessed.

DENORMALIZED PEER<->SERVER LINKAGE — read directly from
ClientController.php, and the single most important structural fact this
deploy service is built around: unlike IPsec's Local/Remote/Child (which
each carry their OWN `connection` ModelRelationField this app sets
directly) or OpenVPN's Instance->StaticKey reference (a plain UUID field
on the Instance this app writes), WireGuard's Server.peers is the ONLY
persisted list — Client.servers is `volatile="true"` (never written to
config; ClientField::actionPostLoadingEvent() recomputes it at LOAD time
purely for display, by scanning every Server's `peers` for this client's
own UUID). Client.servers is nonetheless what THIS APP sends when
creating/updating a peer (as a comma-list of Server UUIDs, named
"servers", holding exactly one UUID in v1) — ClientController::
setClientAction($uuid), read directly from source, does the actual
cross-object write as a side effect of that one POST: it loads a fresh
Server model instance, adds or removes this peer's UUID to/from each
Server's `peers` field depending on whether that Server's UUID is present
in the posted `servers` list, persists that via
`$mdl->serializeToConfig(false, true)` (bypassing normal validation, since
the peer's own UUID may be brand new), and ONLY THEN calls setBase() to
persist the Client record itself — meaning ONE API call
(`/wireguard/client/setClient/{uuid}` or `.../addClient`) both creates/
updates the peer AND attaches it to its server. delClientAction($uuid)
mirrors this on delete: it strips this peer's UUID from every Server's
`peers` list BEFORE deleting the Client record. CONCRETE CONSEQUENCE:
this app never reads or rewrites a Server's `peers` field itself, at any
point, in either direction — see opnsense_client.py's WireGuard section
for the full citation and why getting the deploy ORDER wrong (writing the
peer before its server exists) would silently produce an unlinked peer
rather than a hard error.

SCOPE DECISION — entity shape (composite Server+Peer vs. two separate
top-level entities): WireGuard's real object model is one Server that can
own MANY peers (Server.peers is Multiple=Y) — a genuine one-to-many, not
IPsec's or OpenVPN's shapes. The realistic v1 fleet use case named in the
plan is still fundamentally 1:1, though: one interconnect between two
managed firewalls, or a managed firewall and one external peer — exactly
the same use case IpsecTunnel already models as one composite form rather
than exposing Swanctl's Connection/Local/Remote/Child as four separate
CRUD screens. Given that, `WireguardTunnel` (see its own docstring) models
exactly one Server + exactly one Peer per row — a 2-object composite
closest in SHAPE to OpenVpnInstance's Instance+optional-StaticKey (not
IPsec's 5-object shape, and not OpenVPN's near-flat single object either)
— rather than splitting Server/Peer into two independent top-level
entities the operator would have to keep in sync by hand across two
separate CRUD screens for no v1-realistic benefit. A genuine multi-peer WG
hub (one Server, many Clients, e.g. a road-warrior-style concentrator) is
a real, deliberately deferred gap this decision creates — flagged here
explicitly, not silently designed around, and would need a materially
different form (one Server entity, N Peer entities referencing it) if
built later.

SCOPE DECISION — no server-side key generation in v1: mirrors IPsec's PSK
and OpenVPN's tls_key precedent exactly, for the same reason — both
`keyPairAction` (`configdRun('wireguard gen_keypair')`) and `pskAction`
(`configdRun('wireguard gen_psk')`) exist and work, but this app
deliberately never calls either. The operator supplies the Server's
private key (`server_private_key`) AND its matching public key
(`server_public_key`) themselves — confirmed necessary by reading
ServerField.php directly: unlike a hypothetical "derive pubkey from
privkey on save" convenience, there is NO such derivation anywhere in the
model/field-type layer, so both halves of the keypair are plain,
independently-supplied fields with no cross-validation that they actually
match (OPNsense's own API is the source of truth here too — a mismatched
pair fails at the WireGuard protocol level, not at the OPNsense config-API
level, so this app can't catch that mistake any more than OPNsense itself
does). Same reasoning for the peer's public key.

SCOPE DECISION — the peer pre-shared key (WireGuard's own optional `psk`
field, not IPsec's PSK): v1 SUPPORTS it, toggled via `peer_psk_enabled`
with material stored the same disk-secret way as the Server's private key
(see wireguard_key_store.py) and tracked via `peer_psk_set` — mirroring
OpenVpnInstance's tls_key_enabled/tls_key_material_set pattern exactly,
since it's the closest analogous "optional extra secret layered onto an
already-otherwise-complete peer" shape. Structurally simpler than
OpenVPN's tls_key/StaticKey, though: WireGuard's psk is a single scalar
field ON the Client object itself, not a separate cross-referenced
OPNsense object needing its own add/set/del lifecycle — so disabling it
after being enabled is just "send psk: ''" on the next Client write, no
orphaned-object cleanup step is needed the way OpenVPN's StaticKey
required one.

DELIBERATELY SIMPLER STATUS MODEL THAN rule_deploy_service.py — identical
rationale to IpsecTunnel/OpenVpnInstance (see their own module docstrings
for the full argument): a WireGuard `reconfigure` can equally sever the
exact management path OPNexus needs to auto-revert it, so this entity
also only ever reports "success" or "error" — no pending/confirm/
auto-revert window, no revert attempted automatically or otherwise.
Undeploy (delete) is a normal explicit action, same scope decision as
IPsec/OpenVPN/rule delete.

Per-host deploy order, dictated by the denormalized linkage above: Server
first (so its UUID exists to be referenced), then Peer (posted with
`servers` = that Server's UUID, letting OPNsense's own controller update
the Server's `peers` list as a side effect — see above). Undeploy runs the
exact reverse: Peer deleted first (which also auto-unlinks it from the
Server's `peers` list server-side, per delClientAction's own behavior
quoted above — this app never needs a manual unlink step), then Server,
then reconfigure. If any config-write step fails, the deploy stops there:
status="error" with the concrete failure message, and whatever was
already written up to that point on that host is NOT rolled back (no
savepoint/rollback primitive exists on the OPNsense side, same as every
other entity here). A follow-up deploy attempt reuses whatever UUIDs got
created instead of duplicating them.

The `reconfigure` call is a separate step after both config writes
succeed, per the same pattern IPsec/OpenVPN use: if it fails, status=
"error" with the specific message "Konfiguration wurde geschrieben,
Dienst-Reload aber nicht bestätigt — Erreichbarkeit manuell prüfen"
(byte-for-byte the same message the other two VPN features use,
deliberately — it's the exact same failure mode: config IS live in the
OPNsense model at that point, only the reload confirmation is missing).
The frontend gates the whole "push" action (config write + reconfigure,
one backend call) behind an explicit, hard-to-miss warning — see
frontend/components/WireguardTunnelReview.tsx — not two separate
user-facing steps.
"""
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.wireguard_tunnel import WireguardTunnel
from app.models.wireguard_tunnel_deployment import WireguardTunnelDeployment
from app.services import opnsense_client, wireguard_key_store
from app.services.opnsense_client import OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


def _redact_for_storage(payloads: dict) -> dict:
    """SECURITY FIX (2026-09-09): `payloads["server"]["privkey"]` and
    `payloads["peer"]["psk"]` carry raw secrets, needed on the wire, but
    `payloads` itself used to also become `deployment.last_payload`
    verbatim — i.e. both secrets were persisted in plaintext into
    Postgres, directly contradicting wireguard_key_store.py's own "never
    touches the database, disk-only" docstring. Fixed the same way
    IPsec's/OpenVPN's build_payloads()/_redact_for_storage() now are:
    store/compare a SHA-256 hash of each secret instead of the secret
    itself, so a rotated key or PSK (with every other field unchanged)
    still counts as a real change to push, without either recoverable
    secret ever reaching the deployments table."""
    return {
        "server": {**payloads["server"], "privkey": _hash_secret(payloads["server"].get("privkey", ""))},
        "peer": {**payloads["peer"], "psk": _hash_secret(payloads["peer"].get("psk", ""))},
    }


def build_payloads(tunnel: WireguardTunnel) -> dict:
    """The 2 OPNsense-shaped sub-payloads for this tunnel, keyed the way
    _write_config uses them. `payloads["peer"]["servers"]` is deliberately
    NOT included here (it's the deploy-time-injected Server UUID) — the
    same "don't persist deploy-time wiring into the dict used for no-op
    comparison" rule ipsec_deploy_service.py/openvpn_deploy_service.py
    document for their own connection/tls_key fields."""
    private_key = wireguard_key_store.read_private_key(tunnel.id) or ""
    psk_material = wireguard_key_store.read_psk(tunnel.id) or "" if tunnel.peer_psk_enabled else ""
    return {
        "server": {
            "enabled": "1" if tunnel.enabled else "0",
            "name": tunnel.server_name,
            "privkey": private_key,
            "pubkey": tunnel.server_public_key,
            "port": tunnel.listen_port,
            "mtu": tunnel.mtu,
            "dns": tunnel.dns,
            "tunneladdress": tunnel.server_tunnel_address,
            "disableroutes": "0",
            "debug": "0",
        },
        "peer": {
            "enabled": "1" if tunnel.enabled else "0",
            "name": tunnel.peer_name,
            "pubkey": tunnel.peer_public_key,
            "psk": psk_material,
            "tunneladdress": tunnel.peer_allowed_ips,
            "serveraddress": tunnel.peer_endpoint_address,
            "serverport": tunnel.peer_endpoint_port,
            "keepalive": tunnel.peer_keepalive,
        },
    }


async def _write_config(host: OpnsenseHost, payloads: dict, deployment: WireguardTunnelDeployment) -> None:
    """Writes the Server then the Peer, in that dependency order (the
    Peer's payload needs the Server's UUID for the `servers` field — see
    module docstring), persisting each UUID onto `deployment` as soon as
    it's known so a later retry (after a partial failure) updates rather
    than duplicates. Raises OpnsenseAPIError uncaught on the first
    failure — the caller decides what that means for `deployment.status`."""
    if deployment.opnsense_server_uuid:
        await opnsense_client.set_wireguard_server(host, deployment.opnsense_server_uuid, payloads["server"])
    else:
        result = await opnsense_client.add_wireguard_server(host, payloads["server"])
        deployment.opnsense_server_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not deployment.opnsense_server_uuid:
            raise OpnsenseAPIError("Firewall hat keine Server-UUID zurückgegeben; Stand manuell prüfen")

    # Overlay the deploy-time-known Server UUID onto a shallow copy, never
    # mutating payloads["peer"] itself — the same reasoning ipsec_deploy_
    # service.py/openvpn_deploy_service.py document for their own
    # local_wire/remote_wire/child_wire/instance_wire, to avoid breaking
    # no-op-redeploy detection on the next deploy.
    peer_wire = {**payloads["peer"], "servers": deployment.opnsense_server_uuid}

    if deployment.opnsense_peer_uuid:
        await opnsense_client.set_wireguard_peer(host, deployment.opnsense_peer_uuid, peer_wire)
    else:
        result = await opnsense_client.add_wireguard_peer(host, peer_wire)
        deployment.opnsense_peer_uuid = result.get("uuid") if isinstance(result, dict) else None
        if not deployment.opnsense_peer_uuid:
            raise OpnsenseAPIError("Firewall hat keine Peer-UUID zurückgegeben; Stand manuell prüfen")


async def _deploy_to_host(db: AsyncSession, tunnel: WireguardTunnel, host: OpnsenseHost) -> WireguardTunnelDeployment:
    payloads = build_payloads(tunnel)
    comparable = _redact_for_storage(payloads)
    existing = await db.scalar(
        select(WireguardTunnelDeployment).where(
            WireguardTunnelDeployment.tunnel_id == tunnel.id, WireguardTunnelDeployment.host_id == host.id
        )
    )
    if existing and existing.status == "success" and existing.last_payload == comparable:
        # Nothing actually changed on this host — re-affirm without
        # touching the firewall at all (no risk was introduced).
        existing.message = None
        existing.deployed_at = _now()
        return existing

    deployment = existing or WireguardTunnelDeployment(tunnel_id=tunnel.id, host_id=host.id, status="error")

    try:
        await _write_config(host, payloads, deployment)
        # Without the global switch the config is stored but no interface
        # ever comes up (live-verified), so a "success" would be a lie.
        await opnsense_client.ensure_wireguard_enabled(host)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        deployment.deployed_at = _now()
        if not existing:
            db.add(deployment)
        return deployment

    try:
        await opnsense_client.reconfigure_wireguard(host)
    except OpnsenseAPIError:
        # Config IS live at this point — this exact message is specified
        # by the plan (byte-for-byte the same as IPsec's/OpenVPN's), not a
        # generic str(exc), because "the config write failed" and "the
        # config wrote fine but the reload wasn't confirmed" are different
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


async def deploy_tunnel(db: AsyncSession, tunnel: WireguardTunnel, hosts: list[OpnsenseHost]) -> list[WireguardTunnelDeployment]:
    from app.services.host_maintenance_service import ensure_no_maintenance
    await ensure_no_maintenance(db, hosts)
    deployments = [await _deploy_to_host(db, tunnel, host) for host in hosts]
    await db.commit()
    return deployments


async def undeploy_tunnel(db: AsyncSession, tunnel: WireguardTunnel) -> None:
    """Explicit action, not gated by anything (there is no lockout state
    for this entity to gate on) — deletes the Peer before the Server (the
    dependency direction; deleting the Peer first also auto-unlinks it
    from the Server's `peers` list server-side, per delClientAction's own
    behavior — see module docstring), then reconfigures."""
    failures = []
    result = await db.execute(select(WireguardTunnelDeployment).where(WireguardTunnelDeployment.tunnel_id == tunnel.id))
    for deployment in result.scalars().all():
        host = await db.get(OpnsenseHost, deployment.host_id)
        if host is None:
            await db.delete(deployment)
            continue
        try:
            if deployment.opnsense_peer_uuid:
                await opnsense_client.del_wireguard_peer(host, deployment.opnsense_peer_uuid)
            if deployment.opnsense_server_uuid:
                await opnsense_client.del_wireguard_server(host, deployment.opnsense_server_uuid)
            if deployment.opnsense_peer_uuid or deployment.opnsense_server_uuid:
                await opnsense_client.reconfigure_wireguard(host)
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
