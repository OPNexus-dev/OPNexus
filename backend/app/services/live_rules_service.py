"""Fetches the rules actually configured on a host right now, straight from
OPNsense's searchRule — including ones never pushed by this tool (built-in
defaults, anything configured by hand). Separate from RuleDeployment, which
only tracks what OPNexus itself has deployed.

Response shape verified live against a real OPNsense 25.x host: {"total":
N, "rowCount": N, "current": 1, "rows": [...]}, each row a flat dict with
both raw fields (action, direction, protocol, ...) and OPNsense's own
human-readable "%"-prefixed counterparts (%action, %direction, ...), which
this maps onto LiveRule's *_label fields directly rather than re-deriving
them.
"""
from app.models.host import OpnsenseHost
from app.schemas.host import LiveRule
from app.services import opnsense_client


def _row_to_live_rule(row: dict) -> LiveRule:
    return LiveRule(
        uuid=row.get("uuid", ""),
        description=row.get("description") or "(ohne Beschreibung)",
        action=row.get("action", ""),
        action_label=row.get("%action") or row.get("action", ""),
        direction=row.get("direction", ""),
        direction_label=row.get("%direction") or row.get("direction", ""),
        interface=row.get("interface") or None,
        interface_label=row.get("%interface") or row.get("interface") or None,
        protocol=row.get("protocol") or None,
        protocol_label=row.get("%protocol") or row.get("protocol") or None,
        source_net=row.get("source_net") or "any",
        source_port=row.get("source_port") or None,
        destination_net=row.get("destination_net") or "any",
        destination_port=row.get("destination_port") or None,
        enabled=row.get("enabled") in ("1", 1, True),
        is_automatic=bool(row.get("is_automatic")),
    )


async def get_live_rules(host: OpnsenseHost) -> list[LiveRule]:
    data = await opnsense_client.search_rules(host)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [_row_to_live_rule(row) for row in rows]
