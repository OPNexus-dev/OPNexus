"""Core tree logic for hierarchical pre-/post-rulebases.

Two distinct traversal directions matter here, and conflating them is the
easiest way to get this feature subtly wrong:

- effective_hosts() walks DOWNWARD from a rule's authoring group to every
  descendant group, to find every host the rule should reach at all
  (targeting/preview/deploy).
- ordered_hierarchy_rules() walks UPWARD across a single host's entire
  ancestor chain, to determine what order every applicable rule (from
  every level of the tree, not just one group) must be pushed in on THAT
  host. Since every OPNexus-pushed rule sets "quick": "1" (strict
  first-match), this order is a security property, not cosmetic — see the
  plan/README for the "quick" note.

Both take an already-loaded flat list of every PolicyHierarchyGroup (with
.hosts eagerly loaded) rather than querying inside a loop — the tree is
expected to be small, and building it in Python once per request avoids N+1
queries and awkward recursive-CTE code for what is, in practice, a shallow
org hierarchy.
"""
import hashlib
import json
import uuid

from app.errors import ErrorCode, api_error

from app.models.policy import PolicyHierarchyRuleRevision
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.services.policy_service import policy_lock  # re-exported: don't redefine the advisory lock

HIERARCHY_RULE_FIELDS = (
    "description", "interface", "direction", "ipprotocol", "protocol", "action",
    "source_net", "source_port", "destination_net", "destination_port", "enabled",
)

__all__ = [
    "HIERARCHY_RULE_FIELDS", "policy_lock", "descendants", "effective_hosts", "ancestor_chain",
    "ordered_hierarchy_rules", "snapshot", "revision", "ensure_editable", "preview", "workflow",
]


def _by_parent(all_groups: list[PolicyHierarchyGroup]) -> dict[uuid.UUID | None, list[PolicyHierarchyGroup]]:
    by_parent: dict[uuid.UUID | None, list[PolicyHierarchyGroup]] = {}
    for g in all_groups:
        by_parent.setdefault(g.parent_id, []).append(g)
    return by_parent


def descendants(group_id: uuid.UUID, all_groups: list[PolicyHierarchyGroup]) -> list[PolicyHierarchyGroup]:
    """`group_id` itself plus every descendant group, in no particular order."""
    by_id = {g.id: g for g in all_groups}
    by_parent = _by_parent(all_groups)
    result: list[PolicyHierarchyGroup] = []
    stack = [group_id]
    seen: set[uuid.UUID] = set()
    while stack:
        gid = stack.pop()
        if gid in seen:
            continue
        seen.add(gid)
        g = by_id.get(gid)
        if g is None:
            continue
        result.append(g)
        stack.extend(c.id for c in by_parent.get(gid, []))
    return result


def effective_hosts(rule: PolicyHierarchyRule, all_groups: list[PolicyHierarchyGroup]):
    hosts = {}
    for group in descendants(rule.hierarchy_group_id, all_groups):
        for h in group.hosts:
            hosts[h.id] = h
    return sorted(hosts.values(), key=lambda h: (h.name, str(h.id)))


def ancestor_chain(host, all_groups: list[PolicyHierarchyGroup]) -> list[PolicyHierarchyGroup]:
    """Root-most group first, host's own (leaf-most) group last. Empty if
    the host isn't assigned to any policy-hierarchy group."""
    by_id = {g.id: g for g in all_groups}
    chain: list[PolicyHierarchyGroup] = []
    current_id = host.policy_hierarchy_group_id
    seen: set[uuid.UUID] = set()
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        g = by_id.get(current_id)
        if g is None:
            break
        chain.append(g)
        current_id = g.parent_id
    chain.reverse()
    return chain


def ordered_hierarchy_rules(host, interface: str, rulebase: str, all_groups: list[PolicyHierarchyGroup],
                             all_rules: list[PolicyHierarchyRule]) -> list[PolicyHierarchyRule]:
    """The actual deploy order for every enabled rule on `interface`/
    `rulebase` that applies to `host` via its ancestor chain.

    Pre-rulebase: root-most group's rules first (an org-wide pre-rule must
    win before anything more specific gets a chance to). Post-rulebase:
    leaf-most group's rules first (the most specific group's post-rule is
    the "last word" closest to any host-local default-deny). Within one group, rules
    are ordered by their manually-set `sequence`.
    """
    chain = ancestor_chain(host, all_groups)
    if rulebase == "post":
        chain = list(reversed(chain))
    ordered: list[PolicyHierarchyRule] = []
    for group in chain:
        group_rules = sorted(
            (r for r in all_rules
             if r.hierarchy_group_id == group.id and r.rulebase == rulebase
             and r.interface == interface and r.enabled),
            key=lambda r: (r.sequence, str(r.id)),
        )
        ordered.extend(group_rules)
    return ordered


def snapshot(rule: PolicyHierarchyRule) -> dict:
    return {
        **{key: getattr(rule, key) for key in HIERARCHY_RULE_FIELDS},
        "hierarchy_group_id": str(rule.hierarchy_group_id),
        "rulebase": rule.rulebase,
        "sequence": rule.sequence,
    }


def revision(db, rule: PolicyHierarchyRule, note: str) -> None:
    db.add(PolicyHierarchyRuleRevision(policy_hierarchy_id=rule.id, note=note, snapshot=snapshot(rule)))


def ensure_editable(rule: PolicyHierarchyRule) -> None:
    if any(d.status in ("pending", "revert_failed") for d in rule.deployments):
        raise api_error(409, ErrorCode.OPEN_DEPLOYMENT_DRAFT_UNCHANGED)


def preview(rule: PolicyHierarchyRule, all_groups: list[PolicyHierarchyGroup]) -> dict:
    from app.services.policy_hierarchy_deploy_service import _build_payload

    payload = _build_payload(rule)
    deployments = {d.host_id: d for d in rule.deployments}
    hosts = effective_hosts(rule, all_groups)
    blocked = any(d.status in ("pending", "revert_failed") for d in rule.deployments)
    rows = []
    for host in hosts:
        dep = deployments.get(host.id)
        before = dep.last_payload if dep else None
        action = "create" if not dep or not dep.opnsense_policy_hierarchy_uuid else "update"
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
                         "can_remove": bool(not blocked and dep.host.enabled and dep.opnsense_policy_hierarchy_uuid),
                         "can_retry_rollback": bool(dep.status == "revert_failed" and dep.host.enabled),
                         "changes": []})
    warnings = []
    if blocked:
        warnings.append("Offene Bestätigung oder fehlgeschlagener Rollback: Push gesperrt.")
    if any(r["action"] == "retained" for r in rows):
        warnings.append("Frühere Ziele behalten ihre Regel, bis du sie hier gezielt entfernst. Diese Löschung hat keinen automatischen Rollback.")
    if any(r["action"] == "disabled" for r in rows):
        warnings.append("Deaktivierte Hosts werden übersprungen.")
    if any(d.status == "error" for d in rule.deployments):
        warnings.append("Vorheriger Push fehlgeschlagen. Vor erneutem Push den Firewall-Stand prüfen.")
    state = {"snapshot": snapshot(rule), "rows": rows,
             "hosts": [(str(h.id), h.base_url, h.enabled, h.verify_tls) for h in hosts],
             "deployments": sorted([(str(d.host_id), d.opnsense_policy_hierarchy_uuid, d.last_payload, d.previous_payload,
                                      d.host.base_url, d.host.enabled, d.host.verify_tls) for d in rule.deployments], key=lambda row: row[0])}
    token = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
    return {"token": token, "hosts": rows, "warnings": warnings,
            "can_deploy": not blocked and any(r["action"] in ("create", "update") for r in rows),
            "basis": "Zuletzt von OPNexus gespeicherter Deploy-Stand; kein Live-Abgleich."}


def workflow(rule: PolicyHierarchyRule, deployments, all_groups: list[PolicyHierarchyGroup]):
    from app.services.policy_hierarchy_deploy_service import _build_payload

    hosts = effective_hosts(rule, all_groups)
    active = [h for h in hosts if h.enabled]
    by_host = {d.host_id: d for d in deployments}
    payload = _build_payload(rule)
    target_ids = {h.id for h in hosts}
    counts = {
        "targets": len(hosts),
        "active_targets": len(active),
        "changes": sum(1 for h in active if h.id not in by_host or by_host[h.id].last_payload != payload),
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
