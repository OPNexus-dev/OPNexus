"""ZFS snapshots (boot environments) of one firewall: list, create, delete.

Snapshots are a safety net before bigger changes, not a rollback button:
activating one needs a reboot and discards everything since, so that action is
deliberately NOT offered here. Deleting refuses the active/next-boot snapshot
(the firewall refuses too; we say so up front). Every step is audited.
"""
import re

from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.services import audit_service, opnsense_client
from app.services.opnsense_client import OpnsenseAPIError

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _row(row: dict) -> dict:
    return {
        "uuid": str(row.get("uuid", "")), "name": str(row.get("name", "")), "active": str(row.get("active", "")),
        "size": str(row.get("size", "")), "created": str(row.get("created_str", "")),
        "mountpoint": str(row.get("mountpoint", "")),
    }


async def list_snapshots(host: OpnsenseHost) -> dict:
    try:
        supported = (await opnsense_client._request(host, "GET", "/core/snapshots/isSupported")).get("supported")
    except OpnsenseAPIError as exc:
        if "404" in str(exc):
            return {"supported": False, "rows": []}
        raise
    if not supported:
        return {"supported": False, "rows": []}
    return {"supported": True, "rows": [_row(r) for r in await opnsense_client.list_snapshots(host)]}


async def create(host: OpnsenseHost, name: str, note: str) -> dict:
    if not NAME.match(name):
        raise api_error(422, ErrorCode.NETWORK_FIELD_INVALID, field="name")
    audit = {"kind": "snapshot", "action": "snapshot.add", "entity_name": name, "host_id": str(host.id), "host_name": host.name}
    await audit_service.persist(**audit, status="started", message=None, details={"note": note[:200]})
    try:
        await opnsense_client.create_snapshot(host, name, note[:200])
        created = next((r for r in await opnsense_client.list_snapshots(host) if r.get("name") == name), None)
    except OpnsenseAPIError as exc:
        await audit_service.persist(**audit, status="rejected", message=str(exc)[:500], details={})
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)[:300]) from exc
    await audit_service.persist(**audit, status="applied" if created else "unverified", message=None, details={})
    if not created:
        raise api_error(502, ErrorCode.NETWORK_APPLY_UNCERTAIN, host=host.name, backup_id="-")
    return _row(created)


async def delete(host: OpnsenseHost, uuid: str) -> None:
    rows = await opnsense_client.list_snapshots(host)
    target = next((r for r in rows if str(r.get("uuid")) == uuid), None)
    if target is None:
        raise api_error(404, ErrorCode.NETWORK_ITEM_NOT_FOUND)
    if str(target.get("active", "")).strip() not in ("", "-"):
        # N = active now, R = active after reboot: removing either would break the boot environment.
        raise api_error(409, ErrorCode.NETWORK_CHANGE_BLOCKED, reasons="snapshot_active")
    audit = {"kind": "snapshot", "action": "snapshot.del", "entity_name": str(target.get("name")), "host_id": str(host.id), "host_name": host.name}
    await audit_service.persist(**audit, status="started", message=None, details={})
    try:
        await opnsense_client.delete_snapshot(host, uuid)
        still = any(str(r.get("uuid")) == uuid for r in await opnsense_client.list_snapshots(host))
    except OpnsenseAPIError as exc:
        await audit_service.persist(**audit, status="rejected", message=str(exc)[:500], details={})
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)[:300]) from exc
    await audit_service.persist(**audit, status="unverified" if still else "applied", message=None, details={})
    if still:
        raise api_error(502, ErrorCode.NETWORK_APPLY_UNCERTAIN, host=host.name, backup_id="-")
