"""Gateway health of a firewall (dpinger numbers, as OPNsense reports them).

``get_gateway_health(host)`` is the one reusable entry point: a live read of
/routes/gateway/status, one :class:`GatewayHealth` per gateway. The notification
engine, the host page and the metrics read the SAME numbers from the periodic
resource sample (``compact`` / ``from_compact``), so a scrape or a page view never
fans out to the firewalls; the 1.25 health gate can call ``get_gateway_health``
directly. Parsing is lenient on purpose: "~" means "nothing measured", never zero.
"""
import re

from app.models.host import OpnsenseHost
from app.schemas.gateway import GatewayHealth, GatewayState
from app.services import opnsense_client as client

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_DEGRADED = {"delay", "loss", "delay+loss"}


def _number(value) -> float | None:
    if value is None:
        return None
    match = _NUMBER.search(str(value))
    return float(match.group()) if match else None


def _state(status: str, measured: bool) -> GatewayState:
    if status == "down":
        return "offline"
    if status == "force_down":
        return "forced_down"
    if status in _DEGRADED:
        return "degraded"
    if status == "none":
        # "none" is also what a gateway without monitoring reports; only numbers make it "online".
        return "online" if measured else "unknown"
    return "unknown"


def parse_gateway_status(payload) -> list[GatewayHealth]:
    items = payload.get("items") if isinstance(payload, dict) else None
    result: list[GatewayHealth] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        status = str(item.get("status") or "").lower()
        loss, delay, stddev = _number(item.get("loss")), _number(item.get("delay")), _number(item.get("stddev"))
        if loss is not None and loss >= 100:
            delay = stddev = None  # dpinger prints 0.0 ms when nothing answers: no round trip, not a fast one
        measured = loss is not None or delay is not None
        address = item.get("address")
        monitor = item.get("monitor")
        result.append(GatewayHealth(
            name=str(item["name"]),
            address=None if address in (None, "", "~") else str(address),
            status=status or "unknown",
            state=_state(status, measured),
            loss=loss, delay=delay, stddev=stddev,
            monitor=None if monitor in (None, "", "~") else str(monitor),
        ))
    return result


async def get_gateway_health(host: OpnsenseHost) -> list[GatewayHealth]:
    """Live gateway state of ``host``. Raises OpnsenseAPIError if the firewall cannot be asked."""
    return parse_gateway_status(await client.get_gateway_status(host))


def compact(gateways: list[GatewayHealth]) -> list[dict]:
    """What the resource sampler stores per sample (small, no addresses)."""
    return [
        {"name": g.name, "status": g.status, "state": g.state, "loss": g.loss, "delay": g.delay, "stddev": g.stddev}
        for g in gateways
    ]


def from_compact(rows) -> list[GatewayHealth]:
    return [
        GatewayHealth(
            name=r["name"], status=r.get("status") or "unknown", state=r.get("state") or "unknown",
            loss=r.get("loss"), delay=r.get("delay"), stddev=r.get("stddev"),
        )
        for r in rows or [] if isinstance(r, dict) and r.get("name")
    ]


def is_bad(gateway: GatewayHealth, loss_threshold_pct: float = 0, delay_threshold_ms: float = 0) -> bool:
    """Alarm-worthy: down, or (when the thresholds are on) measured loss / latency beyond them.
    A gateway forced down by the admin and one that is not measured are not alarms."""
    if gateway.state == "offline":
        return True
    if loss_threshold_pct and gateway.loss is not None and gateway.loss >= loss_threshold_pct:
        return True
    if delay_threshold_ms and gateway.delay is not None and gateway.delay >= delay_threshold_ms:
        return True
    return False

