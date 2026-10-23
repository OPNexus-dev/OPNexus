"""Planer: Datei gegen Datenbank, ohne etwas zu schreiben und ohne Firewall-Kontakt.

Der Plan ist ein reiner Inhaltsvergleich der kanonischen Formen (export.canonical und die Datei) je Art und Schlüssel:
`create`, `update` (Feldänderungen vorher/nachher), `delete` (nur mit `prune`), `unchanged`, und Gründe, die ein Objekt
blockieren. Ein Abschnitt, der in der Datei fehlt, lässt die Art unberührt; ein vorhandener, auch leerer Abschnitt plus
`prune` gleicht die Art ab.

Fehler in der Datei (unbekannter Host, Zyklus, doppelter Schlüssel, Platzhalter in Hierarchie-Regeln …) sind
ConfigError (422). Zustände in der Datenbank, die das Übernehmen verhindern (offene Deployments, Prune von
ausgerollten oder noch referenzierten Objekten), sind `blocked`: der Plan wird gezeigt, ist aber nicht anwendbar.

Das Plan-Token ist ein SHA-256 über Datei, `prune` und alles in der Datenbank, wovon das Ergebnis abhängt. Apply
rechnet den Plan unter `policy_lock` neu und vergleicht das Token (`CONFIG_PLAN_STALE`).
"""
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config_code import schema
from app.config_code.export import canonical
from app.config_code.loader import MAX_VARIABLES_PER_HOST, ConfigError, Parsed, _line_for, _path, display
from app.database import Base
from app.models.alias import FirewallAlias
from app.models.host import OpnsenseHost
from app.models.one_to_one import OneToOneRule
from app.models.outbound_nat import OutboundNatRule
from app.models.policy import DeviceGroup
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.port_forward import PortForwardRule
from app.models.rule import FirewallRule
from app.schemas.alias import AliasCreate
from app.schemas.one_to_one import OneToOneCreate
from app.schemas.outbound_nat import OutboundNatCreate
from app.schemas.policy_hierarchy import PolicyHierarchyRuleCreate
from app.schemas.port_forward import PortForwardCreate
from app.schemas.rule import RuleCreate
from app.services.variables_service import names_in

BUSY = ("pending", "revert_failed")
DEPLOYED = ("success", "pending", "revert_failed")


@dataclass(frozen=True)
class Kind:
    section: str
    model: type | None
    ident: str  # identity field in the file ("key"/"name")
    api: type | None = None  # API create schema, run on every file object (same validators as the UI)
    deployable: bool = False


KIND_SPECS = {
    "device_groups": Kind("device_groups", DeviceGroup, "name"),
    "policy_hierarchy_groups": Kind("policy_hierarchy_groups", PolicyHierarchyGroup, "name"),
    "aliases": Kind("aliases", FirewallAlias, "name", AliasCreate, True),
    "rules": Kind("rules", FirewallRule, "key", RuleCreate, True),
    "port_forwards": Kind("port_forwards", PortForwardRule, "key", PortForwardCreate, True),
    "one_to_one": Kind("one_to_one", OneToOneRule, "key", OneToOneCreate, True),
    "outbound_nat": Kind("outbound_nat", OutboundNatRule, "key", OutboundNatCreate, True),
    "policy_hierarchy_rules": Kind("policy_hierarchy_rules", PolicyHierarchyRule, "key", PolicyHierarchyRuleCreate, True),
    "variables": Kind("variables", None, "key"),
}
TARGET_KINDS = ("aliases", "rules", "port_forwards", "one_to_one", "outbound_nat")
HIERARCHY_TARGET_KINDS = ("aliases", "port_forwards", "one_to_one", "outbound_nat")
RULE_KINDS = ("rules", "port_forwards", "one_to_one", "outbound_nat", "policy_hierarchy_rules")
# Fields whose value may name an alias (OPNsense resolves aliases by name in these).
ALIAS_REF_FIELDS = ("source_net", "destination_net", "source_port", "destination_port", "target", "local_port", "target_port", "external")
# Values in those fields that are not aliases: keywords, interface names/addresses, OPNsense's built-in aliases.
_NOT_ALIAS = re.compile(r"^(?:any|\(self\)|(?:lan|wan|lo0|opt\d+)(?:ip|net)?|wanip|lanip|bogons|bogonsv6|sshlockout|virusprot|__.*)$", re.I)
_ALIAS_LIKE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Item:
    kind: str
    key: str
    action: str  # create | update | delete | unchanged
    before: dict | None = None
    after: dict | None = None
    changes: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    deployed_hosts: int = 0
    implicit: bool = False  # not in the file, changed only as a consequence (position, hierarchy members)
    db_id: uuid.UUID | None = None

    def public(self) -> dict:
        return {
            "kind": self.kind,
            "key": self.key,
            "action": "blocked" if self.reasons else self.action,
            "planned_action": self.action,
            "changes": self.changes,
            "deployed_hosts": self.deployed_hosts,
            "reasons": self.reasons,
            "implicit": self.implicit,
        }


@dataclass
class Plan:
    sha256: str
    prune: bool
    touched: list[str]
    items: list[Item]
    warnings: list[dict]
    token: str = ""
    # For apply and its self-check: the complete expected state of every touched kind, keyed by identity.
    expected: dict = field(default_factory=dict)
    positions: dict = field(default_factory=dict)  # hierarchy rule key -> rank in its (group, rulebase)
    host_hierarchy: dict = field(default_factory=dict)  # host name -> hierarchy group name (only when that section is touched)
    file_objects: dict = field(default_factory=dict)  # kind -> {identity: file dict}

    @property
    def applicable(self) -> bool:
        return not any(i.reasons for i in self.items)

    def summary(self) -> dict:
        out = {"create": 0, "update": 0, "delete": 0, "unchanged": 0, "blocked": 0}
        for item in self.items:
            out["blocked" if item.reasons else item.action] += 1
        return out

    def public(self) -> dict:
        changed = [i for i in self.items if i.action != "unchanged" or i.reasons]
        return {
            "token": self.token,
            "sha256": self.sha256,
            "prune": self.prune,
            "kinds": self.touched,
            "summary": self.summary(),
            "items": [i.public() for i in changed],
            "warnings": self.warnings,
            "applicable": self.applicable,
        }


def _err(errors: list, code: str, loc: tuple, lines: dict, **params) -> None:
    errors.append({"code": code, "path": _path(loc), "line": _line_for(lines, loc), "params": params})


def _file_items(doc: schema.ConfigDoc, kind: str) -> list[dict]:
    section = getattr(doc, kind)
    if kind == "variables":
        return [{"key": f"{host}/{name}", "host": host, "name": name, "value": value}
                for host, values in section.items() for name, value in values.items()]
    return [item.model_dump(mode="python") for item in section]


def _normalize(kind: str, item: dict, loc: tuple, errors: list, lines: dict) -> dict:
    """Sorted target/member lists like the exporter writes them; duplicates are an error, not silently merged."""
    def sorted_unique(values: list, where: tuple) -> list:
        seen = set()
        for i, value in enumerate(values):
            if value in seen:
                _err(errors, "duplicate_entry", where + (i,), lines, value=display(value))
            seen.add(value)
        return sorted(seen)

    item = dict(item)
    if "targets" in item:
        targets = dict(item["targets"])
        targets["hosts"] = sorted_unique(targets["hosts"], loc + ("targets", "hosts"))
        targets["groups"] = sorted_unique(targets["groups"], loc + ("targets", "groups"))
        item["targets"] = targets
    if kind in ("device_groups", "policy_hierarchy_groups"):
        item["hosts"] = sorted_unique(item["hosts"], loc + ("hosts",))
    return item


def _changes(before: dict, after: dict) -> list[dict]:
    out = []
    for name in after:
        if name == "targets":
            for sub in after["targets"]:
                if before["targets"].get(sub) != after["targets"][sub]:
                    out.append({"field": f"targets.{sub}", "before": before["targets"].get(sub), "after": after["targets"][sub]})
        elif before.get(name) != after[name]:
            out.append({"field": name, "before": before.get(name), "after": after[name]})
    return out


def _ranks(rows, key_of, bucket_of, order_of) -> dict:
    buckets: dict = {}
    for row in rows:
        buckets.setdefault(bucket_of(row), []).append(row)
    out = {}
    for members in buckets.values():
        for rank, row in enumerate(sorted(members, key=order_of)):
            out[key_of(row)] = rank
    return out


async def _generic_references(db: AsyncSession, target_table: str, skip: set[str], ids: list[uuid.UUID]) -> dict:
    """{id: [table, ...]} for rows in tables outside the import that point at `ids` (VPN, rollout waves, future tables)."""
    found: dict = {}
    if not ids:
        return found
    for table in Base.metadata.sorted_tables:
        if table.name in skip:
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name != target_table:
                continue
            column = fk.parent
            rows = (await db.execute(
                select(column, func.count()).where(column.in_(ids)).group_by(column)
            )).all()
            for ref_id, _count in rows:
                found.setdefault(ref_id, []).append(table.name)
    return found


async def _load(db: AsyncSession) -> dict:
    loaded: dict = {}
    for kind in TARGET_KINDS:
        model = KIND_SPECS[kind].model
        rows = (await db.execute(select(model).options(selectinload(model.deployments)))).scalars().all()
        loaded[kind] = {(r.name if kind == "aliases" else r.config_key): r for r in rows}
    rows = (await db.execute(select(PolicyHierarchyRule).options(selectinload(PolicyHierarchyRule.deployments)))).scalars().all()
    loaded["policy_hierarchy_rules"] = {r.config_key: r for r in rows}
    loaded["device_groups"] = {g.name: g for g in (await db.execute(select(DeviceGroup))).scalars().all()}
    loaded["policy_hierarchy_groups"] = {g.name: g for g in (await db.execute(select(PolicyHierarchyGroup))).scalars().all()}
    loaded["hosts"] = {h.name: h for h in (await db.execute(select(OpnsenseHost))).scalars().all()}
    return loaded


async def build_plan(db: AsyncSession, parsed: Parsed, prune: bool) -> Plan:
    """The plan for `parsed` against the current database. Raises ConfigError for problems in the file itself."""
    doc, lines = parsed.doc, parsed.lines
    touched = [k for k in schema.KINDS if getattr(doc, k) is not None]
    errors: list[dict] = []
    warnings: list[dict] = []

    db_canon = await canonical(db)
    loaded = await _load(db)
    hosts = loaded["hosts"]
    hierarchy_by_id = {g.id: g.name for g in loaded["policy_hierarchy_groups"].values()}

    # -- file objects: identity, duplicates, normalization -------------------------------------------------------------
    file_objects: dict[str, dict[str, dict]] = {}
    file_locs: dict[tuple, tuple] = {}
    for kind in touched:
        spec = KIND_SPECS[kind]
        objects: dict[str, dict] = {}
        for index, raw in enumerate(_file_items(doc, kind)):
            if kind == "variables":
                ident, loc = raw["key"], ("variables", raw["host"], raw["name"])
            else:
                ident, loc = raw[spec.ident], (kind, index)
            if ident in objects:
                _err(errors, "duplicate_object_key", loc + ((spec.ident,) if kind != "variables" else ()), lines, key=display(ident))
                continue
            objects[ident] = raw if kind == "variables" else _normalize(kind, raw, loc, errors, lines)
            file_locs[(kind, ident)] = loc
        file_objects[kind] = objects

    def db_items(kind: str) -> dict[str, dict]:
        if kind == "variables":
            return {f"{h}/{n}": {"key": f"{h}/{n}", "host": h, "name": n, "value": v}
                    for h, values in db_canon["variables"].items() for n, v in values.items()}
        ident = KIND_SPECS[kind].ident
        return {item[ident]: item for item in db_canon[kind]}

    db_objects = {kind: db_items(kind) for kind in schema.KINDS}

    # -- post-import state of every kind (file objects + what stays in the database) -----------------------------------
    deleted: dict[str, set[str]] = {kind: set() for kind in schema.KINDS}
    post: dict[str, dict[str, dict]] = {}
    for kind in schema.KINDS:
        if kind in file_objects:
            remaining = {k: v for k, v in db_objects[kind].items() if k not in file_objects[kind]}
            if prune:
                deleted[kind] = set(remaining)
                remaining = {}
            post[kind] = {**remaining, **file_objects[kind]}
        else:
            post[kind] = dict(db_objects[kind])

    host_names = set(hosts)
    group_names = set(post["device_groups"])
    hierarchy_names = set(post["policy_hierarchy_groups"])

    # -- references in the file ------------------------------------------------------------------------------------------
    for kind in touched:
        for ident, item in file_objects[kind].items():
            loc = file_locs[(kind, ident)]
            if kind == "variables":
                if item["host"] not in host_names:
                    _err(errors, "unknown_host", ("variables", item["host"]), lines, host=display(item["host"]))
                continue
            member_hosts = item.get("hosts", []) + item.get("targets", {}).get("hosts", [])
            for name in member_hosts:
                if name not in host_names:
                    _err(errors, "unknown_host", loc, lines, host=display(name))
            for name in item.get("targets", {}).get("groups", []):
                if name not in group_names:
                    _err(errors, "unknown_group", loc + ("targets", "groups"), lines, group=display(name))
            hierarchy_ref = item.get("targets", {}).get("hierarchy_group") if kind in HIERARCHY_TARGET_KINDS else None
            for ref, where in ((hierarchy_ref, ("targets", "hierarchy_group")), (item.get("parent"), ("parent",)),
                               (item.get("group") if kind == "policy_hierarchy_rules" else None, ("group",))):
                if ref is not None and ref not in hierarchy_names:
                    _err(errors, "unknown_hierarchy_group", loc + where, lines, group=display(ref))
            if kind == "policy_hierarchy_rules":
                for name, value in item.items():
                    if names_in(value):
                        _err(errors, "placeholder_not_supported", loc + (name,), lines)
            spec = KIND_SPECS[kind]
            if spec.api is not None:
                item = file_objects[kind][ident] = post[kind][ident] = _check_api_model(kind, item, loc, errors, lines)
            for name in member_hosts:
                host = hosts.get(name)
                if host is not None and not host.enabled:
                    warnings.append({"code": "target_host_disabled", "path": _path(loc), "params": {"host": display(name)}})

    # -- hierarchy tree and host membership ------------------------------------------------------------------------------
    host_hierarchy: dict[str, str | None] = {h.name: hierarchy_by_id.get(h.policy_hierarchy_group_id) for h in hosts.values()}
    if "policy_hierarchy_groups" in file_objects:
        file_groups = file_objects["policy_hierarchy_groups"]
        for host_name, group in list(host_hierarchy.items()):
            if prune or group in file_groups:
                # The file lists the complete membership of its groups; with prune it is the whole hierarchy.
                host_hierarchy[host_name] = None
        listed: dict[str, str] = {}
        for name, group in file_groups.items():
            for host_name in group["hosts"]:
                if host_name in listed:
                    _err(errors, "host_in_two_hierarchy_groups", file_locs[("policy_hierarchy_groups", name)] + ("hosts",),
                         lines, host=display(host_name), first=display(listed[host_name]))
                listed[host_name] = name
                if host_name in host_hierarchy:
                    host_hierarchy[host_name] = name
        parents = {name: g["parent"] for name, g in post["policy_hierarchy_groups"].items()}
        for start in parents:
            seen, current = set(), start
            while current is not None and current not in seen:
                seen.add(current)
                current = parents.get(current)
            if current is not None and current == start:
                _err(errors, "hierarchy_cycle", file_locs.get(("policy_hierarchy_groups", start), ("policy_hierarchy_groups",)),
                     lines, group=display(start))
        for name, group in post["policy_hierarchy_groups"].items():  # membership as it will be after the import
            post["policy_hierarchy_groups"][name] = {**group, "hosts": sorted(h for h, g in host_hierarchy.items() if g == name)}

    if "variables" in file_objects:  # the host page allows MAX_VARIABLES_PER_HOST; a merged import must not get past it
        per_host: dict[str, int] = {}
        for item in post["variables"].values():
            per_host[item["host"]] = per_host.get(item["host"], 0) + 1
        for host_name, count in sorted(per_host.items()):
            if count > MAX_VARIABLES_PER_HOST:
                _err(errors, "too_many_variables", ("variables", host_name), lines, host=display(host_name),
                     max=MAX_VARIABLES_PER_HOST, count=count)

    if errors:
        raise ConfigError("CONFIG_INVALID", errors[:100])

    # -- hierarchy rule positions ----------------------------------------------------------------------------------------
    db_rule_rows = loaded["policy_hierarchy_rules"]
    db_positions = _ranks(db_rule_rows.values(), lambda r: r.config_key,
                          lambda r: (hierarchy_by_id[r.hierarchy_group_id], r.rulebase), lambda r: (r.sequence, str(r.id)))  # = deploy order
    positions = dict(db_positions)
    if "policy_hierarchy_rules" in file_objects:
        buckets: dict[tuple, list[str]] = {}
        for key, item in file_objects["policy_hierarchy_rules"].items():  # file order = order in the file
            buckets.setdefault((item["group"], item["rulebase"]), []).append(key)
        for key, item in post["policy_hierarchy_rules"].items():
            if key not in file_objects["policy_hierarchy_rules"]:  # stays in the database: after the file's rules
                buckets.setdefault((item["group"], item["rulebase"]), [])
        positions = {}
        extras: dict[tuple, list[str]] = {}
        for key in sorted((k for k in post["policy_hierarchy_rules"] if k not in file_objects["policy_hierarchy_rules"]),
                          key=lambda k: db_positions.get(k, 0)):
            item = post["policy_hierarchy_rules"][key]
            extras.setdefault((item["group"], item["rulebase"]), []).append(key)
        for bucket in set(buckets) | set(extras):
            for rank, key in enumerate(buckets.get(bucket, []) + extras.get(bucket, [])):
                positions[key] = rank

    # -- items -----------------------------------------------------------------------------------------------------------
    items: list[Item] = []
    for kind in touched:
        rows = loaded.get(kind, {})
        for ident in sorted(set(file_objects[kind]) | deleted[kind] | _implicit(kind, post, db_objects, positions, db_positions)):
            row = rows.get(ident) if kind != "variables" else None
            before = db_objects[kind].get(ident)
            after = post[kind].get(ident) if ident not in deleted[kind] else None
            if before is None:
                item = Item(kind, ident, "create", None, after)
            elif after is None:
                item = Item(kind, ident, "delete", before, None)
            else:
                changes = _changes(before, after)
                if kind == "policy_hierarchy_rules" and db_positions.get(ident) != positions.get(ident):
                    changes.append({"field": "position", "before": db_positions.get(ident), "after": positions.get(ident)})
                item = Item(kind, ident, "update" if changes else "unchanged", before, after, changes)
            item.implicit = ident not in file_objects[kind] and ident not in deleted[kind]
            if row is not None:
                item.db_id = row.id
                deployments = getattr(row, "deployments", None) or []
                item.deployed_hosts = sum(1 for d in deployments if d.status in DEPLOYED)
                if item.action in ("update", "delete") and any(d.status in BUSY for d in deployments):
                    item.reasons.append("busy_deployment")
                if item.action == "delete" and deployments:
                    item.reasons.append("prune_deployed")
            items.append(item)

    await _prune_references(db, items, post, loaded, deleted, host_hierarchy)

    redeploy = sum(1 for i in items if i.action == "update" and i.deployed_hosts)
    if redeploy:
        warnings.append({"code": "redeploy_needed", "path": "", "params": {"count": redeploy}})
    creates_with_targets = sum(1 for i in items if i.action == "create" and i.kind in TARGET_KINDS
                               and (i.after["targets"]["hosts"] or i.after["targets"]["groups"] or i.after["targets"].get("hierarchy_group")))
    if creates_with_targets and not any(loaded[k] for k in TARGET_KINDS + ("policy_hierarchy_rules",)):
        warnings.append({"code": "targets_without_link", "path": "", "params": {"count": creates_with_targets}})
    warnings += _alias_reference_warnings(post, file_objects, file_locs)
    warnings += _variable_warnings(post, deleted)

    plan = Plan(parsed.sha256, prune, touched, items, warnings, positions=positions,
                host_hierarchy=host_hierarchy if "policy_hierarchy_groups" in file_objects else {},
                file_objects=file_objects)
    plan.expected = {kind: post[kind] for kind in touched}
    plan.token = _token(plan, db_canon, db_positions, loaded)
    return plan


def _implicit(kind: str, post: dict, db_objects: dict, positions: dict, db_positions: dict) -> set[str]:
    """Objects the file does not name but whose stored state still changes (rank in a reordered list, group members)."""
    out = set()
    if kind == "policy_hierarchy_rules":
        out = {k for k in post[kind] if k in db_objects[kind] and positions.get(k) != db_positions.get(k)}
    if kind == "policy_hierarchy_groups":
        out = {k for k, v in post[kind].items() if k in db_objects[kind] and db_objects[kind][k] != v}
    return out


def _check_api_model(kind: str, item: dict, loc: tuple, errors: list, lines: dict) -> dict:
    """The real API schema on the same values (minus references), so a file can never store what the UI refuses.
    Returns the item with the values as the API would store them (stripped fields, cleaned alias entries)."""
    values = {k: v for k, v in item.items() if k not in ("key", "targets", "group", "entries")}
    if kind == "aliases":
        values["content"] = "\n".join(item["entries"])
    if kind == "policy_hierarchy_rules":
        values["hierarchy_group_id"] = uuid.UUID(int=0)
    try:
        cleaned = KIND_SPECS[kind].api.model_validate(values)
    except ValidationError as exc:
        for error in exc.errors(include_url=False):
            _err(errors, "bad_value", loc + tuple(error["loc"]), lines)
        return item
    out = dict(item)
    for name in item:
        if name in values and name != "content" and hasattr(cleaned, name):
            out[name] = getattr(cleaned, name)
    if kind == "aliases":
        out["entries"] = [e for e in cleaned.content.split("\n") if e]
    return out


async def _prune_references(db, items: list[Item], post: dict, loaded: dict, deleted: dict, host_hierarchy: dict) -> None:
    by_key = {(i.kind, i.key): i for i in items}

    # device groups: post-state objects of the import, plus every other table that points at the group
    if deleted["device_groups"]:
        users: dict[str, list[str]] = {}
        for kind in TARGET_KINDS:
            for ident, item in post[kind].items():
                for group in item["targets"]["groups"]:
                    users.setdefault(group, []).append(f"{kind}:{ident}")
        ids = {loaded["device_groups"][n].id: n for n in deleted["device_groups"]}
        skip = {"device_group_members", "rule_groups", "alias_groups", "port_forward_groups", "one_to_one_groups", "outbound_nat_groups"}
        for ref_id, tables in (await _generic_references(db, "device_groups", skip, list(ids))).items():
            users.setdefault(ids[ref_id], []).extend(tables)
        for name in deleted["device_groups"]:
            if users.get(name):
                by_key[("device_groups", name)].reasons.append("prune_referenced")

    if deleted["policy_hierarchy_groups"]:
        users = {}
        for kind in HIERARCHY_TARGET_KINDS:
            for ident, item in post[kind].items():
                if item["targets"]["hierarchy_group"]:
                    users.setdefault(item["targets"]["hierarchy_group"], []).append(f"{kind}:{ident}")
        for ident, item in post["policy_hierarchy_rules"].items():
            users.setdefault(item["group"], []).append(f"policy_hierarchy_rules:{ident}")
        for name, item in post["policy_hierarchy_groups"].items():
            if item["parent"]:
                users.setdefault(item["parent"], []).append(f"policy_hierarchy_groups:{name}")
        for host_name, group in host_hierarchy.items():
            if group:
                users.setdefault(group, []).append(f"host:{host_name}")
        ids = {loaded["policy_hierarchy_groups"][n].id: n for n in deleted["policy_hierarchy_groups"]}
        skip = {"policy_hierarchy_groups", "firewall_aliases", "port_forward_rules", "one_to_one_rules", "outbound_nat_rules",
                "policy_hierarchy_rules", "opnsense_hosts"}
        for ref_id, tables in (await _generic_references(db, "policy_hierarchy_groups", skip, list(ids))).items():
            users.setdefault(ids[ref_id], []).extend(tables)
        for name in deleted["policy_hierarchy_groups"]:
            if users.get(name):
                by_key[("policy_hierarchy_groups", name)].reasons.append("prune_referenced")

    if deleted["aliases"]:
        used: set[str] = set()
        for kind in RULE_KINDS:
            for item in post[kind].values():
                used.update(str(item[f]) for f in ALIAS_REF_FIELDS if item.get(f) is not None)
        for item in post["aliases"].values():
            used.update(item["entries"])
        for name in deleted["aliases"]:
            if name in used and "prune_referenced" not in by_key[("aliases", name)].reasons:
                by_key[("aliases", name)].reasons.append("prune_referenced")


def _alias_reference_warnings(post: dict, file_objects: dict, file_locs: dict) -> list[dict]:
    aliases = set(post["aliases"])
    out = []
    for kind in RULE_KINDS:
        for ident, item in file_objects.get(kind, {}).items():
            for name in ALIAS_REF_FIELDS:
                value = item.get(name)
                if isinstance(value, str) and _ALIAS_LIKE.match(value) and not _NOT_ALIAS.match(value) and value not in aliases:
                    out.append({"code": "unknown_alias_reference", "path": _path(file_locs[(kind, ident)] + (name,)),
                                "params": {"value": display(value)}})
    return out


def _variable_warnings(post: dict, deleted: dict) -> list[dict]:
    if not deleted["variables"]:
        return []
    used: set[str] = set()
    for kind in RULE_KINDS + ("aliases",):
        for item in post[kind].values():
            for value in item.values():
                if isinstance(value, str):
                    used |= names_in(value)
                elif isinstance(value, list):
                    for entry in value:
                        used |= names_in(entry)
    out = []
    for key in sorted(deleted["variables"]):
        host, name = key.split("/", 1)
        if name in used:
            out.append({"code": "variable_removed_in_use", "path": f"variables.{host}", "params": {"host": display(host), "name": name}})
    return out


def _token(plan: Plan, db_canon: dict, db_positions: dict, loaded: dict) -> str:
    deployments = {
        kind: {ident: sorted((str(d.host_id), d.status) for d in (row.deployments or []))
               for ident, row in loaded[kind].items()}
        for kind in TARGET_KINDS + ("policy_hierarchy_rules",)
    }
    material = {
        "file": plan.file_objects,
        "prune": plan.prune,
        "db": db_canon,
        "positions": db_positions,
        "hosts": sorted((h.name, str(h.id), h.enabled, str(h.policy_hierarchy_group_id)) for h in loaded["hosts"].values()),
        "ids": {k: sorted((n, str(o.id)) for n, o in loaded[k].items()) for k in ("device_groups", "policy_hierarchy_groups")},
        "deployments": deployments,
        "items": [i.public() for i in plan.items],
        "warnings": plan.warnings,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
