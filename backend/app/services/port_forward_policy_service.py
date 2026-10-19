"""Draft/preview/push state machine for Port Forward rules — mirrors
alias_policy_service.py exactly. effective_hosts is generic over any
{targets, groups, deployments} entity, reused directly from
policy_service rather than duplicated.
"""
import hashlib
import json

from app.models.policy import PortForwardRevision
from app.services.port_forward_deploy_service import _build_payload
from app.services import variables_service
from app.services.policy_service import effective_hosts

PORT_FORWARD_FIELDS = (
    "description", "interface", "ipprotocol", "protocol", "source_net", "source_port",
    "destination_net", "destination_port", "target", "local_port", "natreflection", "enabled",
)


def snapshot(rule):
    return {
        **{key: getattr(rule, key) for key in PORT_FORWARD_FIELDS},
        "target_host_ids": sorted(str(h.id) for h in rule.targets),
        "target_group_ids": sorted(str(g.id) for g in rule.groups),
        "policy_hierarchy_group_id": str(rule.policy_hierarchy_group_id) if rule.policy_hierarchy_group_id else None,
    }


def revision(db, rule, note):
    db.add(PortForwardRevision(port_forward_id=rule.id, note=note, snapshot=snapshot(rule)))


def preview(rule):
    deployments = {d.host_id: d for d in rule.deployments}
    hosts = effective_hosts(rule)
    blocked = any(d.status in ("pending", "revert_failed") for d in rule.deployments)
    rows = []
    for host in hosts:
        dep = deployments.get(host.id)
        try:
            payload = _build_payload(rule, host)
        except variables_service.MissingVariables as exc:
            rows.append(variables_service.preview_row_missing(host, exc.missing[host.name], dep))
            continue
        before = dep.last_payload if dep else None
        action = "create" if not dep or not dep.opnsense_port_forward_uuid else "update"
        if before == payload and dep and dep.status in ("success", "reverted"):
            action = "unchanged"
        if not host.enabled:
            action = "disabled"
        rows.append({"host_id": str(host.id), "host_name": host.name, "action": action,
                     "status": dep.status if dep else None,
                     "message": dep.message if dep else None,
                     "can_remove": False,
                     "can_retry_rollback": bool(dep and dep.status == "revert_failed" and host.enabled),
                     "changes": [{"field": k, "before": (before or {}).get(k), "after": v}
                                 for k, v in payload.items() if (before or {}).get(k) != v]})
    target_ids = {h.id for h in hosts}
    for dep in rule.deployments:
        if dep.host_id not in target_ids:
            rows.append({"host_id": str(dep.host_id), "host_name": dep.host.name,
                         "action": "retained", "status": dep.status, "message": dep.message,
                         "can_remove": bool(not blocked and dep.host.enabled and dep.opnsense_port_forward_uuid),
                         "can_retry_rollback": bool(dep.status == "revert_failed" and dep.host.enabled),
                         "changes": []})
    warnings = []
    if blocked:
        warnings.append("Offene Bestätigung oder fehlgeschlagener Rollback: Push gesperrt.")
    if any(r["action"] == "retained" for r in rows):
        warnings.append("Frühere Ziele behalten diese Regel, bis du sie hier gezielt entfernst. Diese Löschung hat keinen automatischen Rollback.")
    if any(r["action"] == "missing_variable" for r in rows):
        warnings.append("Für mindestens einen Ziel-Host fehlt der Wert einer Variable ({{NAME}}). Erst auf der Host-Seite unter „Variablen“ eintragen.")
    if any(r["action"] == "disabled" for r in rows):
        warnings.append("Deaktivierte Hosts werden übersprungen.")
    if any(d.status == "error" for d in rule.deployments):
        warnings.append("Vorheriger Push fehlgeschlagen. Vor erneutem Push den Firewall-Stand prüfen.")
    state = {"snapshot": snapshot(rule), "rows": rows,
             "hosts": [(str(h.id), h.base_url, h.enabled, h.verify_tls) for h in hosts],
             "deployments": sorted([(str(d.host_id), d.opnsense_port_forward_uuid, d.last_payload, d.previous_payload,
                                      d.host.base_url, d.host.enabled, d.host.verify_tls) for d in rule.deployments], key=lambda row: row[0])}
    token = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
    return {"token": token, "hosts": rows, "warnings": warnings,
            "can_deploy": not blocked and not any(r["action"] == "missing_variable" for r in rows) and any(r["action"] in ("create", "update") for r in rows),
            "basis": "Zuletzt von OPNexus gespeicherter Deploy-Stand; kein Live-Abgleich."}


def workflow(rule, deployments):
    hosts = effective_hosts(rule)
    active = [h for h in hosts if h.enabled]
    by_host = {d.host_id: d for d in deployments}
    target_ids = {h.id for h in hosts}
    counts = {
        "targets": len(hosts),
        "active_targets": len(active),
        "changes": sum(1 for h in active if h.id not in by_host or by_host[h.id].last_payload != variables_service.payload_or_none(_build_payload, rule, h)),
        "pending": sum(d.status == "pending" for d in deployments),
        "errors": sum(d.status in ("error", "revert_failed") for d in deployments),
        "retained": sum(d.host_id not in target_ids for d in deployments),
    }
    if counts["errors"]:
        state = "attention"
    elif counts["pending"]:
        state = "pending"
    elif counts["retained"]:
        state = "cleanup"
    elif not hosts:
        state = "unassigned"
    elif not active:
        state = "disabled"
    elif counts["changes"]:
        state = "draft"
    else:
        state = "synced"
    return state, counts
