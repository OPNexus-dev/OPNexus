"""Manual NAT only works in hybrid/advanced. Never change the global mode."""
from app.errors import ErrorCode, api_error
from app.services import opnsense_client
from app.services.opnsense_client import OpnsenseAPIError


def parse_mode(data: dict) -> str:
    def child(value, key):
        return value.get(key) if isinstance(value, dict) else None
    model = child(data, "filter")
    value = child(child(child(model, "settings"), "nat"), "snat_mode")
    if value is None:
        value = child(child(model, "general"), "snat_mode")
    if isinstance(value, dict):
        selected = [k for k, v in value.items() if isinstance(v, dict) and v.get("selected") in (True, 1, "1")]
        value = selected[0] if len(selected) == 1 else None
    return value if isinstance(value, str) and value in {"automatic", "hybrid", "advanced", "disabled"} else "unknown"


async def host_mode(host):
    try:
        mode = parse_mode(await opnsense_client.get_source_nat_settings(host))
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.OUTBOUND_MODE_UNREADABLE, host=host.name, reason=str(exc)) from exc
    return {"host_id": str(host.id), "host_name": host.name, "mode": mode, "can_deploy": mode in ("hybrid", "advanced")}


async def require_manual_mode(host):
    state = await host_mode(host)
    if not state["can_deploy"]:
        raise api_error(409, ErrorCode.OUTBOUND_MODE_BLOCKS_PUSH, host=host.name, mode=state["mode"])
    return state
