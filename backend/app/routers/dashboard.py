"""Fleet-wide "what needs my attention" summary for the frontend dashboard
(app/dashboard/page.tsx). Deliberately DB-only — no live OPNsense calls —
so this endpoint stays fast and doesn't fail just because one host is
offline; the dashboard fetches live-only data (certificate expiry, firmware
update availability) itself per host via the existing
/hosts/{id}/certificates and /hosts/{id}/maintenance endpoints, exactly the
way the Certificates and Update Center pages already do, so a slow/offline
host degrades only that one panel instead of the whole page.

"Pending actions" aggregates the same three attention-worthy deployment
statuses ("pending" awaiting confirmation, "error" the deploy call itself
failed, "revert_failed" the rollback call failed) across all deployable
entity kinds (rules, aliases, the three NAT types, policy-hierarchy rules,
IPsec/OpenVPN/WireGuard — the VPN kinds only ever reach "error") plus any host_operations still awaiting manual confirmation
(status in "starting"/"submitted"/"unknown" — mirrors
HostMaintenance.tsx's own definition of "needs a click"). Restore-operation
locks are deliberately NOT included here (no fleet-wide list endpoint
exists for those, only per-host, and it's a rarely-triggered flow) —
a documented v1 gap, not an oversight.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.alias import FirewallAlias
from app.models.alias_deployment import AliasDeployment
from app.models.audit import AuditEvent
from app.models.carp_maintenance_window import CarpMaintenanceWindow
from app.models.host import OpnsenseHost
from app.models.host_operation import HostOperation
from app.models.ipsec_tunnel import IpsecTunnel
from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
from app.models.one_to_one import OneToOneRule
from app.models.one_to_one_deployment import OneToOneDeployment
from app.models.outbound_nat import OutboundNatRule
from app.models.openvpn_instance import OpenVpnInstance
from app.models.openvpn_instance_deployment import OpenVpnInstanceDeployment
from app.models.outbound_nat_deployment import OutboundNatDeployment
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.models.port_forward import PortForwardRule
from app.models.port_forward_deployment import PortForwardDeployment
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from app.models.wireguard_tunnel import WireguardTunnel
from app.models.wireguard_tunnel_deployment import WireguardTunnelDeployment
from app.schemas.dashboard import ActivityItem, DashboardHost, DashboardSummary, PendingAction
from app.services.host_status_service import get_statuses

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

ATTENTION_STATUSES = ("pending", "error", "revert_failed")
STUCK_OPERATION_STATUSES = ("starting", "submitted", "unknown")

OPERATION_LABELS = {
    "reboot": "Neustart",
    "update": "Firmware-Update",
    "major_update": "Major-Upgrade",
    "carp_enter": "CARP-Übergabe",
    "carp_leave": "CARP-Normalbetrieb",
}


async def _rule_actions(db: AsyncSession) -> list[PendingAction]:
    rows = (
        await db.execute(
            select(RuleDeployment, FirewallRule.description, OpnsenseHost.name)
            .join(FirewallRule, RuleDeployment.rule_id == FirewallRule.id)
            .join(OpnsenseHost, RuleDeployment.host_id == OpnsenseHost.id)
            .where(RuleDeployment.status.in_(ATTENTION_STATUSES))
        )
    ).all()
    return [
        PendingAction(
            kind="rule", status=dep.status, entity_name=description, host_name=host_name,
            message=dep.message, created_at=dep.deployed_at, link=f"/rules?rule={dep.rule_id}",
        )
        for dep, description, host_name in rows
    ]


async def _nat_actions(
    db: AsyncSession, deployment_model, rule_model, fk_attr: str, kind: str, nat_type: str
) -> list[PendingAction]:
    fk_column = getattr(deployment_model, fk_attr)
    rows = (
        await db.execute(
            select(deployment_model, rule_model.description, OpnsenseHost.name)
            .join(rule_model, fk_column == rule_model.id)
            .join(OpnsenseHost, deployment_model.host_id == OpnsenseHost.id)
            .where(deployment_model.status.in_(ATTENTION_STATUSES))
        )
    ).all()
    return [
        PendingAction(
            kind=kind, status=dep.status, entity_name=description, host_name=host_name,
            message=dep.message, created_at=dep.deployed_at,
            link=f"/nat?type={nat_type}&rule={getattr(dep, fk_attr)}",
        )
        for dep, description, host_name in rows
    ]


async def _simple_actions(
    db: AsyncSession, deployment_model, parent_model, fk_attr: str, name_column, kind: str, link_template: str
) -> list[PendingAction]:
    """Same shape as _nat_actions for kinds whose entity label is not
    `description` and whose page takes a different query parameter;
    `link_template` is formatted with the parent id."""
    rows = (
        await db.execute(
            select(deployment_model, name_column, OpnsenseHost.name)
            .join(parent_model, getattr(deployment_model, fk_attr) == parent_model.id)
            .join(OpnsenseHost, deployment_model.host_id == OpnsenseHost.id)
            .where(deployment_model.status.in_(ATTENTION_STATUSES))
        )
    ).all()
    return [
        PendingAction(
            kind=kind, status=dep.status, entity_name=name, host_name=host_name,
            message=dep.message, created_at=dep.deployed_at, link=link_template.format(getattr(dep, fk_attr)),
        )
        for dep, name, host_name in rows
    ]


async def _policy_hierarchy_actions(db: AsyncSession) -> list[PendingAction]:
    rows = (
        await db.execute(
            select(PolicyHierarchyRuleDeployment, PolicyHierarchyRule.description,
                   PolicyHierarchyRule.hierarchy_group_id, OpnsenseHost.name)
            .join(PolicyHierarchyRule, PolicyHierarchyRuleDeployment.policy_hierarchy_id == PolicyHierarchyRule.id)
            .join(OpnsenseHost, PolicyHierarchyRuleDeployment.host_id == OpnsenseHost.id)
            .where(PolicyHierarchyRuleDeployment.status.in_(ATTENTION_STATUSES))
        )
    ).all()
    return [
        PendingAction(
            kind="policy_hierarchy", status=dep.status, entity_name=description, host_name=host_name,
            message=dep.message, created_at=dep.deployed_at, link=f"/policy-hierarchy?group={group_id}",
        )
        for dep, description, group_id, host_name in rows
    ]


async def _stuck_host_operations(db: AsyncSession) -> list[PendingAction]:
    rows = (
        await db.scalars(
            select(HostOperation)
            .where(HostOperation.status.in_(STUCK_OPERATION_STATUSES))
            .order_by(HostOperation.created_at.desc())
        )
    ).all()
    return [
        PendingAction(
            kind="host_operation", status=op.status,
            entity_name=OPERATION_LABELS.get(op.action, op.action), host_name=op.host_name,
            message=op.message, created_at=op.created_at, link=f"/hosts/{op.host_id}",
        )
        for op in rows
    ]


async def _carp_auto_revert_actions(db: AsyncSession) -> list[PendingAction]:
    rows = (
        await db.scalars(
            select(CarpMaintenanceWindow)
            .where(CarpMaintenanceWindow.status.in_(("active", "revert_failed")))
            .order_by(CarpMaintenanceWindow.entered_at.desc())
        )
    ).all()
    actions = []
    for window in rows:
        if window.status == "active":
            message = f"Automatischer Rückschalt-Zeitpunkt: {window.auto_revert_at.strftime('%d.%m. %H:%M') if window.auto_revert_at else 'unbekannt'}"
        else:
            message = window.message
        actions.append(PendingAction(
            kind="carp_auto_revert", status=window.status, entity_name="CARP-Wartungsmodus",
            host_name=window.host_name, message=message, created_at=window.entered_at,
            link=f"/hosts/{window.host_id}",
        ))
    return actions


async def collect_pending_actions(db: AsyncSession) -> list[PendingAction]:
    """Everything that needs a human, newest first (shared with the metrics endpoint)."""
    pending_actions: list[PendingAction] = []
    pending_actions += await _rule_actions(db)
    pending_actions += await _nat_actions(db, PortForwardDeployment, PortForwardRule, "port_forward_id", "port_forward", "port-forward")
    pending_actions += await _nat_actions(db, OneToOneDeployment, OneToOneRule, "one_to_one_id", "one_to_one", "one-to-one")
    pending_actions += await _nat_actions(db, OutboundNatDeployment, OutboundNatRule, "outbound_nat_id", "outbound_nat", "outbound-nat")
    pending_actions += await _policy_hierarchy_actions(db)
    pending_actions += await _simple_actions(db, AliasDeployment, FirewallAlias, "alias_id", FirewallAlias.name, "alias", "/aliases?alias={}")
    pending_actions += await _simple_actions(db, IpsecTunnelDeployment, IpsecTunnel, "tunnel_id", IpsecTunnel.description, "ipsec", "/ipsec?tunnel={}")
    pending_actions += await _simple_actions(db, OpenVpnInstanceDeployment, OpenVpnInstance, "instance_id", OpenVpnInstance.description, "openvpn", "/openvpn?instance={}")
    pending_actions += await _simple_actions(db, WireguardTunnelDeployment, WireguardTunnel, "tunnel_id", WireguardTunnel.server_name, "wireguard", "/wireguard?tunnel={}")
    pending_actions += await _stuck_host_operations(db)
    pending_actions += await _carp_auto_revert_actions(db)
    pending_actions.sort(key=lambda item: item.created_at, reverse=True)
    return pending_actions


@router.get("", response_model=DashboardSummary)
async def summary(db: AsyncSession = Depends(get_db)):
    hosts = (await db.scalars(select(OpnsenseHost).order_by(OpnsenseHost.name))).all()
    statuses = await get_statuses(hosts)
    host_summaries = [
        DashboardHost(
            id=host.id, name=host.name, status=statuses.get(host.id, {}).get("status", "offline"),
            carp_status=statuses.get(host.id, {}).get("carp_status"), cluster_name=host.cluster_name,
        )
        for host in hosts
    ]

    pending_actions = await collect_pending_actions(db)

    activity_rows = (
        await db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(15))
    ).all()
    recent_activity = [
        ActivityItem(
            id=row.id, created_at=row.created_at, actor=row.actor, kind=row.kind, action=row.action,
            status=row.status, entity_name=row.entity_name, host_name=row.host_name, message=row.message,
        )
        for row in activity_rows
    ]

    return DashboardSummary(hosts=host_summaries, pending_actions=pending_actions[:30], recent_activity=recent_activity)
