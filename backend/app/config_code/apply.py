"""Apply: einen geprüften Plan in einer Transaktion in die Datenbank schreiben. Nie auf eine Firewall.

Ablauf: `policy_lock`, Plan neu berechnen, Token vergleichen (sonst PlanStale), blockierte Pläne ablehnen, schreiben
(Hierarchiegruppen → Gerätegruppen → Aliase → Regeln → NAT → Hierarchie-Regeln → Host-Zuordnung → Variablen,
Löschungen umgekehrt), Revisionen und Audit wie in der UI, dann Selbstprüfung: die Datenbank muss danach genau dem
erwarteten Zustand des Plans entsprechen, sonst SelfCheckFailed (der Aufrufer rollt zurück).

Dieses Modul importiert bewusst weder `opnsense_client` noch einen `*_deploy_service` (test_config_code_apply prüft das).
Es legt keine Deployment-Zeile an und ruft kein `undeploy_*`: gelöscht werden nur Objekte ohne jedes Deployment.
Den Commit macht der Aufrufer.
"""
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config_code import plan as planner
from app.config_code.export import canonical
from app.config_code.loader import Parsed
from app.models.alias import FirewallAlias
from app.models.host import OpnsenseHost
from app.models.host_variable import HostVariable
from app.models.one_to_one import OneToOneRule
from app.models.outbound_nat import OutboundNatRule
from app.models.policy import (AliasRevision, DeviceGroup, OneToOneRevision, OutboundNatRevision,
                               PolicyHierarchyRuleRevision, PortForwardRevision, RuleRevision)
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.port_forward import PortForwardRule
from app.models.rule import FirewallRule
from app.services import audit_service

NOTE = "Import (Konfiguration als Code)"
AUDIT_KIND = {
    "device_groups": "group", "policy_hierarchy_groups": "policy_hierarchy_group", "aliases": "alias", "rules": "rule",
    "port_forwards": "port_forward", "one_to_one": "one_to_one", "outbound_nat": "outbound_nat",
    "policy_hierarchy_rules": "policy_hierarchy", "variables": "variable",
}


class PlanStale(Exception):
    pass


class PlanBlocked(Exception):
    def __init__(self, plan: planner.Plan):
        self.plan = plan
        super().__init__("plan blocked")


class SelfCheckFailed(Exception):
    def __init__(self, differences: list[str]):
        self.differences = differences
        super().__init__("; ".join(differences[:5]))


def _revision_writer(kind: str):
    """(revision model, foreign key, snapshot function) as the UI uses them, imported lazily per kind.

    The *_policy_service modules import their deploy services (for previews); importing them only here keeps the
    import of this module free of any firewall client, and nothing here calls into them except `snapshot`.
    """
    if kind == "rules":
        from app.services.policy_service import snapshot
        return RuleRevision, "rule_id", snapshot
    if kind == "aliases":
        from app.services.alias_policy_service import snapshot
        return AliasRevision, "alias_id", snapshot
    if kind == "port_forwards":
        from app.services.port_forward_policy_service import snapshot
        return PortForwardRevision, "port_forward_id", snapshot
    if kind == "one_to_one":
        from app.services.one_to_one_policy_service import snapshot
        return OneToOneRevision, "one_to_one_id", snapshot
    if kind == "outbound_nat":
        from app.services.outbound_nat_policy_service import snapshot
        return OutboundNatRevision, "outbound_nat_id", snapshot
    from app.services.policy_hierarchy_service import snapshot
    return PolicyHierarchyRuleRevision, "policy_hierarchy_id", snapshot


MODELS = {"aliases": FirewallAlias, "rules": FirewallRule, "port_forwards": PortForwardRule, "one_to_one": OneToOneRule,
          "outbound_nat": OutboundNatRule, "policy_hierarchy_rules": PolicyHierarchyRule}
SCALARS = {
    "aliases": ("name", "type", "description", "enabled"),
    "rules": ("description", "interface", "direction", "ipprotocol", "protocol", "action", "source_net", "source_port",
              "destination_net", "destination_port", "enabled", "log"),
    "port_forwards": ("description", "interface", "ipprotocol", "protocol", "source_net", "source_port", "destination_net",
                      "destination_port", "target", "local_port", "natreflection", "enabled"),
    "one_to_one": ("description", "interface", "type", "external", "source_net", "destination_net", "source_not",
                   "destination_not", "natreflection", "log", "enabled"),
    "outbound_nat": ("description", "interface", "ipprotocol", "protocol", "source_net", "source_port", "destination_net",
                     "destination_port", "source_not", "destination_not", "target", "target_port", "enabled"),
    "policy_hierarchy_rules": ("rulebase", "description", "interface", "direction", "ipprotocol", "protocol", "action",
                               "source_net", "source_port", "destination_net", "destination_port", "enabled"),
}


class _Writer:
    def __init__(self, db: AsyncSession, plan: planner.Plan):
        self.db = db
        self.plan = plan
        self.now = datetime.now(timezone.utc)
        self.events: list[dict] = []

    async def load(self) -> None:
        db = self.db
        self.hosts = {h.name: h for h in (await db.execute(select(OpnsenseHost))).scalars().all()}
        self.groups = {g.name: g for g in (await db.execute(
            select(DeviceGroup).options(selectinload(DeviceGroup.hosts)))).scalars().all()}
        self.hierarchy = {g.name: g for g in (await db.execute(select(PolicyHierarchyGroup))).scalars().all()}
        self.rows: dict[str, dict] = {}
        for kind, model in MODELS.items():
            options = [selectinload(model.deployments)]
            if kind != "policy_hierarchy_rules":
                options += [selectinload(model.targets), selectinload(model.groups)]
            rows = (await db.execute(select(model).options(*options))).scalars().all()
            self.rows[kind] = {(r.name if kind == "aliases" else r.config_key): r for r in rows}

    def items(self, kind: str, *actions: str) -> list[planner.Item]:
        return [i for i in self.plan.items if i.kind == kind and i.action in actions]

    def event(self, item: planner.Item, entity_id, name: str) -> None:
        self.events.append({"kind": AUDIT_KIND[item.kind], "action": f"import_{item.action}", "status": "success",
                            "entity_id": str(entity_id) if entity_id else None, "entity_name": name,
                            "details": {"key": item.key, "fields": [c["field"] for c in item.changes]}})

    # -- groups ----------------------------------------------------------------------------------------------------------
    async def hierarchy_groups(self) -> None:
        items = self.items("policy_hierarchy_groups", "create", "update")
        for item in items:
            if item.action == "create":
                group = PolicyHierarchyGroup(name=item.key, description=item.after["description"])
                self.db.add(group)
                self.hierarchy[item.key] = group
        await self.db.flush()
        for item in items:
            group = self.hierarchy[item.key]
            group.description = item.after["description"]
            parent = item.after["parent"]
            group.parent_id = self.hierarchy[parent].id if parent else None
            self.event(item, group.id, group.name)
        await self.db.flush()

    async def device_groups(self) -> None:
        for item in self.items("device_groups", "create", "update"):
            group = self.groups.get(item.key)
            if group is None:
                group = DeviceGroup(name=item.key)
                self.db.add(group)
                self.groups[item.key] = group
            group.description = item.after["description"]
            group.hosts = [self.hosts[n] for n in item.after["hosts"]]
            await self.db.flush()
            self.event(item, group.id, group.name)

    # -- objects ---------------------------------------------------------------------------------------------------------
    async def objects(self, kind: str) -> None:
        model = MODELS[kind]
        revision_model, fk, snapshot = _revision_writer(kind)
        for item in self.items(kind, "create", "update"):
            after = item.after
            row = self.rows[kind].get(item.key)
            if row is None:
                row = model(targets=[], groups=[]) if kind != "policy_hierarchy_rules" else model()
                if kind != "aliases":
                    row.config_key = item.key
                self.db.add(row)
                self.rows[kind][item.key] = row
            elif not await self.db.scalar(select(revision_model.id).where(getattr(revision_model, fk) == row.id).limit(1)):
                self.db.add(revision_model(**{fk: row.id}, note="Vor erster Bearbeitung", snapshot=snapshot(row)))
            for name in SCALARS[kind]:
                setattr(row, name, after[name])
            if kind == "aliases":
                row.content = "\n".join(after["entries"])
            if kind == "policy_hierarchy_rules":
                row.hierarchy_group_id = self.hierarchy[after["group"]].id
            else:
                row.targets = [self.hosts[n] for n in after["targets"]["hosts"]]
                row.groups = [self.groups[n] for n in after["targets"]["groups"]]
                if "hierarchy_group" in after["targets"]:
                    hierarchy = after["targets"]["hierarchy_group"]
                    row.policy_hierarchy_group_id = self.hierarchy[hierarchy].id if hierarchy else None
            row.updated_at = self.now  # also for target-only changes: invalidates optimistic edits open in the UI
            await self.db.flush()
            self.db.add(revision_model(**{fk: row.id}, note=NOTE, snapshot=snapshot(row)))
            self.event(item, row.id, row.name if kind == "aliases" else row.description)

    async def hierarchy_positions(self) -> None:
        """sequence = rank for every rule in a list the file touches (contiguous 0..n-1, same order as the plan)."""
        if "policy_hierarchy_rules" not in self.plan.touched:
            return
        deleted = {i.key for i in self.items("policy_hierarchy_rules", "delete")}
        for key, rank in self.plan.positions.items():
            row = self.rows["policy_hierarchy_rules"].get(key)
            if row is not None and key not in deleted and row.sequence != rank:
                row.sequence = rank
        await self.db.flush()

    async def host_hierarchy(self) -> None:
        for host_name, group_name in self.plan.host_hierarchy.items():
            host = self.hosts[host_name]
            wanted = self.hierarchy[group_name].id if group_name else None
            if host.policy_hierarchy_group_id != wanted:
                host.policy_hierarchy_group_id = wanted
        await self.db.flush()

    async def variables(self) -> None:
        if "variables" not in self.plan.touched:
            return
        existing = {(v.host_id, v.name): v for v in (await self.db.execute(select(HostVariable))).scalars().all()}
        for item in self.items("variables", "create", "update"):
            host = self.hosts[item.after["host"]]
            row = existing.get((host.id, item.after["name"]))
            if row is None:
                self.db.add(HostVariable(host_id=host.id, name=item.after["name"], value=item.after["value"]))
            else:
                row.value = item.after["value"]
            self.event(item, None, item.key)
        for item in self.items("variables", "delete"):
            host = self.hosts[item.before["host"]]
            await self.db.execute(delete(HostVariable).where(HostVariable.host_id == host.id, HostVariable.name == item.before["name"]))
            self.event(item, None, item.key)
        await self.db.flush()

    # -- deletions -------------------------------------------------------------------------------------------------------
    async def deletions(self) -> None:
        for kind in ("policy_hierarchy_rules", "outbound_nat", "one_to_one", "port_forwards", "rules", "aliases"):
            for item in self.items(kind, "delete"):
                row = self.rows[kind][item.key]
                if row.deployments:  # the planner blocks this; never undeploy from here
                    raise RuntimeError(f"{kind} {item.key}: delete with deployments")
                self.event(item, row.id, row.name if kind == "aliases" else row.description)
                await self.db.delete(row)
        await self.db.flush()
        for item in self.items("device_groups", "delete"):
            row = self.groups[item.key]
            self.event(item, row.id, row.name)
            await self.db.delete(row)
        await self.db.flush()
        doomed = [self.hierarchy[i.key] for i in self.items("policy_hierarchy_groups", "delete")]
        by_id = {g.id: g for g in self.hierarchy.values()}

        def depth(group) -> int:
            n, current = 0, group
            while current.parent_id is not None and current.parent_id in by_id and n < 1000:
                n, current = n + 1, by_id[current.parent_id]
            return n

        for row in sorted(doomed, key=depth, reverse=True):  # children before parents (parent_id is RESTRICT)
            item = next(i for i in self.items("policy_hierarchy_groups", "delete") if i.key == row.name)
            self.event(item, row.id, row.name)
            await self.db.delete(row)
            await self.db.flush()


async def _self_check(db: AsyncSession, plan: planner.Plan) -> None:
    db.expire_all()  # compare what is in the database, not what the session believes
    actual = await canonical(db, set(plan.touched))
    differences: list[str] = []
    for kind in plan.touched:
        if kind == "variables":
            got = {f"{h}/{n}": {"key": f"{h}/{n}", "host": h, "name": n, "value": v}
                   for h, values in actual["variables"].items() for n, v in values.items()}
        else:
            ident = planner.KIND_SPECS[kind].ident
            got = {item[ident]: item for item in actual[kind]}
        want = plan.expected[kind]
        for key in sorted(set(got) | set(want)):
            if got.get(key) != want.get(key):
                differences.append(f"{kind}:{key}")
    if "policy_hierarchy_rules" in plan.touched:
        rows = (await db.execute(select(PolicyHierarchyRule).options(selectinload(PolicyHierarchyRule.group)))).scalars().all()
        ranks = planner._ranks(rows, lambda r: r.config_key, lambda r: (r.group.name, r.rulebase), lambda r: (r.sequence, str(r.id)))
        for key, rank in ranks.items():
            if plan.positions.get(key) != rank:
                differences.append(f"policy_hierarchy_rules:{key}:position")
    if differences:
        raise SelfCheckFailed(differences)


async def apply_config(db: AsyncSession, parsed: Parsed, prune: bool, token: str) -> planner.Plan:
    """Writes the plan into the session (flushed, not committed). Raises PlanStale, PlanBlocked, SelfCheckFailed or
    ConfigError; on any exception the caller must roll back."""
    from app.services.policy_service import policy_lock

    await policy_lock(db)
    plan = await planner.build_plan(db, parsed, prune)
    if plan.token != token:
        raise PlanStale()
    if not plan.applicable:
        raise PlanBlocked(plan)
    writer = _Writer(db, plan)
    await writer.load()
    await writer.hierarchy_groups()
    await writer.device_groups()
    for kind in ("aliases", "rules", "port_forwards", "one_to_one", "outbound_nat", "policy_hierarchy_rules"):
        if kind in plan.touched:
            await writer.objects(kind)
    await writer.hierarchy_positions()
    await writer.host_hierarchy()
    await writer.variables()
    await writer.deletions()
    await _self_check(db, plan)
    db.add(audit_service.make_event(kind="config", action="import", status="success", entity_name=parsed.sha256[:16],
                                    details={"sha256": parsed.sha256, "prune": prune, "kinds": plan.touched,
                                             "summary": plan.summary()}))
    for values in writer.events:
        db.add(audit_service.make_event(**values))
    await db.flush()
    return plan
