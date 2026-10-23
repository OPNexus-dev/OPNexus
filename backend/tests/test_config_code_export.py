"""Export (1.32 P3): deterministic, complete, schema-conform, and free of anything secret."""
import re

import pytest
import yaml

from app.config_code import schema
from app.config_code.export import canonical, dump_yaml, export_yaml
from app.database import SessionLocal
from app.models.rule import FirewallRule
from tests.config_code_seed import seed_everything

GOLDEN = '''\
# OPNexus-Konfiguration (Schema v1), exportiert von OPNexus {version}.
# Enthält keine Zugänge zu Firewalls, Benutzer, Tokens oder Schlüssel. Prüfen: opnexus apply DATEI --dry-run
opnexus_config: 1
device_groups:
- name: Filialen
  description: alle Filialen
  hosts:
  - fw-filiale-1
  - fw-filiale-2
policy_hierarchy_groups:
- name: Organisation
  parent: null
  description: ''
  hosts: []
- name: Nord
  parent: Organisation
  description: Region Nord
  hosts:
  - fw-filiale-1
aliases:
- name: WEB_SERVERS
  type: host
  description: DMZ
  enabled: true
  entries:
  - 10.20.30.10
  - 203.0.113.20
  targets:
    hosts: []
    groups:
    - Filialen
    hierarchy_group: null
'''


async def _export(**kw) -> str:
    async with SessionLocal() as db:
        return await export_yaml(db, **kw)


async def test_export_starts_with_the_fixed_header_and_the_expected_sections():
    from app.version import APP_VERSION

    await seed_everything()
    text = await _export()
    assert text.startswith(GOLDEN.format(version=APP_VERSION))
    assert [k for k in yaml.safe_load(text) if k != "opnexus_config"] == list(schema.KINDS)
    assert text.endswith("\n") and "\r" not in text


async def test_export_is_deterministic_and_validates_against_the_schema():
    await seed_everything()
    first, second = await _export(), await _export()
    assert first == second
    doc = schema.ConfigDoc.model_validate(yaml.safe_load(first))  # strict: unknown fields, wrong types, control chars
    assert [r.key for r in doc.rules] == ["allow-dns-lan", "block-all"]
    assert doc.variables == {"fw-filiale-1": {"LAN_NET": "10.20.30.0/24", "SITE": "Filiale Nord"}}


async def test_hierarchy_rules_are_ordered_by_group_rulebase_and_sequence_and_keep_placeholders_unresolved():
    await seed_everything()
    data = yaml.safe_load(await _export())
    assert [r["key"] for r in data["policy_hierarchy_rules"]] == ["ph-block-bogons", "ph-allow-mgmt", "ph-post-deny"]
    rule = next(r for r in data["rules"] if r["key"] == "allow-dns-lan")
    assert rule["source_net"] == "{{LAN_NET}}" and rule["destination_port"] == "53"


async def test_ports_are_written_as_strings_so_they_survive_the_round_trip():
    await seed_everything()
    text = await _export()
    assert re.search(r"destination_port: '53'", text)
    assert schema.ConfigDoc.model_validate(yaml.safe_load(text)).rules[0].destination_port == "53"


async def test_export_never_contains_secrets_or_connection_data():
    await seed_everything()
    text = (await _export()).lower()
    for forbidden in ("akia-secret-key", "secret-host.example", "https://", "api_key", "api_secret", "private key", "opx_", "password"):
        assert forbidden not in text


async def test_kinds_limits_the_sections():
    await seed_everything()
    data = yaml.safe_load(await _export(kinds={"aliases", "rules"}))
    assert list(data) == ["opnexus_config", "aliases", "rules"]


async def test_new_objects_get_a_key_and_an_empty_database_exports_empty_sections():
    async with SessionLocal() as db:
        rule = FirewallRule(description="x", interface="lan", direction="in")
        db.add(rule)
        await db.commit()
        assert re.fullmatch(r"r-[0-9a-f]{12}", rule.config_key) and rule.config_key == f"r-{rule.id.hex[:12]}"
    data = yaml.safe_load(await _export(kinds={"aliases", "variables"}))
    assert data["aliases"] == [] and data["variables"] == {}


async def test_exporter_field_lists_match_the_schema():
    """A field added to the schema but not to the exporter (or vice versa) must fail loudly, not export silently wrong."""
    await seed_everything()
    async with SessionLocal() as db:
        content = await canonical(db)
    for kind, doc in (("rules", schema.RuleDoc), ("port_forwards", schema.PortForwardDoc), ("one_to_one", schema.OneToOneDoc),
                      ("outbound_nat", schema.OutboundNatDoc), ("policy_hierarchy_rules", schema.HierarchyRuleDoc),
                      ("aliases", schema.AliasDoc), ("device_groups", schema.DeviceGroupDoc),
                      ("policy_hierarchy_groups", schema.HierarchyGroupDoc)):
        assert all(list(item) == list(doc.model_fields) for item in content[kind]), kind
    assert dump_yaml(content) == await _export()


async def test_objects_are_sorted_by_key_not_by_insertion_order():
    async with SessionLocal() as db:
        for key in ("zz-last", "aa-first", "mm-middle"):
            db.add(FirewallRule(config_key=key, description=key, interface="lan", direction="in"))
        await db.commit()
    assert [r["key"] for r in yaml.safe_load(await _export())["rules"]] == ["aa-first", "mm-middle", "zz-last"]
