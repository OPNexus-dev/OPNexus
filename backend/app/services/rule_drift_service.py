"""Backend port of frontend/components/LiveRulesDrift.tsx's fingerprint/diff
logic — needed so the notification checker can detect fleet-wide rule drift
without a browser involved. Keep this in exact sync with that file's
fingerprint() and computeClusterDrift(): if the two diverge, the UI and the
notifications will disagree about what counts as drift.

Rules have no stable cross-host id (each OPNsense instance mints its own
UUID independently) — matched by the same best-effort content fingerprint
the frontend uses. Good enough to spot "this rule exists on A but not B";
a rule edited differently on two hosts without touching any of these fields
won't show up as drift, on either side.
"""
from app.models.host import OpnsenseHost
from app.schemas.host import LiveRule
from app.services.live_rules_service import get_live_rules
from app.services.opnsense_client import OpnsenseAPIError


def fingerprint(rule: LiveRule) -> str:
    return "|".join([
        rule.description,
        rule.interface or "",
        rule.direction,
        rule.protocol or "",
        rule.source_net,
        rule.source_port or "",
        rule.destination_net,
        rule.destination_port or "",
        rule.action,
    ])


async def _live_rules_or_none(host: OpnsenseHost) -> list[LiveRule] | None:
    try:
        return await get_live_rules(host)
    except OpnsenseAPIError:
        return None


async def compute_cluster_drift(hosts: list[OpnsenseHost]) -> list[dict]:
    """Returns one row per fingerprint that isn't present on every reachable
    host in the cluster: {"fingerprint": str, "sample": LiveRule,
    "present_on": set[host_id], "reachable_host_ids": set[host_id]}.
    Unreachable hosts are excluded from the comparison entirely, same as
    the frontend's catch-and-skip — host-offline reporting already covers
    that case separately, no need to double-report it here as drift.
    """
    per_host = {host.id: await _live_rules_or_none(host) for host in hosts}
    reachable_ids = {host_id for host_id, rules in per_host.items() if rules is not None}

    by_fingerprint: dict[str, dict] = {}
    for host_id in reachable_ids:
        for rule in per_host[host_id]:
            fp = fingerprint(rule)
            entry = by_fingerprint.setdefault(fp, {"fingerprint": fp, "sample": rule, "present_on": set()})
            entry["present_on"].add(host_id)

    for entry in by_fingerprint.values():
        entry["reachable_host_ids"] = reachable_ids

    return [entry for entry in by_fingerprint.values() if len(entry["present_on"]) < len(reachable_ids)]
