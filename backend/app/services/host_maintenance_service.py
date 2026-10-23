"""Single-host maintenance. Previews are short-lived, bound to state and single use.
An accepted request is NOT proof of completed reboot/update. Explicit human
verification closes the operation; uncertain transport results are never retried.
"""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from app.errors import ErrorCode, api_error
from sqlalchemy import select
from app.models import OpnsenseHost, RuleDeployment, AliasDeployment, PortForwardDeployment, OneToOneDeployment, OutboundNatDeployment
from app.models.host_operation import HostOperation
from app.models.restore_operation import RestoreOperation
from app.services import opnsense_client as client, backup_store, carp_maintenance_window_service
from app.services.deploy_flow import unsettled
from app.services.host_status_service import get_statuses

DEPLOYMENTS = (RuleDeployment, AliasDeployment, PortForwardDeployment, OneToOneDeployment, OutboundNatDeployment)
ACTIVE = ("starting", "submitted", "unknown")
# A restore that's been handed off to a human (manually applying it via the
# OPNsense GUI) is exactly the window where an automated push or reboot/
# update could land mid-restore or on stale config — block both until the
# operator confirms or cancels it. "preview" alone blocks nothing since
# nothing has actually happened yet.
RESTORE_OPEN = ("handed_off",)


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


async def peers(db, host):
    if host.cluster_name:
        return list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.cluster_name == host.cluster_name).order_by(OpnsenseHost.id))).all())
    return [host]


async def ensure_no_open_cleanup(db, hosts):
    """A rule cleanup (1.31) that is running, waiting for its confirmation or unresolved owns the host and its
    cluster peers: nothing else may write to the firewall until it is confirmed, reverted or settled."""
    from app.services.rule_cleanup_service import has_open_cleanup
    ids = set()
    for host in hosts:
        ids.update(h.id for h in await peers(db, host))
    if ids and await has_open_cleanup(db, ids):
        raise api_error(409, ErrorCode.OPEN_CLEANUP_BLOCKS_DEPLOY)


async def ensure_no_open_cleanup_on(db, model, condition):
    """ensure_no_open_cleanup for every host that has a deployment row of ``model`` matching ``condition`` (the
    undeploy paths: deleting a central object removes it from all its firewalls)."""
    ids = set((await db.scalars(select(model.host_id).where(condition))).all())
    hosts = [h for h in [await db.get(OpnsenseHost, i) for i in ids] if h is not None]
    await ensure_no_open_cleanup(db, hosts)


async def ensure_no_maintenance(db, hosts, *, cleanup=True):
    """cleanup=False: only the cleanup service itself, which reports an open cleanup with its own error code."""
    ids = set()
    for host in hosts:
        ids.update(h.id for h in await peers(db, host))
    if ids and await db.scalar(select(HostOperation.id).where(HostOperation.host_id.in_(ids), HostOperation.status.in_(ACTIVE)).limit(1)):
        raise api_error(409, ErrorCode.OPEN_HOST_MAINTENANCE_BLOCKS_DEPLOY)
    if ids and await db.scalar(select(RestoreOperation.id).where(RestoreOperation.host_id.in_(ids), RestoreOperation.status.in_(RESTORE_OPEN)).limit(1)):
        raise api_error(409, ErrorCode.OPEN_RESTORE_BLOCKS_DEPLOY)
    if cleanup and ids:
        await ensure_no_open_cleanup(db, hosts)


async def _shared_change_blockers(db, fleet):
    blocked = []
    fleet_ids = [h.id for h in fleet]
    for model in DEPLOYMENTS:
        if await db.scalar(select(model.id).where(model.host_id.in_(fleet_ids), unsettled(model)).limit(1)):
            blocked.append("Offene Bestätigung oder fehlgeschlagener Rollback im Host/Cluster.")
            break
    operations = list((await db.scalars(select(HostOperation).where(HostOperation.host_id.in_(fleet_ids), HostOperation.status.in_(ACTIVE)))).all())
    if operations:
        blocked.append("Eine frühere Wartungsaktion im Host/Cluster muss zuerst geprüft und abgeschlossen werden.")
    if await db.scalar(select(RestoreOperation.id).where(RestoreOperation.host_id.in_(fleet_ids), RestoreOperation.status.in_(RESTORE_OPEN)).limit(1)):
        blocked.append("Eine Wiederherstellung im Host/Cluster ist offen und muss zuerst bestätigt oder abgebrochen werden.")
    from app.services.rule_cleanup_service import has_open_cleanup
    if await has_open_cleanup(db, fleet_ids):
        blocked.append("Eine Regel-Bereinigung im Host/Cluster ist offen und muss zuerst bestätigt oder zurückgenommen werden.")
    return blocked


async def inspect_host(db, host, *, skip_role_checks=False):
    """skip_role_checks: for the planned CARP handover, where the node is still MASTER when
    it is inspected and becomes BACKUP only afterwards (the role is verified separately)."""
    fleet = await peers(db, host)
    statuses, info, firmware, running = await asyncio.gather(
        get_statuses(fleet), client.get_firmware_info(host), client.get_firmware_status(host), client.get_firmware_running(host))
    blocked = []
    if not host.enabled: blocked.append("Host ist deaktiviert.")
    if statuses.get(host.id, {}).get("status") != "online": blocked.append("Host ist nicht erreichbar.")
    # Fail closed: only the verified ready value permits a disruptive action.
    if running.get("status") != "ready": blocked.append("Firmware-Auftrag läuft oder Status ist unbekannt.")
    if host.cluster_name and not skip_role_checks:
        others = [h for h in fleet if h.id != host.id]
        if statuses.get(host.id, {}).get("carp_status") != "BACKUP":
            blocked.append("Cluster-Wartung nur auf dem CARP-BACKUP. Rollenwechsel separat in OPNsense durchführen.")
        if not others or not all(h.enabled and statuses.get(h.id, {}).get("status") == "online" for h in others) or not any(statuses.get(h.id, {}).get("carp_status") == "MASTER" for h in others):
            blocked.append("Kein erreichbarer aktiver Cluster-Partner bestätigt.")
    blocked.extend(await _shared_change_blockers(db, fleet))
    backups = backup_store.list_snapshots(host.id)
    latest = backups[0] if backups else None
    fresh = bool(latest and latest['size'] > 0 and (datetime.now(timezone.utc)-latest['created_at']).total_seconds() <= 86400)
    snapshot = {
        "host_id": str(host.id), "host_name": host.name, "base_url": host.base_url, "enabled": host.enabled,
        "verify_tls": host.verify_tls, "cluster_name": host.cluster_name,
        "carp_status": statuses.get(host.id, {}).get("carp_status"),
        "carp_maintenance_mode": statuses.get(host.id, {}).get("carp_maintenance_mode", False),
        "version": info.get("product_version", "unbekannt"),
        "running": running.get("status", "unknown"),
        "firmware_status": firmware.get("status", "unknown"), "firmware_message": firmware.get("status_msg", ""),
        "last_check": firmware.get("last_check"), "reboot_required": str(firmware.get("status_reboot", "0")) == "1",
        "packages": firmware.get("all_packages", {}), "upgrade_sets": firmware.get("all_sets", {}), "product_latest": firmware.get("product_latest"),
        "peers": [{"id": str(h.id), "name": h.name, "enabled": h.enabled,
                   "status": statuses.get(h.id, {}).get("status"), "carp_status": statuses.get(h.id, {}).get("carp_status"),
                   "carp_maintenance_mode": statuses.get(h.id, {}).get("carp_maintenance_mode", False)} for h in fleet],
        "backup": {"id": latest['id'], "created_at": latest['created_at'].isoformat(), "size": latest['size']} if latest else None,
        "backup_fresh": fresh, "blocked": blocked,
    }
    backup_blocker = [] if fresh else ["Vor der Installation ein lokales Konfigurationsbackup erstellen (maximal 24 Stunden alt)."]
    snapshot['actions'] = {
        "reboot": blocked,
        "update": blocked + backup_blocker
                          + ([] if firmware.get("status") == "update" else ["Kein stabiles Update zur Installation gemeldet. Update-Suche ausführen."]),
        # Major upgrades reuse every gate the stable update path has (backup
        # freshness, HA ordering, no open deploy/restore) plus the same
        # fail-closed `running == "ready"` check at preview/execute time in
        # maintenance.py - OPNsense's own statusAction() only ever reports
        # "upgrade" once there are zero pending "update" packages, so this
        # can never race ahead of a pending stable update.
        "major_update": blocked + backup_blocker
                          + ([] if firmware.get("status") == "upgrade" else ["Kein Major-Upgrade zur Installation gemeldet. Update-Suche ausführen."]),
    }
    return snapshot


async def inspect_carp_switch(db, host):
    """Return a fail-closed snapshot for the upstream CARP-mode toggle."""
    fleet = await peers(db, host)
    statuses = await get_statuses(fleet)
    blocked = []
    if not host.enabled:
        blocked.append("Host ist deaktiviert.")
    if not host.cluster_name or len(fleet) < 2:
        blocked.append("CARP-Rollenwechsel benötigt einen Cluster mit mindestens zwei Hosts.")
    current = statuses.get(host.id, {})
    if current.get("status") != "online":
        blocked.append("Der ausgewählte Host ist nicht erreichbar.")
    if not current.get("carp_allowed", False):
        blocked.append("CARP ist auf dem ausgewählten Host deaktiviert.")
    maintenance_mode = bool(current.get("carp_maintenance_mode"))
    action = "carp_leave" if maintenance_mode else "carp_enter"
    others = [h for h in fleet if h.id != host.id]
    if action == "carp_enter":
        if current.get("carp_status") != "MASTER":
            blocked.append("Nur der aktuelle CARP-MASTER kann auf den Partner umgeschaltet werden.")
        if not any(h.enabled and statuses.get(h.id, {}).get("status") == "online" and statuses.get(h.id, {}).get("carp_status") == "BACKUP" for h in others):
            blocked.append("Kein erreichbarer CARP-BACKUP als Übernahmeziel bestätigt.")
        already_demoted = next((h for h in others if statuses.get(h.id, {}).get("carp_maintenance_mode")), None)
        if already_demoted:
            # A demoted BACKUP still reports carp_status BACKUP, so the check
            # above alone can't catch this: demoting a second node on top of
            # an already-demoted one leaves both stuck (observed live twice —
            # OPNsense's maintenance toggle then doesn't reliably re-elect,
            # see opnsense/core#7877). The only valid way back is leaving
            # maintenance on the node that's still in it.
            blocked.append(f"{already_demoted.name} steht bereits im CARP-Wartungsmodus. Zuerst dort „CARP-Normalbetrieb herstellen“ verwenden, nicht hier erneut umschalten.")
    elif not any(h.enabled and statuses.get(h.id, {}).get("status") == "online" and statuses.get(h.id, {}).get("carp_status") == "MASTER" for h in others):
        blocked.append("Kein erreichbarer CARP-MASTER bestätigt, der den Verkehr aktuell trägt.")
    running = await asyncio.gather(*(client.get_firmware_running(h) for h in fleet))
    if any(result.get("status") != "ready" for result in running):
        blocked.append("Auf mindestens einem Cluster-Knoten läuft ein Firmware-Auftrag oder sein Zustand ist unbekannt.")
    blocked.extend(await _shared_change_blockers(db, fleet))
    window = await carp_maintenance_window_service.active_window(db, host.id)
    return {
        "host_id": str(host.id), "host_name": host.name, "base_url": host.base_url,
        "cluster_name": host.cluster_name, "action": action,
        "carp_status": current.get("carp_status"),
        "carp_maintenance_mode": maintenance_mode,
        "carp_demotion": current.get("carp_demotion"),
        "peers": [{"id": str(h.id), "name": h.name, "status": statuses.get(h.id, {}).get("status"),
                   "carp_status": statuses.get(h.id, {}).get("carp_status"),
                   "carp_maintenance_mode": statuses.get(h.id, {}).get("carp_maintenance_mode", False)} for h in fleet],
        "blocked": blocked, "actions": {"carp": blocked},
        "auto_revert_window": {"auto_revert_at": window.auto_revert_at.isoformat()} if window and window.auto_revert_at else None,
    }
