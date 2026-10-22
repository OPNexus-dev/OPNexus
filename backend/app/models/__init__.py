from app.models.host import OpnsenseHost
from app.models.rule_target import rule_targets
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from app.models.alias_target import alias_targets
from app.models.alias import FirewallAlias
from app.models.alias_deployment import AliasDeployment
from app.models.port_forward_target import port_forward_targets
from app.models.port_forward import PortForwardRule
from app.models.port_forward_deployment import PortForwardDeployment
from app.models.user import AdminUser, Session

__all__ = [
    "OpnsenseHost", "FirewallRule", "RuleDeployment", "rule_targets",
    "FirewallAlias", "AliasDeployment", "alias_targets",
    "PortForwardRule", "PortForwardDeployment", "port_forward_targets",
    "AdminUser", "Session",
]

from app.models.policy import DeviceGroup, RuleRevision, AliasRevision, PortForwardRevision

from app.models.one_to_one import OneToOneRule
from app.models.one_to_one_deployment import OneToOneDeployment
from app.models.one_to_one_target import one_to_one_targets
from app.models.policy import OneToOneRevision
__all__ += ["OneToOneRule", "OneToOneDeployment", "one_to_one_targets", "OneToOneRevision"]

from app.models.outbound_nat import OutboundNatRule
from app.models.outbound_nat_deployment import OutboundNatDeployment
from app.models.outbound_nat_target import outbound_nat_targets
from app.models.policy import OutboundNatRevision
__all__ += ["OutboundNatRule", "OutboundNatDeployment", "outbound_nat_targets", "OutboundNatRevision"]

from app.models.audit import AuditEvent
__all__ += ["AuditEvent"]

from app.models.host_operation import HostOperation
__all__ += ["HostOperation"]

from app.models.preferences import UserPreferences
__all__ += ["UserPreferences"]

from app.models.notification import NotificationSettings, NotificationProblem
__all__ += ["NotificationSettings", "NotificationProblem"]

from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.models.policy import PolicyHierarchyRuleRevision
__all__ += ["PolicyHierarchyGroup", "PolicyHierarchyRule", "PolicyHierarchyRuleDeployment", "PolicyHierarchyRuleRevision"]

from app.models.log_entry import FirewallLogEntry
from app.models.log_cursor import HostLogCursor
from app.models.log_settings import LogSettings
__all__ += ["FirewallLogEntry", "HostLogCursor", "LogSettings"]

from app.models.ipsec_tunnel_target import ipsec_tunnel_targets
from app.models.ipsec_tunnel import IpsecTunnel
from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
from app.models.policy import ipsec_tunnel_groups, IpsecTunnelRevision

from app.models.openvpn_instance_target import openvpn_instance_targets
from app.models.openvpn_instance import OpenVpnInstance
from app.models.openvpn_instance_deployment import OpenVpnInstanceDeployment
from app.models.policy import openvpn_instance_groups, OpenVpnInstanceRevision
__all__ += ["IpsecTunnel", "IpsecTunnelDeployment", "ipsec_tunnel_targets", "ipsec_tunnel_groups", "IpsecTunnelRevision"]
__all__ += ["OpenVpnInstance", "OpenVpnInstanceDeployment", "openvpn_instance_targets", "openvpn_instance_groups", "OpenVpnInstanceRevision"]

from app.models.wireguard_tunnel_target import wireguard_tunnel_targets
from app.models.wireguard_tunnel import WireguardTunnel
from app.models.wireguard_tunnel_deployment import WireguardTunnelDeployment
from app.models.policy import wireguard_tunnel_groups, WireguardTunnelRevision
__all__ += ["WireguardTunnel", "WireguardTunnelDeployment", "wireguard_tunnel_targets", "wireguard_tunnel_groups", "WireguardTunnelRevision"]

# Trust-Store + ACME certificate management — deliberately per-host (plain
# host_id FK, no DeviceGroup/effective_hosts targeting) — see
# ManagedCertificate/AcmeAccount/AcmeCertificateRequest's own docstrings
# for why.
from app.models.managed_certificate import ManagedCertificate
from app.models.acme_account import AcmeAccount
from app.models.acme_certificate_request import AcmeCertificateRequest
__all__ += ["ManagedCertificate", "AcmeAccount", "AcmeCertificateRequest"]

from app.models.restore_operation import RestoreOperation
__all__ += ["RestoreOperation"]

from app.models.carp_event import CarpEvent
from app.models.carp_maintenance_window import CarpMaintenanceWindow
__all__ += ["CarpEvent", "CarpMaintenanceWindow"]

from app.models.resource_sample import HostResourceSample
__all__ += ["HostResourceSample"]

from app.models.scheduled_maintenance import ScheduledMaintenance
__all__ += ["ScheduledMaintenance"]

from app.models.maintenance_window import MaintenanceWindow, MaintenanceWindowException
__all__ += ["MaintenanceWindow", "MaintenanceWindowException"]

from app.models.rollout import RolloutPlan, RolloutPlanWave, RolloutRun, RolloutRunHost, RolloutRunWave, rollout_wave_groups
__all__ += ["RolloutPlan", "RolloutPlanWave", "RolloutRun", "RolloutRunHost", "RolloutRunWave", "rollout_wave_groups"]

from app.models.gui_access import GuiAccessSettings, GuiSession, GuiTicket
__all__ += ["GuiAccessSettings", "GuiSession", "GuiTicket"]

from app.models.rule_history import LiveRuleState, RuleChangeEvent
__all__ += ["LiveRuleState", "RuleChangeEvent"]

from app.models.rule_cleanup import RuleCleanupOperation, RuleCleanupItem
__all__ += ["RuleCleanupOperation", "RuleCleanupItem"]

# Every mapped class must be registered here: a script that imports only app.models (python -m app.reset_password,
# app.reset_2fa) otherwise fails at the first query because OpnsenseHost refers to classes nobody imported.
from app.models.host_variable import HostVariable
__all__ += ["HostVariable"]
