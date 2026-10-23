"""Kanonische Darstellung der zentralen Objekte und deren YAML-Text.

`canonical(db)` liefert dieselbe Form, die der Lader aus einer Datei erzeugt (Feldnamen und -folge des Schemas). Der
Planer vergleicht beide; der Export schreibt sie nur aus. Deterministisch: feste Abschnittsfolge, Objekte nach Schlüssel,
Ziel- und Mitgliederlisten sortiert, alle Felder immer (auch Defaults und `null`), kein Zeitstempel.
"""
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config_code import schema
from app.models.alias import FirewallAlias
from app.models.host import OpnsenseHost
from app.models.one_to_one import OneToOneRule
from app.models.outbound_nat import OutboundNatRule
from app.models.policy import DeviceGroup
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.port_forward import PortForwardRule
from app.models.rule import FirewallRule
from app.version import APP_VERSION

HEADER = (
    "# OPNexus-Konfiguration (Schema v{schema}), exportiert von OPNexus {version}.\n"
    "# Enthält keine Zugänge zu Firewalls, Benutzer, Tokens oder Schlüssel. Prüfen: opnexus apply DATEI --dry-run\n"
)


def _ordered(doc: type[schema._Doc], values: dict) -> dict:
    """Values in the field order of the schema model; a missing or extra field is a bug in the exporter."""
    fields = list(doc.model_fields)
    if set(values) != set(fields):
        raise RuntimeError(f"{doc.__name__}: exporter fields differ from schema ({sorted(set(values) ^ set(fields))})")
    return {name: values[name] for name in fields}


def _targets(doc: type[schema._Doc], obj, hierarchy_names: dict) -> dict:
    values = {"hosts": sorted(h.name for h in obj.targets), "groups": sorted(g.name for g in obj.groups)}
    if "hierarchy_group" in doc.model_fields:
        values["hierarchy_group"] = hierarchy_names.get(obj.policy_hierarchy_group_id)
    return _ordered(doc, values)


def _flat(doc: type[schema._Doc], obj, **extra) -> dict:
    """Scalar columns of `obj` for every schema field that is not computed here (`key` comes from `config_key`)."""
    values = dict(extra)
    for name in doc.model_fields:
        if name in values:
            continue
        values[name] = obj.config_key if name == "key" else getattr(obj, name)
    return _ordered(doc, values)


def _hierarchy_order(groups: list[PolicyHierarchyGroup]) -> list[PolicyHierarchyGroup]:
    """Parents before children, siblings by name; the same rule the loader expects."""
    children: dict = {}
    for group in groups:
        children.setdefault(group.parent_id, []).append(group)
    ordered: list[PolicyHierarchyGroup] = []

    def walk(parent_id) -> None:
        for group in sorted(children.get(parent_id, []), key=lambda g: g.name):
            ordered.append(group)
            walk(group.id)

    walk(None)
    return ordered


async def canonical(db: AsyncSession, kinds: set[str] | None = None) -> dict[str, list | dict]:
    """{kind: content} for the requested kinds (default all), in file order."""
    wanted = set(schema.KINDS) if kinds is None else kinds
    out: dict[str, list | dict] = {}

    all_groups = (await db.execute(select(PolicyHierarchyGroup).options(selectinload(PolicyHierarchyGroup.hosts)))).scalars().all()
    hierarchy_names = {g.id: g.name for g in all_groups}

    if "device_groups" in wanted:
        groups = (await db.execute(select(DeviceGroup).options(selectinload(DeviceGroup.hosts)))).scalars().all()
        out["device_groups"] = [
            _ordered(schema.DeviceGroupDoc, {"name": g.name, "description": g.description or "", "hosts": sorted(h.name for h in g.hosts)})
            for g in sorted(groups, key=lambda g: g.name)
        ]

    if "policy_hierarchy_groups" in wanted:
        out["policy_hierarchy_groups"] = [
            _ordered(schema.HierarchyGroupDoc, {
                "name": g.name,
                "parent": hierarchy_names.get(g.parent_id),
                "description": g.description or "",
                "hosts": sorted(h.name for h in g.hosts),
            })
            for g in _hierarchy_order(list(all_groups))
        ]

    if "aliases" in wanted:
        aliases = (await db.execute(
            select(FirewallAlias).options(selectinload(FirewallAlias.targets), selectinload(FirewallAlias.groups))
        )).scalars().all()
        out["aliases"] = [
            _ordered(schema.AliasDoc, {
                "name": a.name,
                "type": a.type,
                "description": a.description or "",
                "enabled": a.enabled,
                "entries": [line for line in (a.content or "").split("\n") if line],
                "targets": _targets(schema.TargetsWithHierarchy, a, hierarchy_names),
            })
            for a in sorted(aliases, key=lambda a: a.name)
        ]

    simple = (
        ("rules", FirewallRule, schema.RuleDoc, schema.Targets),
        ("port_forwards", PortForwardRule, schema.PortForwardDoc, schema.TargetsWithHierarchy),
        ("one_to_one", OneToOneRule, schema.OneToOneDoc, schema.TargetsWithHierarchy),
        ("outbound_nat", OutboundNatRule, schema.OutboundNatDoc, schema.TargetsWithHierarchy),
    )
    for kind, model, doc, targets_doc in simple:
        if kind not in wanted:
            continue
        rows = (await db.execute(select(model).options(selectinload(model.targets), selectinload(model.groups)))).scalars().all()
        out[kind] = [
            _flat(doc, row, targets=_targets(targets_doc, row, hierarchy_names))
            for row in sorted(rows, key=lambda r: r.config_key)
        ]

    if "policy_hierarchy_rules" in wanted:
        rules = (await db.execute(select(PolicyHierarchyRule))).scalars().all()
        group_order = {g.id: i for i, g in enumerate(_hierarchy_order(list(all_groups)))}
        out["policy_hierarchy_rules"] = [
            _flat(schema.HierarchyRuleDoc, r, group=hierarchy_names[r.hierarchy_group_id])
            for r in sorted(rules, key=lambda r: (group_order[r.hierarchy_group_id], r.rulebase != "pre", r.sequence, str(r.id)))  # deploy order
        ]

    if "variables" in wanted:
        hosts = (await db.execute(select(OpnsenseHost).options(selectinload(OpnsenseHost.variables)))).scalars().all()
        out["variables"] = {
            h.name: {v.name: v.value for v in sorted(h.variables, key=lambda v: v.name)}
            for h in sorted(hosts, key=lambda h: h.name)
            if h.variables
        }
    return out


def dump_yaml(content: dict[str, list | dict]) -> str:
    """The file text: fixed header, `opnexus_config: 1`, then the sections in schema order."""
    document = {"opnexus_config": schema.SCHEMA_VERSION, **{kind: content[kind] for kind in schema.KINDS if kind in content}}
    body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=True, width=4096)
    return HEADER.format(schema=schema.SCHEMA_VERSION, version=APP_VERSION) + body


async def export_yaml(db: AsyncSession, kinds: set[str] | None = None) -> str:
    return dump_yaml(await canonical(db, kinds))
