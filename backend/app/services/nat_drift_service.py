"""Backend port of frontend/components/LiveNatDrift.tsx's fingerprint/diff
logic for the three NAT types, mirroring rule_drift_service.py's approach
for firewall rules. Keep the three fingerprint functions here in exact sync
with LiveNatDrift.tsx's frontend copies.

Field names verified against OPNsense core source before use, not guessed:
- Port Forward (D-NAT): DNatController::searchRuleAction returns "descr"
  (not "description") and dotted flat keys "source.network"/"source.port"/
  "destination.network"/"destination.port" (not nested objects — that
  nesting only exists in the addRule/setRule *write* payload shape used by
  port_forward_deploy_service.py's _build_payload, not the search response).
- One-to-One / Outbound NAT: flat "description"/"source_net"/
  "destination_net" etc., matching the field names already verified and
  used by one_to_one_deploy_service.py / outbound_nat_deploy_service.py's
  own _build_payload (search response and write payload agree here since
  neither model nests source/destination).

Only the "rules" key (user-managed) is compared — "automatic_rules" are
OPNsense-internal, host/interface-specific, and would produce false-positive
drift if compared across hosts (see nat_live_service.py's own docstring).
"""
from app.models.host import OpnsenseHost
from app.services import opnsense_client
from app.services.nat_live_service import split_rows
from app.services.opnsense_client import OpnsenseAPIError


def port_forward_fingerprint(row: dict) -> str:
    return "|".join(str(row.get(key, "")) for key in (
        "descr", "interface", "protocol", "source.network", "source.port",
        "destination.network", "destination.port", "target", "local-port",
    ))


def one_to_one_fingerprint(row: dict) -> str:
    return "|".join(str(row.get(key, "")) for key in (
        "description", "interface", "type", "source_net", "destination_net", "external",
    ))


def outbound_nat_fingerprint(row: dict) -> str:
    return "|".join(str(row.get(key, "")) for key in (
        "description", "interface", "protocol", "source_net", "source_port",
        "destination_net", "destination_port", "target", "target_port",
    ))


_SPECS = {
    "port_forward": (opnsense_client.search_port_forward_rules, port_forward_fingerprint),
    "one_to_one": (opnsense_client.search_one_to_one_rules, one_to_one_fingerprint),
    "outbound_nat": (opnsense_client.search_outbound_nat_rules, outbound_nat_fingerprint),
}


async def _live_rows_or_none(host: OpnsenseHost, nat_type: str) -> list[dict] | None:
    search_fn, _ = _SPECS[nat_type]
    try:
        data = await search_fn(host)
        return split_rows(data)["rules"]
    except OpnsenseAPIError:
        return None


async def compute_cluster_drift(hosts: list[OpnsenseHost], nat_type: str) -> list[dict]:
    """Same shape/contract as rule_drift_service.compute_cluster_drift:
    one row per fingerprint missing from at least one reachable host."""
    _, fingerprint_fn = _SPECS[nat_type]
    per_host = {host.id: await _live_rows_or_none(host, nat_type) for host in hosts}
    reachable_ids = {host_id for host_id, rows in per_host.items() if rows is not None}

    by_fingerprint: dict[str, dict] = {}
    for host_id in reachable_ids:
        for row in per_host[host_id]:
            fp = fingerprint_fn(row)
            entry = by_fingerprint.setdefault(fp, {"fingerprint": fp, "sample": row, "present_on": set()})
            entry["present_on"].add(host_id)

    for entry in by_fingerprint.values():
        entry["reachable_host_ids"] = reachable_ids

    return [entry for entry in by_fingerprint.values() if len(entry["present_on"]) < len(reachable_ids)]
