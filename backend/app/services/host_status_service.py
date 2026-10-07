"""Live host status (online/offline + CARP master/backup) via getVipStatus.

Each host is queried independently and failures are caught per-host, so one
unreachable firewall never blocks the others from reporting — this backs
the hosts list endpoint's mixed online/offline behaviour.
"""
import asyncio

from app.models.host import OpnsenseHost
from app.services.opnsense_client import OpnsenseAPIError, get_vip_status


def _truthy(value, default: bool) -> bool:
    """OPNsense's getVipStatus returns carp.maintenancemode as a native JSON
    bool but other carp.* flags as "1"/"0" strings — normalize both."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value) == "1"


def _extract_carp_status(data: dict | None) -> str | None:
    rows = data.get("rows") if isinstance(data, dict) else None
    if not rows:
        return None
    statuses = {row.get("status", "").upper() for row in rows if isinstance(row, dict)}
    if "MASTER" in statuses:
        return "MASTER"
    if "BACKUP" in statuses:
        return "BACKUP"
    return None


async def _status_for(host: OpnsenseHost) -> dict:
    if not host.enabled:
        return {"status": "disabled", "carp_status": None, "status_detail": None}
    try:
        data = await get_vip_status(host)
    except OpnsenseAPIError as exc:
        return {"status": "offline", "carp_status": None, "status_detail": str(exc)}
    carp = data.get("carp", {}) if isinstance(data, dict) else {}
    return {
        "status": "online",
        "carp_status": _extract_carp_status(data),
        "carp_maintenance_mode": _truthy(carp.get("maintenancemode"), default=False),
        "carp_allowed": _truthy(carp.get("allow"), default=True),
        "carp_demotion": carp.get("demotion"),
        "status_detail": carp.get("status_msg") or None,
    }


async def get_statuses(hosts: list[OpnsenseHost]) -> dict:
    results = await asyncio.gather(*(_status_for(h) for h in hosts))
    return {host.id: result for host, result in zip(hosts, results)}
