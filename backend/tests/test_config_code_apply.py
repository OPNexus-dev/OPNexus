"""Planner and apply (1.32 P5/P6): round trip, partial files, prune protection, conflicts, atomicity, no firewall."""
import ast
import copy
from pathlib import Path

import pytest
import respx
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config_code import apply as applier
from app.config_code import plan as planner
from app.config_code.export import export_yaml
from app.config_code.loader import ConfigError, parse
from app.database import SessionLocal
from app.models.alias import FirewallAlias
from app.models.audit import AuditEvent
from app.models.host import OpnsenseHost
from app.models.host_variable import HostVariable
from app.models.one_to_one import OneToOneRule
from app.models.outbound_nat import OutboundNatRule
from app.models.policy import DeviceGroup, RuleRevision
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.port_forward import PortForwardRule
from app.models.rollout import RolloutPlan, RolloutPlanWave
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from tests.config_code_seed import seed_everything

CENTRAL = (PolicyHierarchyRule, OutboundNatRule, OneToOneRule, PortForwardRule, FirewallRule, FirewallAlias, DeviceGroup)


async def export() -> str:
    async with SessionLocal() as db:
        return await export_yaml(db)


async def plan(text: str, prune: bool = False) -> planner.Plan:
    async with SessionLocal() as db:
        return await planner.build_plan(db, parse(text), prune)


async def apply(text: str, prune: bool = False, token: str | None = None) -> planner.Plan:
    token = token or (await plan(text, prune)).token
    async with SessionLocal() as db:
        try:
            result = await applier.apply_config(db, parse(text), prune, token)
        except Exception:
            await db.rollback()
            raise
        await db.commit()
        return result


def edit(text: str, fn) -> str:
    data = yaml.safe_load(text)
    fn(data)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def by_key(p: planner.Plan) -> dict:
    return {(i.kind, i.key): i for i in p.items}


async def wipe_central_objects() -> None:
    async with SessionLocal() as db:
        await db.execute(HostVariable.__table__.delete())
        for model in CENTRAL:
            await db.execute(model.__table__.delete())
        await db.execute(PolicyHierarchyGroup.__table__.update().values(parent_id=None))
        await db.execute(OpnsenseHost.__table__.update().values(policy_hierarchy_group_id=None))
        await db.execute(PolicyHierarchyGroup.__table__.delete())
        await db.commit()


async def payloads() -> dict:
    """What every object would push to every host: the ROADMAP criterion "identical preview" after a round trip."""
    from app.services.alias_deploy_service import _build_payload as alias_payload
    from app.services.one_to_one_deploy_service import _build_payload as one_payload
    from app.services.outbound_nat_deploy_service import _build_payload as out_payload
    from app.services.port_forward_deploy_service import _build_payload as pf_payload
    from app.services.rule_deploy_service import _build_payload as rule_payload

    out = {}
    async with SessionLocal() as db:
        hosts = (await db.execute(select(OpnsenseHost))).scalars().all()
        for model, builder, ident in ((FirewallRule, rule_payload, "config_key"), (PortForwardRule, pf_payload, "config_key"),
                                      (OneToOneRule, one_payload, "config_key"), (OutboundNatRule, out_payload, "config_key"),
                                      (FirewallAlias, alias_payload, "name")):
            for row in (await db.execute(select(model))).scalars().all():
                for host in hosts:
                    try:
                        out[(model.__name__, getattr(row, ident), host.name)] = builder(row, host)
                    except Exception as exc:  # a missing variable is part of the result too
                        out[(model.__name__, getattr(row, ident), host.name)] = type(exc).__name__
    return out


# -- round trip ---------------------------------------------------------------------------------------------------------

async def test_export_then_plan_shows_no_changes_and_apply_is_a_no_op():
    await seed_everything()
    text = await export()
    p = await plan(text)
    assert p.applicable and p.summary()["create"] == p.summary()["update"] == p.summary()["delete"] == 0
    await apply(text, prune=True)
    assert await export() == text


async def test_import_into_an_instance_without_central_objects_restores_everything_identically():
    await seed_everything()
    text, before = await export(), await payloads()
    await wipe_central_objects()
    p = await plan(text, prune=True)
    assert p.applicable and p.summary()["create"] > 10
    await apply(text, prune=True)
    assert await export() == text
    assert await payloads() == before
    assert (await plan(text, prune=True)).summary()["create"] == 0


async def test_apply_writes_revisions_and_audit_events_without_the_file_content():
    await seed_everything()
    text = edit(await export(), lambda d: d["rules"][0].update(description="geändert"))
    await apply(text)
    async with SessionLocal() as db:
        notes = (await db.execute(select(RuleRevision.note))).scalars().all()
        events = (await db.execute(select(AuditEvent))).scalars().all()
    assert "Import (Konfiguration als Code)" in notes and "Vor erster Bearbeitung" in notes
    summary = next(e for e in events if e.kind == "config")
    assert summary.action == "import" and summary.details["summary"]["update"] == 1
    assert any(e.kind == "rule" and e.action == "import_update" and e.details["fields"] == ["description"] for e in events)
    assert all("geändert" not in str(e.details) for e in events if e.kind == "config")


# -- partial files and prune ---------------------------------------------------------------------------------------------

async def test_a_missing_section_leaves_the_kind_alone_and_an_empty_one_only_deletes_with_prune():
    await seed_everything()
    only_aliases = "opnexus_config: 1\naliases: []\n"
    p = await plan(only_aliases)
    assert p.touched == ["aliases"] and p.summary() == {"create": 0, "update": 0, "delete": 0, "unchanged": 0, "blocked": 0}
    await apply(only_aliases)
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FirewallAlias)) == 1
        assert await db.scalar(select(func.count()).select_from(FirewallRule)) == 2
    p = await plan(only_aliases, prune=True)
    assert by_key(p)[("aliases", "WEB_SERVERS")].reasons == ["prune_referenced"]  # a rule still names it
    rules_without_alias = edit(await export(), lambda d: d["rules"][0].update(destination_net="any"))
    await apply(rules_without_alias)
    await apply(only_aliases, prune=True)
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FirewallAlias)) == 0
        assert await db.scalar(select(func.count()).select_from(FirewallRule)) == 2


async def test_prune_never_deletes_a_deployed_object_and_nothing_changes():
    await seed_everything()
    async with SessionLocal() as db:
        rule = await db.scalar(select(FirewallRule).where(FirewallRule.config_key == "block-all"))
        host = await db.scalar(select(OpnsenseHost).where(OpnsenseHost.name == "fw-filiale-2"))
        db.add(RuleDeployment(rule_id=rule.id, host_id=host.id, status="reverted", opnsense_rule_uuid="u"))
        await db.commit()
    text = edit(await export(), lambda d: d.update(rules=[r for r in d["rules"] if r["key"] != "block-all"]))
    p = await plan(text, prune=True)
    assert by_key(p)[("rules", "block-all")].reasons == ["prune_deployed"] and not p.applicable
    with pytest.raises(applier.PlanBlocked):
        await apply(text, prune=True, token=p.token)
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FirewallRule)) == 2


async def test_an_object_with_an_open_deployment_cannot_be_changed():
    await seed_everything()
    async with SessionLocal() as db:
        rule = await db.scalar(select(FirewallRule).where(FirewallRule.config_key == "block-all"))
        host = await db.scalar(select(OpnsenseHost).where(OpnsenseHost.name == "fw-filiale-2"))
        db.add(RuleDeployment(rule_id=rule.id, host_id=host.id, status="pending", opnsense_rule_uuid="u"))
        await db.commit()
    text = edit(await export(), lambda d: d["rules"][1].update(description="neu"))
    item = by_key(await plan(text))[("rules", "block-all")]
    assert item.reasons == ["busy_deployment"] and item.deployed_hosts == 1
    unchanged = await plan(await export())
    assert unchanged.applicable  # an unchanged busy object does not block


async def test_pruning_a_group_still_used_by_a_rollout_wave_is_blocked():
    await seed_everything()
    async with SessionLocal() as db:
        group = await db.scalar(select(DeviceGroup).where(DeviceGroup.name == "Filialen"))
        rollout = RolloutPlan(name="r", action="update", created_by="t")
        db.add(rollout)
        await db.flush()
        wave = RolloutPlanWave(plan_id=rollout.id, position=0, label="w", groups=[group])
        db.add(wave)
        await db.commit()
    data = yaml.safe_load(await export())
    for section in ("aliases", "rules", "one_to_one", "outbound_nat"):
        for obj in data[section]:
            obj["targets"]["groups"] = []
    data["device_groups"] = []
    item = by_key(await plan(yaml.safe_dump(data), prune=True))[("device_groups", "Filialen")]
    assert item.reasons == ["prune_referenced"]


async def test_pruning_a_hierarchy_group_is_blocked_while_hosts_rules_or_children_remain():
    await seed_everything()
    data = yaml.safe_load(await export())
    data["policy_hierarchy_groups"] = [g for g in data["policy_hierarchy_groups"] if g["name"] != "Organisation"]
    with pytest.raises(ConfigError) as info:  # Nord still names Organisation as parent
        await plan(yaml.safe_dump(data), prune=True)
    assert info.value.errors[0]["code"] == "unknown_hierarchy_group"
    data = yaml.safe_load(await export())
    data["policy_hierarchy_groups"] = [g for g in data["policy_hierarchy_groups"] if g["name"] != "Nord"]
    text = yaml.safe_dump(data)
    with pytest.raises(ConfigError):  # the hierarchy rule ph-post-deny still sits in Nord
        await plan(text, prune=True)
    data["policy_hierarchy_rules"] = [r for r in data["policy_hierarchy_rules"] if r["group"] != "Nord"]
    p = await plan(yaml.safe_dump(data), prune=True)
    assert p.applicable  # fw-filiale-1 drops out of the hierarchy with its group
    await apply(yaml.safe_dump(data), prune=True)
    async with SessionLocal() as db:
        host = await db.scalar(select(OpnsenseHost).where(OpnsenseHost.name == "fw-filiale-1"))
        assert host.policy_hierarchy_group_id is None
    data = yaml.safe_load(await export())
    data.pop("policy_hierarchy_rules")
    data["policy_hierarchy_groups"] = []
    item = by_key(await plan(yaml.safe_dump(data), prune=True))[("policy_hierarchy_groups", "Organisation")]
    assert item.reasons == ["prune_referenced"]  # its hierarchy rules stay in the database


# -- validation against the database ------------------------------------------------------------------------------------

@pytest.mark.parametrize("change,code", [
    (lambda d: d["rules"][0]["targets"]["hosts"].append("nope"), "unknown_host"),
    (lambda d: d["rules"][0]["targets"]["groups"].append("nope"), "unknown_group"),
    (lambda d: d["rules"].append(copy.deepcopy(d["rules"][0])), "duplicate_object_key"),
    (lambda d: d["rules"][0]["targets"]["groups"].append("Filialen"), "duplicate_entry"),
    (lambda d: d["policy_hierarchy_rules"][0].update(description="{{SITE}}"), "placeholder_not_supported"),
    (lambda d: d["policy_hierarchy_groups"][0].update(parent="Nord"), "hierarchy_cycle"),
    (lambda d: d["policy_hierarchy_groups"][0]["hosts"].append("fw-filiale-1"), "host_in_two_hierarchy_groups"),
    (lambda d: d["variables"].update({"nope": {"X": "1"}}), "unknown_host"),
    (lambda d: d["aliases"][0]["targets"].update(hierarchy_group="nope"), "unknown_hierarchy_group"),
])
async def test_file_errors_are_reported_with_a_path(change, code):
    await seed_everything()
    with pytest.raises(ConfigError) as info:
        await plan(edit(await export(), change))
    assert code in [e["code"] for e in info.value.errors]
    assert all(e["path"] for e in info.value.errors)


async def test_warnings_for_unknown_aliases_removed_variables_and_redeploys():
    await seed_everything()
    async with SessionLocal() as db:
        rule = await db.scalar(select(FirewallRule).where(FirewallRule.config_key == "allow-dns-lan"))
        host = await db.scalar(select(OpnsenseHost).where(OpnsenseHost.name == "fw-filiale-1"))
        db.add(RuleDeployment(rule_id=rule.id, host_id=host.id, status="success", opnsense_rule_uuid="u"))
        await db.commit()

    def change(d):
        d["rules"][0].update(destination_net="MISSING_ALIAS", source_net="{{LAN_NET}}")
        d["rules"][1].update(source_net="lan", destination_net="wanip")
        d["variables"] = {"fw-filiale-1": {"SITE": "x"}}

    p = await plan(edit(await export(), change), prune=True)
    codes = {w["code"]: w for w in p.warnings}
    assert codes["unknown_alias_reference"]["params"]["value"] == "MISSING_ALIAS"
    assert codes["variable_removed_in_use"]["params"]["name"] == "LAN_NET"
    assert codes["redeploy_needed"]["params"]["count"] == 1
    assert [w for w in p.warnings if w["code"] == "unknown_alias_reference"] == [codes["unknown_alias_reference"]]


# -- hierarchy order -----------------------------------------------------------------------------------------------------

async def test_hierarchy_rule_order_is_the_order_in_the_file():
    await seed_everything()
    data = yaml.safe_load(await export())
    rules = data["policy_hierarchy_rules"]
    rules[0], rules[1] = rules[1], rules[0]  # both in Organisation/pre
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    p = await plan(text)
    changed = {i.key: i.changes for i in p.items if i.action == "update"}
    assert set(changed) == {"ph-block-bogons", "ph-allow-mgmt"} and changed["ph-allow-mgmt"][0]["field"] == "position"
    await apply(text)
    assert [r["key"] for r in yaml.safe_load(await export())["policy_hierarchy_rules"]] == ["ph-allow-mgmt", "ph-block-bogons", "ph-post-deny"]
    assert (await plan(text)).summary()["update"] == 0


async def test_new_hierarchy_rules_go_before_rules_the_file_does_not_name():
    await seed_everything()
    text = ("opnexus_config: 1\npolicy_hierarchy_rules:\n"
            "- {key: ph-new, group: Organisation, rulebase: pre, description: neu, interface: lan}\n")
    p = await plan(text)
    assert {i.key: i.action for i in p.items} == {"ph-new": "create", "ph-block-bogons": "update", "ph-allow-mgmt": "update"}
    assert all(i.implicit for i in p.items if i.key != "ph-new")
    await apply(text)
    assert [r["key"] for r in yaml.safe_load(await export())["policy_hierarchy_rules"]][:3] == ["ph-new", "ph-block-bogons", "ph-allow-mgmt"]


# -- variables -----------------------------------------------------------------------------------------------------------

async def test_variables_are_merged_per_name_and_only_removed_with_prune():
    await seed_everything()
    text = "opnexus_config: 1\nvariables:\n  fw-filiale-2:\n    LAN_NET: 10.9.0.0/24\n  fw-filiale-1:\n    SITE: Neu\n"
    await apply(text)
    data = yaml.safe_load(await export())["variables"]
    assert data == {"fw-filiale-1": {"LAN_NET": "10.20.30.0/24", "SITE": "Neu"}, "fw-filiale-2": {"LAN_NET": "10.9.0.0/24"}}
    await apply(text, prune=True)
    assert yaml.safe_load(await export())["variables"] == {"fw-filiale-1": {"SITE": "Neu"}, "fw-filiale-2": {"LAN_NET": "10.9.0.0/24"}}


# -- conflicts, atomicity, self-check -----------------------------------------------------------------------------------

async def test_a_change_between_plan_and_apply_makes_the_plan_stale():
    await seed_everything()
    text = edit(await export(), lambda d: d["rules"][0].update(description="aus der Datei"))
    token = (await plan(text)).token
    async with SessionLocal() as db:
        group = await db.scalar(select(DeviceGroup).where(DeviceGroup.name == "Filialen").options(selectinload(DeviceGroup.hosts)))
        group.hosts = group.hosts[:1]  # only a group member: no updated_at anywhere
        await db.commit()
    with pytest.raises(applier.PlanStale):
        await apply(text, token=token)
    async with SessionLocal() as db:
        db.add(FirewallRule(description="neu in der UI", interface="lan", direction="in"))
        await db.commit()
    token = (await plan(text)).token
    async with SessionLocal() as db:
        rule = await db.scalar(select(FirewallRule).where(FirewallRule.description == "neu in der UI"))
        rule.description = "noch einmal geändert"
        await db.commit()
    with pytest.raises(applier.PlanStale):
        await apply(text, token=token)


async def test_a_failure_in_the_middle_leaves_the_database_unchanged(monkeypatch):
    await seed_everything()
    before = await export()
    text = edit(before, lambda d: [r.update(description=f"neu {i}") for i, r in enumerate(d["rules"])] and
                d["aliases"][0].update(description="neu"))
    original = applier._Writer.objects

    async def broken(self, kind):
        await original(self, kind)
        if kind == "rules":
            raise RuntimeError("injected")

    monkeypatch.setattr(applier._Writer, "objects", broken)
    with pytest.raises(RuntimeError):
        await apply(text)
    assert await export() == before


async def test_the_self_check_catches_a_writer_that_drops_a_field(monkeypatch):
    await seed_everything()
    before = await export()
    text = edit(before, lambda d: d["rules"][0].update(log=True))
    monkeypatch.setitem(applier.SCALARS, "rules", tuple(f for f in applier.SCALARS["rules"] if f != "log"))
    with pytest.raises(applier.SelfCheckFailed) as info:
        await apply(text)
    assert info.value.differences == ["rules:allow-dns-lan"]
    assert await export() == before


async def test_the_writer_covers_every_field_of_the_schema():
    from app.config_code import schema

    docs = {"aliases": schema.AliasDoc, "rules": schema.RuleDoc, "port_forwards": schema.PortForwardDoc,
            "one_to_one": schema.OneToOneDoc, "outbound_nat": schema.OutboundNatDoc, "policy_hierarchy_rules": schema.HierarchyRuleDoc}
    computed = {"key", "targets", "entries", "group"}
    for kind, doc in docs.items():
        assert set(applier.SCALARS[kind]) == set(doc.model_fields) - computed, kind


# -- never a firewall ----------------------------------------------------------------------------------------------------

async def test_plan_and_apply_never_talk_to_a_firewall():
    await seed_everything()
    text = edit(await export(), lambda d: d["rules"][0].update(description="x"))
    with respx.mock(assert_all_called=False) as mock:  # no routes: any HTTP request fails the test
        await apply(text, prune=True)
        await wipe_central_objects()
        await apply(text, prune=True)
    assert mock.calls.call_count == 0


def test_the_config_code_package_does_not_import_a_firewall_client():
    forbidden = ("opnsense_client", "_deploy_service", "rollout_engine", "host_maintenance_service")
    for path in Path(planner.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                assert not any(f in n for n in names for f in forbidden), f"{path.name}: {names}"


async def test_two_concurrent_applies_of_the_same_plan_serialize_and_the_second_is_stale():
    import asyncio

    await seed_everything()
    text = edit(await export(), lambda d: d["rules"][0].update(description="parallel"))
    token = (await plan(text)).token
    results = await asyncio.gather(apply(text, token=token), apply(text, token=token), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["Plan", "PlanStale"]


async def test_variables_merged_beyond_the_per_host_limit_are_refused():
    """The host page allows 200 variables per host; merging a file into existing values must not get past that."""
    await seed_everything()  # fw-filiale-1 has 2 already
    values = "".join(f"    V{i}: x\n" for i in range(199))
    with pytest.raises(ConfigError) as info:
        await plan("opnexus_config: 1\nvariables:\n  fw-filiale-1:\n" + values)
    assert [e["code"] for e in info.value.errors] == ["too_many_variables"]
    assert (await plan("opnexus_config: 1\nvariables:\n  fw-filiale-1:\n" + values, prune=True)).applicable


async def test_what_the_ui_can_store_survives_the_round_trip_and_padding_is_cleaned_like_the_ui_does():
    await seed_everything()
    async with SessionLocal() as db:
        group = await db.scalar(select(DeviceGroup).where(DeviceGroup.name == "Filialen"))
        group.description = "zwei\nZeilen "  # GroupWrite does not strip descriptions
        alias = await db.scalar(select(FirewallAlias).where(FirewallAlias.name == "WEB_SERVERS"))
        alias.description = " eingerückt"
        await db.commit()
    text = await export()
    assert (await plan(text)).summary()["update"] == 0
    padded = edit(text, lambda d: d["rules"][0].update(description="  DNS aus {{SITE}}  ", interface=" lan "))
    p = await plan(padded)
    assert p.summary()["update"] == 0  # RuleCreate strips these fields, so this is the same rule
    with_entries = edit(text, lambda d: d["aliases"][0].update(entries=[" 10.20.30.10 ", "203.0.113.20"]))
    assert (await plan(with_entries)).summary()["update"] == 0
