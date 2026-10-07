"""Non-deploy helpers for OpenVpnInstance: snapshot/revision/preview/
workflow, mirroring ipsec_service.py's role for IpsecTunnel.
effective_hosts() and policy_lock() are NOT reimplemented here —
policy_service's versions work unchanged for any object exposing
`.targets`/`.groups`, same reuse ipsec_service.py already established."""
import hashlib
import json

from app.models.policy import OpenVpnInstanceRevision
from app.services.openvpn_deploy_service import MissingCertError, build_payloads
from app.services.policy_service import effective_hosts  # noqa: F401 - re-exported for routers/openvpn.py

OPENVPN_FIELDS = (
    "description", "role", "proto", "port", "local", "remote", "server_network",
    "cert_refs", "ca_refs", "tls_key_enabled", "tls_key_mode", "enabled",
)

# Exact wording shared verbatim with frontend/components/OpenVpnInstanceReview.tsx
# (its NO_ROLLBACK_WARNING constant) so the frontend can filter it out of
# the generic warnings list and render it as its own prominent banner —
# same technique already used for IpsecTunnelReview.tsx.
NO_ROLLBACK_WARNING = (
    "Kein automatischer Rollback: falls dieser Host nur über den OpenVPN-Tunnel erreichbar ist, den du gerade "
    "änderst, kann OPNexus die Verbindung nach einem Fehler NICHT automatisch wiederherstellen."
)


def snapshot(instance):
    return {
        **{key: getattr(instance, key) for key in OPENVPN_FIELDS},
        "tls_key_material_set": instance.tls_key_material_set,
        "target_host_ids": sorted(str(h.id) for h in instance.targets),
        "target_group_ids": sorted(str(g.id) for g in instance.groups),
    }


def revision(db, instance, note):
    db.add(OpenVpnInstanceRevision(instance_id=instance.id, note=note, snapshot=snapshot(instance)))


def preview(instance):
    deployments = {d.host_id: d for d in instance.deployments}
    hosts = effective_hosts(instance)
    rows = []
    for host in hosts:
        dep = deployments.get(host.id)
        before = dep.last_payload if dep else None
        try:
            payloads = build_payloads(instance, host.id)
        except MissingCertError:
            rows.append({
                "host_id": str(host.id), "host_name": host.name, "action": "missing_cert",
                "status": dep.status if dep else None,
                "message": "Kein Zertifikat/CA für diesen Host ausgewählt.",
                "can_remove": False,
                "changes": [],
            })
            continue
        action = "create" if not dep or not dep.opnsense_instance_uuid else "update"
        if before == payloads and dep and dep.status == "success":
            action = "unchanged"
        if not host.enabled:
            action = "disabled"
        changes = []
        if action not in ("disabled",):
            for section in ("statickey", "instance"):
                before_section = (before or {}).get(section) or {}
                after_section = payloads[section] or {}
                keys = set(before_section) | set(after_section)
                for key in keys:
                    if key == "tls_key":
                        continue  # UUID filled in at deploy time, not a user-visible field
                    prior = before_section.get(key)
                    value = after_section.get(key)
                    if prior != value:
                        label = "Static-Key" if section == "statickey" and key == "key" else f"{section}.{key}"
                        # Never surface the key material's own value in a diff view.
                        shown_before = "•••" if section == "statickey" and key == "key" and prior else prior
                        shown_after = "•••" if section == "statickey" and key == "key" and value else value
                        changes.append({"field": label, "before": shown_before, "after": shown_after})
        rows.append({
            "host_id": str(host.id), "host_name": host.name, "action": action,
            "status": dep.status if dep else None,
            "message": dep.message if dep else None,
            "can_remove": False,
            "changes": changes,
        })
    target_ids = {h.id for h in hosts}
    for dep in instance.deployments:
        if dep.host_id not in target_ids:
            rows.append({
                "host_id": str(dep.host_id), "host_name": dep.host.name,
                "action": "retained", "status": dep.status, "message": dep.message,
                "can_remove": bool(dep.host.enabled and (dep.opnsense_instance_uuid or dep.opnsense_statickey_uuid)),
                "changes": [],
            })
    warnings = [NO_ROLLBACK_WARNING]
    if any(r["action"] == "retained" for r in rows):
        warnings.append("Frühere Ziele behalten ihre Instanz, bis du sie hier gezielt entfernst.")
    if any(r["action"] == "disabled" for r in rows):
        warnings.append("Deaktivierte Hosts werden übersprungen.")
    if any(r["action"] == "missing_cert" for r in rows):
        warnings.append("Für mindestens einen Ziel-Host fehlt eine Zertifikat-/CA-Auswahl. Dieser Host wird beim Push mit einem Fehler übersprungen.")
    if any(d.status == "error" for d in instance.deployments):
        warnings.append("Vorheriger Push fehlgeschlagen. Vor erneutem Push den Firewall-Stand prüfen.")
    if instance.tls_key_enabled and not instance.tls_key_material_set:
        warnings.append("TLS-Hardening (tls-auth/tls-crypt) ist aktiviert, aber es wurde noch kein Schlüsselmaterial hinterlegt.")
    blocked = instance.tls_key_enabled and not instance.tls_key_material_set
    state = {
        "snapshot": snapshot(instance), "rows": rows,
        "hosts": [(str(h.id), h.base_url, h.enabled, h.verify_tls) for h in hosts],
        "deployments": sorted(
            [(str(d.host_id), d.opnsense_statickey_uuid, d.opnsense_instance_uuid, d.last_payload,
              d.host.base_url, d.host.enabled, d.host.verify_tls)
             for d in instance.deployments],
            key=lambda row: row[0],
        ),
    }
    token = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "token": token, "hosts": rows, "warnings": warnings,
        "can_deploy": not blocked and any(r["action"] in ("create", "update") for r in rows),
        "basis": "Zuletzt von OPNexus gespeicherter Deploy-Stand; kein Live-Abgleich.",
    }


def _host_changed(instance, by_host, host):
    try:
        payloads = build_payloads(instance, host.id)
    except MissingCertError:
        return True
    dep = by_host.get(host.id)
    return dep is None or dep.last_payload != payloads


def workflow(instance, deployments):
    hosts = effective_hosts(instance)
    active = [h for h in hosts if h.enabled]
    by_host = {d.host_id: d for d in deployments}
    target_ids = {h.id for h in hosts}
    counts = {
        "targets": len(hosts),
        "active_targets": len(active),
        "changes": sum(1 for h in active if _host_changed(instance, by_host, h)),
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
    elif counts["changes"] or (instance.tls_key_enabled and not instance.tls_key_material_set):
        state = "draft"
    else:
        state = "synced"
    return state, counts
