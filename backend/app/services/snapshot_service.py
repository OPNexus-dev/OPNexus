"""ZFS snapshots (boot environments) of one firewall: list, create, delete.

Snapshots are a safety net before bigger changes. Activating an arbitrary one is
deliberately NOT offered (it needs a reboot and discards everything since); the
only way back is the guided firmware rollback to a point OPNexus itself created
right before an update (see "Rollback points" below, maintenance action
`firmware_rollback`). Deleting refuses the active/next-boot snapshot
(the firewall refuses too; we say so up front). Every step is audited.
"""
import logging
import re
from datetime import datetime, timezone

from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.services import audit_service, opnsense_client
from app.services.opnsense_client import OpnsenseAPIError

logger = logging.getLogger(__name__)
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _row(row: dict) -> dict:
    return {
        "uuid": str(row.get("uuid", "")), "name": str(row.get("name", "")), "active": str(row.get("active", "")),
        "size": str(row.get("size", "")), "created": str(row.get("created_str", "")),
        "mountpoint": str(row.get("mountpoint", "")),
    }


async def supported(host: OpnsenseHost) -> bool:
    try:
        return bool((await opnsense_client._request(host, "GET", "/core/snapshots/isSupported")).get("supported"))
    except OpnsenseAPIError as exc:
        if "404" in str(exc):  # OPNsense without the snapshots API
            return False
        raise


async def list_snapshots(host: OpnsenseHost) -> dict:
    if not await supported(host):
        return {"supported": False, "rows": []}
    return {"supported": True, "rows": [_row(r) for r in await opnsense_client.list_snapshots(host)]}


# ── Rollback points (1.32, B5) ──────────────────────────────────────────────
# Before every firmware update/upgrade OPNexus creates a boot environment of the running system. Going back to it
# (activate + reboot) restores the old firmware AND the old configuration of that moment: everything changed on the
# firewall since is gone. Only these points are offered for going back, never boot environments made by hand.
ROLLBACK_PREFIX = "opnexus-pre-"
KEEP_ROLLBACK_POINTS = 2
_POINT = re.compile(r"^opnexus-pre-(?P<version>[A-Za-z0-9._-]+)-(?P<stamp>\d{8}-\d{6})$")


def _is_active(row: dict) -> bool:
    # N = active now, R = active after reboot.
    return str(row.get("active", "")).strip() not in ("", "-")


def rollback_points(rows: list[dict]) -> list[dict]:
    """OPNexus rollback points, newest first: {uuid, name, version, stamp, active, created}."""
    out = []
    for row in rows:
        match = _POINT.match(str(row.get("name", "")))
        if match:
            out.append({"uuid": str(row.get("uuid", "")), "name": match.group(0), "version": match.group("version"),
                        "stamp": match.group("stamp"), "active": str(row.get("active", "")).strip(),
                        "created": str(row.get("created_str", ""))})
    return sorted(out, key=lambda p: p["stamp"], reverse=True)


async def create_rollback_point(host: OpnsenseHost, version: str | None) -> dict | None:
    """Boot environment right before a firmware action. None when the firewall has no ZFS snapshots (UFS install):
    the update may still run, without a way back. Raises OpnsenseAPIError when creating it fails (callers abort the
    update: fail closed). Older OPNexus points beyond KEEP_ROLLBACK_POINTS are removed afterwards."""
    if not await supported(host):
        return None
    clean_version = re.sub(r"[^A-Za-z0-9._-]", "-", version or "unbekannt")[:30] or "unbekannt"
    name = f"{ROLLBACK_PREFIX}{clean_version}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    audit = {"kind": "snapshot", "action": "snapshot.rollback_point", "entity_name": name, "host_id": str(host.id), "host_name": host.name}
    await audit_service.persist(**audit, status="started", message=None, details={"version": version})
    try:
        await opnsense_client.create_snapshot(host, name, f"OPNexus: Rückweg-Punkt vor dem Firmware-Update von {version}")
        rows = await opnsense_client.list_snapshots(host)
    except OpnsenseAPIError as exc:
        await audit_service.persist(**audit, status="rejected", message=str(exc)[:500], details={})
        raise
    point = next((p for p in rollback_points(rows) if p["name"] == name), None)
    if point is None:
        await audit_service.persist(**audit, status="unverified", message=None, details={})
        raise OpnsenseAPIError(f"Rückweg-Punkt {name} nach dem Anlegen nicht in der Liste")
    await audit_service.persist(**audit, status="applied", message=None, details={})
    for old in [p for p in rollback_points(rows) if not _is_active(p)][KEEP_ROLLBACK_POINTS:]:
        try:  # housekeeping only: a point that cannot be removed stays, the update goes on
            await opnsense_client.delete_snapshot(host, old["uuid"])
            await audit_service.persist(kind="snapshot", action="snapshot.del", entity_name=old["name"], host_id=str(host.id),
                                        host_name=host.name, status="applied", message="Älterer Rückweg-Punkt aufgeräumt", details={})
        except OpnsenseAPIError as exc:
            logger.warning("Rückweg-Punkt %s auf %s nicht gelöscht: %s", old["name"], host.name, exc)
    return {"uuid": point["uuid"], "name": point["name"], "version": version}


async def rollback_state(host: OpnsenseHost) -> dict:
    """What going back would do: the newest OPNexus point that is not running now, and the boot environment that is."""
    if not await supported(host):
        return {"supported": False, "target": None, "current": None}
    rows = await opnsense_client.list_snapshots(host)
    current = next((r for r in rows if "N" in str(r.get("active", ""))), None)  # running now (R alone = next start)
    target = next((p for p in rollback_points(rows) if "N" not in p["active"]), None)
    return {"supported": True, "target": target,
            "current": {"uuid": str(current.get("uuid", "")), "name": str(current.get("name", ""))} if current else None}


async def activate_and_reboot(host: OpnsenseHost, rollback: dict) -> dict:
    """Activate the rollback point, check it is marked for the next start, reboot. If the reboot cannot be handed over,
    the previous boot environment is activated again: an activated old one must never wait for the next unplanned
    reboot."""
    target, current = rollback["target"], rollback["current"]

    async def undo() -> None:
        if current:
            try:
                await opnsense_client.activate_snapshot(host, current["uuid"])
            except OpnsenseAPIError:
                logger.warning("Rückweg auf %s: vorheriges Boot-Environment nicht reaktiviert", host.name)

    await opnsense_client.activate_snapshot(host, target["uuid"])
    try:
        rows = await opnsense_client.list_snapshots(host)
    except OpnsenseAPIError:
        await undo()  # cannot confirm the mark: never leave it waiting for an unplanned reboot
        raise
    marked = next((r for r in rows if str(r.get("uuid", "")) == target["uuid"]), None)
    if marked is None or "R" not in str(marked.get("active", "")):
        await undo()
        raise OpnsenseAPIError(f"Boot-Environment {target['name']} ist nicht für den nächsten Start vorgemerkt")
    try:
        return await opnsense_client.reboot_host(host)
    except OpnsenseAPIError:
        await undo()
        raise


async def create(host: OpnsenseHost, name: str, note: str) -> dict:
    if not NAME.match(name) or name.startswith(ROLLBACK_PREFIX):  # that prefix marks OPNexus' own rollback points
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
