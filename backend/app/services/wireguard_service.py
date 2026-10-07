"""Non-deploy helpers for WireguardTunnel: snapshot/revision/preview/
workflow, mirroring ipsec_service.py's/openvpn_service.py's role for their
own entities. effective_hosts() and policy_lock() are NOT reimplemented
here — policy_service's versions work unchanged for any object exposing
`.targets`/`.groups`, same reuse already established for the other two
VPN features."""
import hashlib
import json

from app.models.policy import WireguardTunnelRevision
from app.services import wireguard_key_store
from app.services.wireguard_deploy_service import _redact_for_storage, build_payloads
from app.services.policy_service import effective_hosts  # noqa: F401 - re-exported for routers/wireguard.py

WIREGUARD_FIELDS = (
    "server_name", "server_public_key", "listen_port", "server_tunnel_address", "dns", "mtu",
    "peer_name", "peer_public_key", "peer_psk_enabled", "peer_allowed_ips",
    "peer_endpoint_address", "peer_endpoint_port", "peer_keepalive", "enabled",
)

# Exact wording shared verbatim with
# frontend/components/WireguardTunnelReview.tsx (its NO_ROLLBACK_WARNING
# constant) so the frontend can filter it out of the generic warnings list
# and render it as its own prominent banner — same technique already used
# for IpsecTunnelReview.tsx/OpenVpnInstanceReview.tsx.
NO_ROLLBACK_WARNING = (
    "Kein automatischer Rollback: falls dieser Host nur über den WireGuard-Tunnel erreichbar ist, den du gerade "
    "änderst, kann OPNexus die Verbindung nach einem Fehler NICHT automatisch wiederherstellen."
)


def snapshot(tunnel):
    return {
        **{key: getattr(tunnel, key) for key in WIREGUARD_FIELDS},
        "server_private_key_set": tunnel.server_private_key_set,
        "peer_psk_set": tunnel.peer_psk_set,
        "target_host_ids": sorted(str(h.id) for h in tunnel.targets),
        "target_group_ids": sorted(str(g.id) for g in tunnel.groups),
    }


def revision(db, tunnel, note):
    db.add(WireguardTunnelRevision(tunnel_id=tunnel.id, note=note, snapshot=snapshot(tunnel)))


def preview(tunnel):
    # Compare in the stored (secret -> hash) form, see ipsec_service.preview().
    payloads = _redact_for_storage(build_payloads(tunnel))
    deployments = {d.host_id: d for d in tunnel.deployments}
    hosts = effective_hosts(tunnel)
    rows = []
    for host in hosts:
        dep = deployments.get(host.id)
        before = dep.last_payload if dep else None
        action = "create" if not dep or not dep.opnsense_server_uuid else "update"
        if before == payloads and dep and dep.status == "success":
            action = "unchanged"
        if not host.enabled:
            action = "disabled"
        changes = []
        if action not in ("disabled",):
            for section in ("server", "peer"):
                before_section = (before or {}).get(section) or {}
                after_section = payloads[section] or {}
                keys = set(before_section) | set(after_section)
                for key in keys:
                    if key == "servers":
                        continue  # UUID filled in at deploy time, not a user-visible field
                    prior = before_section.get(key)
                    value = after_section.get(key)
                    if prior != value:
                        is_secret = (section == "server" and key == "privkey") or (section == "peer" and key == "psk")
                        label = "Server-Privatschlüssel" if is_secret and section == "server" else ("Peer-PSK" if is_secret else f"{section}.{key}")
                        # Never surface secret material's own value in a diff view.
                        shown_before = "•••" if is_secret and prior else prior
                        shown_after = "•••" if is_secret and value else value
                        changes.append({"field": label, "before": shown_before, "after": shown_after})
        rows.append({
            "host_id": str(host.id), "host_name": host.name, "action": action,
            "status": dep.status if dep else None,
            "message": dep.message if dep else None,
            "can_remove": False,
            "changes": changes,
        })
    target_ids = {h.id for h in hosts}
    for dep in tunnel.deployments:
        if dep.host_id not in target_ids:
            rows.append({
                "host_id": str(dep.host_id), "host_name": dep.host.name,
                "action": "retained", "status": dep.status, "message": dep.message,
                "can_remove": bool(dep.host.enabled and (dep.opnsense_server_uuid or dep.opnsense_peer_uuid)),
                "changes": [],
            })
    warnings = [NO_ROLLBACK_WARNING]
    if any(r["action"] == "retained" for r in rows):
        warnings.append("Frühere Ziele behalten ihren Tunnel, bis du sie hier gezielt entfernst.")
    if any(r["action"] == "disabled" for r in rows):
        warnings.append("Deaktivierte Hosts werden übersprungen.")
    if any(d.status == "error" for d in tunnel.deployments):
        warnings.append("Vorheriger Push fehlgeschlagen. Vor erneutem Push den Firewall-Stand prüfen.")
    if not tunnel.server_private_key_set:
        warnings.append("Für diesen Tunnel ist noch kein Server-Privatschlüssel hinterlegt.")
    if tunnel.peer_psk_enabled and not tunnel.peer_psk_set:
        warnings.append("Peer-Pre-Shared-Key ist aktiviert, aber es wurde noch kein Schlüsselmaterial hinterlegt.")
    blocked = not tunnel.server_private_key_set or (tunnel.peer_psk_enabled and not tunnel.peer_psk_set)
    state = {
        "snapshot": snapshot(tunnel), "rows": rows,
        "secret_version": wireguard_key_store.version(tunnel.id),
        "hosts": [(str(h.id), h.base_url, h.enabled, h.verify_tls) for h in hosts],
        "deployments": sorted(
            [(str(d.host_id), d.opnsense_server_uuid, d.opnsense_peer_uuid, d.last_payload,
              d.host.base_url, d.host.enabled, d.host.verify_tls)
             for d in tunnel.deployments],
            key=lambda row: row[0],
        ),
    }
    token = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "token": token, "hosts": rows, "warnings": warnings,
        "can_deploy": not blocked and any(r["action"] in ("create", "update") for r in rows),
        "basis": "Zuletzt von OPNexus gespeicherter Deploy-Stand; kein Live-Abgleich.",
    }


def workflow(tunnel, deployments):
    hosts = effective_hosts(tunnel)
    active = [h for h in hosts if h.enabled]
    by_host = {d.host_id: d for d in deployments}
    payloads = _redact_for_storage(build_payloads(tunnel))
    target_ids = {h.id for h in hosts}
    counts = {
        "targets": len(hosts),
        "active_targets": len(active),
        "changes": sum(1 for h in active if h.id not in by_host or by_host[h.id].last_payload != payloads),
        "errors": sum(d.status == "error" for d in deployments),
        "retained": sum(d.host_id not in target_ids for d in deployments),
    }
    missing_secret = not tunnel.server_private_key_set or (tunnel.peer_psk_enabled and not tunnel.peer_psk_set)
    if counts["errors"]:
        state = "attention"
    elif counts["retained"]:
        state = "cleanup"
    elif not hosts:
        state = "unassigned"
    elif not active:
        state = "disabled"
    elif counts["changes"] or missing_secret:
        state = "draft"
    else:
        state = "synced"
    return state, counts
