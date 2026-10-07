"""Non-deploy helpers for IpsecTunnel: snapshot/revision/preview/workflow,
mirroring policy_service.py's role for FirewallRule. effective_hosts() and
policy_lock() are NOT reimplemented here — policy_service's versions work
unchanged for any object exposing `.targets`/`.groups` (see plan: "reuse
policy_service.effective_hosts directly").
"""
import hashlib
import json

from app.models.policy import IpsecTunnelRevision
from app.services.ipsec_deploy_service import build_payloads
from app.services.policy_service import effective_hosts  # noqa: F401 - re-exported for routers/ipsec.py

IPSEC_FIELDS = (
    "description", "local_address", "remote_address", "ike_version", "proposal",
    "local_identity", "remote_identity", "local_networks", "remote_networks",
    "esp_proposal", "enabled",
)


def snapshot(tunnel):
    return {
        **{key: getattr(tunnel, key) for key in IPSEC_FIELDS},
        "pre_shared_key_set": tunnel.pre_shared_key_set,
        "target_host_ids": sorted(str(h.id) for h in tunnel.targets),
        "target_group_ids": sorted(str(g.id) for g in tunnel.groups),
    }


def revision(db, tunnel, note):
    db.add(IpsecTunnelRevision(tunnel_id=tunnel.id, note=note, snapshot=snapshot(tunnel)))


def preview(tunnel):
    payloads = build_payloads(tunnel)
    deployments = {d.host_id: d for d in tunnel.deployments}
    hosts = effective_hosts(tunnel)
    rows = []
    for host in hosts:
        dep = deployments.get(host.id)
        before = dep.last_payload if dep else None
        action = "create" if not dep or not dep.opnsense_connection_uuid else "update"
        if before == payloads and dep and dep.status == "success":
            action = "unchanged"
        if not host.enabled:
            action = "disabled"
        changes = []
        if action not in ("disabled",):
            for section in ("connection", "local", "remote", "child", "psk"):
                before_section = (before or {}).get(section, {})
                after_section = payloads[section]
                for key, value in after_section.items():
                    if key == "connection":
                        continue  # UUID filled in at deploy time, not a user-visible field
                    prior = before_section.get(key)
                    if prior != value:
                        label = "Pre-Shared-Key" if section == "psk" and key == "Key" else f"{section}.{key}"
                        # Never surface the PSK's own value in a diff view.
                        shown_before = "•••" if section == "psk" and key == "Key" and prior else prior
                        shown_after = "•••" if section == "psk" and key == "Key" else value
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
                "can_remove": bool(dep.host.enabled and (dep.opnsense_connection_uuid or dep.opnsense_psk_uuid)),
                "changes": [],
            })
    warnings = [
        "Kein automatischer Rollback: falls dieser Host nur über den VPN-Tunnel erreichbar ist, den du gerade "
        "änderst, kann OPNexus die Verbindung nach einem Fehler NICHT automatisch wiederherstellen."
    ]
    if any(r["action"] == "retained" for r in rows):
        warnings.append("Frühere Ziele behalten ihren Tunnel, bis du sie hier gezielt entfernst.")
    if any(r["action"] == "disabled" for r in rows):
        warnings.append("Deaktivierte Hosts werden übersprungen.")
    if any(d.status == "error" for d in tunnel.deployments):
        warnings.append("Vorheriger Push fehlgeschlagen. Vor erneutem Push den Firewall-Stand prüfen.")
    if not tunnel.pre_shared_key_set:
        warnings.append("Für diesen Tunnel ist noch kein Pre-Shared-Key hinterlegt.")
    state = {
        "snapshot": snapshot(tunnel), "rows": rows,
        "hosts": [(str(h.id), h.base_url, h.enabled, h.verify_tls) for h in hosts],
        "deployments": sorted(
            [(str(d.host_id), d.opnsense_connection_uuid, d.opnsense_local_uuid, d.opnsense_remote_uuid,
              d.opnsense_child_uuid, d.opnsense_psk_uuid, d.last_payload, d.host.base_url, d.host.enabled, d.host.verify_tls)
             for d in tunnel.deployments],
            key=lambda row: row[0],
        ),
    }
    token = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "token": token, "hosts": rows, "warnings": warnings,
        "can_deploy": tunnel.pre_shared_key_set and any(r["action"] in ("create", "update") for r in rows),
        "basis": "Zuletzt von OPNexus gespeicherter Deploy-Stand; kein Live-Abgleich.",
    }


def workflow(tunnel, deployments):
    hosts = effective_hosts(tunnel)
    active = [h for h in hosts if h.enabled]
    by_host = {d.host_id: d for d in deployments}
    payloads = build_payloads(tunnel)
    target_ids = {h.id for h in hosts}
    counts = {
        "targets": len(hosts),
        "active_targets": len(active),
        "changes": sum(1 for h in active if h.id not in by_host or by_host[h.id].last_payload != payloads),
        "errors": sum(d.status == "error" for d in deployments),
        "retained": sum(d.host_id not in target_ids for d in deployments),
    }
    if counts["errors"]:
        state = "attention"
    elif counts["retained"]:
        state = "cleanup"
    elif not hosts:
        state = "unassigned"
    elif not active:
        state = "disabled"
    elif counts["changes"] or not tunnel.pre_shared_key_set:
        state = "draft"
    else:
        state = "synced"
    return state, counts
