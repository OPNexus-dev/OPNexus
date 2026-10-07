"""Five detection functions, each returning {dedup_key: context} for
whatever's currently bad. Pure detection — no delivery, no state tracking
here (see notification_dedup.py for the open/resolved diffing that turns
these snapshots into actual notifications)."""
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alias_deployment import AliasDeployment
from app.models.host import OpnsenseHost
from app.models.host_operation import HostOperation
from app.models.one_to_one_deployment import OneToOneDeployment
from app.models.outbound_nat_deployment import OutboundNatDeployment
from app.models.port_forward_deployment import PortForwardDeployment
from app.models.rule_deployment import RuleDeployment
from app.services import nat_drift_service, rule_drift_service
from app.services.host_status_service import get_statuses
from app.services.live_certificates_service import get_live_certificates
from app.services.opnsense_client import OpnsenseAPIError

# (deployment_type, model, id_attr) — same shape audit_service.py's mapping
# uses, so a revert_failed row can point back at the underlying entity.
_DEPLOYMENT_MODELS = [
    ("rule", RuleDeployment, "rule_id"),
    ("alias", AliasDeployment, "alias_id"),
    ("port_forward", PortForwardDeployment, "port_forward_id"),
    ("outbound_nat", OutboundNatDeployment, "outbound_nat_id"),
    ("one_to_one", OneToOneDeployment, "one_to_one_id"),
]


async def _enabled_hosts(db: AsyncSession) -> list[OpnsenseHost]:
    result = await db.execute(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)))
    return list(result.scalars().all())


async def check_host_offline(db: AsyncSession) -> dict[str, dict]:
    hosts = await _enabled_hosts(db)
    statuses = await get_statuses(hosts)
    problems: dict[str, dict] = {}
    for host in hosts:
        info = statuses.get(host.id, {})
        if info.get("status") != "offline":
            continue
        problems[str(host.id)] = {
            "host_id": str(host.id),
            "host_name": host.name,
            "detail": info.get("status_detail"),
        }
    return problems


async def check_certificates(db: AsyncSession) -> dict[str, dict]:
    hosts = await _enabled_hosts(db)
    problems: dict[str, dict] = {}
    for host in hosts:
        try:
            certs = await get_live_certificates(host)
        except OpnsenseAPIError:
            # Unreachable — already reported by check_host_offline, no need
            # to double-report it here as a certificate problem.
            continue
        for cert in certs:
            if cert.status not in ("expiring_soon", "expired"):
                continue
            key = f"{host.id}:{cert.uuid}"
            problems[key] = {
                "host_id": str(host.id),
                "host_name": host.name,
                "cert_descr": cert.descr,
                "common_name": cert.common_name,
                "status": cert.status,
                "days_until_expiry": cert.days_until_expiry,
            }
    return problems


async def check_rule_drift(db: AsyncSession) -> dict[str, dict]:
    hosts = await _enabled_hosts(db)
    by_cluster: dict[str, list[OpnsenseHost]] = defaultdict(list)
    for host in hosts:
        if host.cluster_name:
            by_cluster[host.cluster_name].append(host)

    problems: dict[str, dict] = {}
    for cluster_name, cluster_hosts in by_cluster.items():
        if len(cluster_hosts) < 2:
            continue
        names_by_id = {h.id: h.name for h in cluster_hosts}
        for row in await rule_drift_service.compute_cluster_drift(cluster_hosts):
            key = f"{cluster_name}:{row['fingerprint']}"
            present_names = sorted(names_by_id[hid] for hid in row["present_on"])
            missing_names = sorted(
                names_by_id[hid] for hid in row["reachable_host_ids"] - row["present_on"]
            )
            problems[key] = {
                "cluster_name": cluster_name,
                "description": row["sample"].description,
                "present_on": present_names,
                "missing_from": missing_names,
            }
    return problems


_NAT_TYPE_LABELS = {"port_forward": "Port-Forward", "one_to_one": "1:1-NAT", "outbound_nat": "Outbound-NAT"}


async def check_nat_drift(db: AsyncSession) -> dict[str, dict]:
    hosts = await _enabled_hosts(db)
    by_cluster: dict[str, list[OpnsenseHost]] = defaultdict(list)
    for host in hosts:
        if host.cluster_name:
            by_cluster[host.cluster_name].append(host)

    problems: dict[str, dict] = {}
    for cluster_name, cluster_hosts in by_cluster.items():
        if len(cluster_hosts) < 2:
            continue
        names_by_id = {h.id: h.name for h in cluster_hosts}
        for nat_type in _NAT_TYPE_LABELS:
            for row in await nat_drift_service.compute_cluster_drift(cluster_hosts, nat_type):
                key = f"{cluster_name}:{nat_type}:{row['fingerprint']}"
                present_names = sorted(names_by_id[hid] for hid in row["present_on"])
                missing_names = sorted(
                    names_by_id[hid] for hid in row["reachable_host_ids"] - row["present_on"]
                )
                problems[key] = {
                    "cluster_name": cluster_name,
                    "nat_type": _NAT_TYPE_LABELS[nat_type],
                    "description": row["sample"].get("descr") or row["sample"].get("description"),
                    "present_on": present_names,
                    "missing_from": missing_names,
                }
    return problems


async def check_revert_failed(db: AsyncSession) -> dict[str, dict]:
    problems: dict[str, dict] = {}
    for deployment_type, model, id_attr in _DEPLOYMENT_MODELS:
        result = await db.execute(select(model).where(model.status == "revert_failed"))
        for deployment in result.scalars().all():
            host = await db.get(OpnsenseHost, deployment.host_id)
            key = f"{deployment_type}:{deployment.id}"
            problems[key] = {
                "deployment_type": deployment_type,
                "deployment_id": str(deployment.id),
                "entity_id": str(getattr(deployment, id_attr)),
                "host_id": str(deployment.host_id),
                "host_name": host.name if host else None,
                "message": deployment.message,
            }
    return problems


async def check_carp_health(db: AsyncSession) -> dict[str, dict]:
    hosts = await _enabled_hosts(db)
    by_cluster: dict[str, list[OpnsenseHost]] = defaultdict(list)
    for host in hosts:
        if host.cluster_name:
            by_cluster[host.cluster_name].append(host)

    problems: dict[str, dict] = {}
    for cluster_name, cluster_hosts in by_cluster.items():
        if len(cluster_hosts) < 2:
            continue
        statuses = await get_statuses(cluster_hosts)
        online = [h for h in cluster_hosts if statuses.get(h.id, {}).get("status") == "online"]
        if len(online) < 2:
            # Can't tell split-brain from a simple outage with only one
            # reachable node — check_host_offline already reports that case.
            continue
        masters = [h for h in online if statuses.get(h.id, {}).get("carp_status") == "MASTER"]
        if len(masters) == 2:
            key = f"{cluster_name}:split_brain"
            problems[key] = {
                "cluster_name": cluster_name,
                "condition": "split_brain",
                "host_names": sorted(h.name for h in online),
            }
        elif len(masters) == 0:
            key = f"{cluster_name}:no_master"
            problems[key] = {
                "cluster_name": cluster_name,
                "condition": "no_master",
                "host_names": sorted(h.name for h in online),
            }
    return problems


async def check_maintenance_unknown(db: AsyncSession) -> dict[str, dict]:
    result = await db.execute(select(HostOperation).where(HostOperation.status == "unknown"))
    problems: dict[str, dict] = {}
    for operation in result.scalars().all():
        problems[str(operation.id)] = {
            "operation_id": str(operation.id),
            "host_id": str(operation.host_id),
            "host_name": operation.host_name,
            "action": operation.action,
            "message": operation.message,
        }
    return problems


async def check_carp_auto_revert_failed(db: AsyncSession) -> dict[str, dict]:
    from app.models.carp_maintenance_window import CarpMaintenanceWindow

    result = await db.execute(select(CarpMaintenanceWindow).where(CarpMaintenanceWindow.status == "revert_failed"))
    problems: dict[str, dict] = {}
    for window in result.scalars().all():
        problems[str(window.id)] = {
            "window_id": str(window.id),
            "host_id": str(window.host_id),
            "host_name": window.host_name,
            "message": window.message,
        }
    return problems
