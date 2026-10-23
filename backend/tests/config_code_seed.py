"""Seed data for the configuration-as-code tests: every object kind, hierarchy, placeholders, variables."""
from app.database import SessionLocal
from app.models.alias import FirewallAlias
from app.models.host import OpnsenseHost
from app.models.host_variable import HostVariable
from app.models.one_to_one import OneToOneRule
from app.models.outbound_nat import OutboundNatRule
from app.models.policy import DeviceGroup
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.port_forward import PortForwardRule
from app.models.rule import FirewallRule


async def seed_everything() -> dict:
    """Two hosts, a device group, a hierarchy (Organisation > Nord), and one object of every kind."""
    async with SessionLocal() as db:
        fw1 = OpnsenseHost(name="fw-filiale-1", base_url="https://secret-host.example:8443", api_key="AKIA-SECRET-KEY")
        fw2 = OpnsenseHost(name="fw-filiale-2", base_url="https://other.example", api_key="k2")
        group = DeviceGroup(name="Filialen", description="alle Filialen", hosts=[fw1, fw2])
        org = PolicyHierarchyGroup(name="Organisation", description="")
        db.add_all([fw1, fw2, group, org])
        await db.flush()
        nord = PolicyHierarchyGroup(name="Nord", description="Region Nord", parent_id=org.id)
        db.add(nord)
        await db.flush()
        fw1.policy_hierarchy_group_id = nord.id
        db.add_all([
            HostVariable(host_id=fw1.id, name="LAN_NET", value="10.20.30.0/24"),
            HostVariable(host_id=fw1.id, name="SITE", value="Filiale Nord"),
            FirewallAlias(name="WEB_SERVERS", type="host", description="DMZ", enabled=True,
                          content="10.20.30.10\n203.0.113.20", groups=[group], policy_hierarchy_group_id=None),
            FirewallRule(config_key="allow-dns-lan", description="DNS aus {{SITE}}", interface="lan", direction="in",
                         source_net="{{LAN_NET}}", destination_net="WEB_SERVERS", destination_port="53", protocol="TCP/UDP",
                         groups=[group], targets=[]),
            FirewallRule(config_key="block-all", description="alles sperren", interface="wan", direction="in", action="block",
                         log=True, targets=[fw2], groups=[]),
            PortForwardRule(config_key="pf-web", description="Web DMZ", interface="wan", destination_net="wanip",
                            destination_port="443", target="10.20.30.10", local_port="443", targets=[fw1], groups=[]),
            OneToOneRule(config_key="nat1-mail", description="Mail", interface="wan", external="203.0.113.25",
                         source_net="10.20.30.25", destination_net="any", groups=[group], targets=[]),
            OutboundNatRule(config_key="out-lan", description="LAN raus", interface="wan", protocol="any",
                            source_net="{{LAN_NET}}", destination_net="any", target="wanip", groups=[group], targets=[],
                            policy_hierarchy_group_id=None),
            PolicyHierarchyRule(config_key="ph-block-bogons", hierarchy_group_id=org.id, rulebase="pre", sequence=0,
                                description="Bogons", interface="wan", direction="in", action="block", source_net="bogons"),
            PolicyHierarchyRule(config_key="ph-allow-mgmt", hierarchy_group_id=org.id, rulebase="pre", sequence=1,
                                description="Mgmt", interface="lan", direction="in", action="pass"),
            PolicyHierarchyRule(config_key="ph-post-deny", hierarchy_group_id=nord.id, rulebase="post", sequence=0,
                                description="Standard sperren", interface="lan", direction="in", action="block"),
        ])
        await db.commit()
    return {"fw1": "fw-filiale-1", "fw2": "fw-filiale-2"}
