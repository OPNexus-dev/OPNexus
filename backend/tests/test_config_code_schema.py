"""Schema v1 (1.32 P3): strict, equal to the API limits, JSON Schema file up to date."""
import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config_code import schema
from app.schemas.alias import AliasCreate
from app.schemas.one_to_one import OneToOneCreate
from app.schemas.outbound_nat import OutboundNatCreate
from app.schemas.policy_hierarchy import PolicyHierarchyRuleCreate
from app.schemas.port_forward import PortForwardCreate
from app.schemas.rule import RuleCreate

# (file model, API model, fields that exist only on one side)
PAIRS = [
    (schema.RuleDoc, RuleCreate, {"key", "targets"}, {"target_host_ids", "target_group_ids"}),
    (schema.PortForwardDoc, PortForwardCreate, {"key", "targets"}, {"target_host_ids", "target_group_ids", "policy_hierarchy_group_id"}),
    (schema.OneToOneDoc, OneToOneCreate, {"key", "targets"}, {"target_host_ids", "target_group_ids", "policy_hierarchy_group_id"}),
    (schema.OutboundNatDoc, OutboundNatCreate, {"key", "targets"}, {"target_host_ids", "target_group_ids", "policy_hierarchy_group_id"}),
    (schema.HierarchyRuleDoc, PolicyHierarchyRuleCreate, {"key", "group"}, {"hierarchy_group_id"}),
    (schema.AliasDoc, AliasCreate, {"entries", "targets"}, {"content", "target_host_ids", "target_group_ids", "policy_hierarchy_group_id"}),
]


@pytest.mark.parametrize("doc,api,only_doc,only_api", PAIRS, ids=[p[0].__name__ for p in PAIRS])
def test_file_model_has_the_same_fields_defaults_and_limits_as_the_api_model(doc, api, only_doc, only_api):
    assert set(doc.model_fields) - only_doc == set(api.model_fields) - only_api
    for name in set(doc.model_fields) - only_doc:
        a, b = doc.model_fields[name], api.model_fields[name]
        assert a.default == b.default, name
        assert _limits(a) == _limits(b), name


def _limits(field) -> dict:
    """Length/pattern limits of a field, also when they sit on the str branch of `str | None`."""
    metas = list(field.metadata)
    for arg in typing.get_args(field.annotation):
        metas += list(getattr(arg, "__metadata__", ()))
    metas = [m for meta in metas for m in (meta.metadata if hasattr(meta, "metadata") else [meta])]  # unwrap Field(...)
    out = {}
    for meta in metas:
        for attr in ("min_length", "max_length", "pattern"):
            if getattr(meta, attr, None) is not None:
                out[attr] = getattr(meta, attr)
    return out


def test_enumerations_match_the_api():
    for doc, api, name in ((schema.RuleDoc, RuleCreate, "action"), (schema.PortForwardDoc, PortForwardCreate, "natreflection"),
                           (schema.OneToOneDoc, OneToOneCreate, "natreflection"), (schema.OneToOneDoc, OneToOneCreate, "type"),
                           (schema.AliasDoc, AliasCreate, "type"), (schema.HierarchyRuleDoc, PolicyHierarchyRuleCreate, "rulebase")):
        assert doc.model_fields[name].annotation == api.model_fields[name].annotation, (doc.__name__, name)


def _rule(**over):
    return {"key": "r-1", "description": "d", "interface": "lan", **over}


def test_unknown_fields_wrong_types_and_secret_ref_are_refused():
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(secret_ref="x"))
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(enabled="yes"))  # strict: no truthy strings
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(source_port=True))  # bool is never a port
    with pytest.raises(ValidationError):
        schema.ConfigDoc.model_validate({"opnexus_config": 2})


def test_ports_accept_numbers_and_ranges_and_become_strings():
    assert schema.RuleDoc.model_validate(_rule(destination_port=53)).destination_port == "53"
    assert schema.RuleDoc.model_validate(_rule(destination_port="8000-8100")).destination_port == "8000-8100"
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(destination_port="1" * 33))


@pytest.mark.parametrize("bad", ["a\x00b", "a\x1b[31m", "a\x7fb", "a\rb", "a\x0bb"])
def test_control_characters_are_refused(bad):
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(description=bad))
    with pytest.raises(ValidationError):
        schema.AliasDoc.model_validate({"name": "A", "entries": [bad]})
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(interface=bad))


def test_descriptions_may_have_several_lines_and_padding_but_names_and_keys_not():
    assert schema.RuleDoc.model_validate(_rule(description="a\n\tb ")).description == "a\n\tb "
    assert schema.DeviceGroupDoc.model_validate({"name": "G", "description": " zwei\nZeilen "}).description == " zwei\nZeilen "
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(interface="a\nb"))  # a newline only in free text
    for bad in (" G", "G "):
        with pytest.raises(ValidationError):
            schema.DeviceGroupDoc.model_validate({"name": bad})
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(targets={"hosts": [" fw"]}))


@pytest.mark.parametrize("key", ["", "Upper", "-lead", "a b", "x" * 65, "ü"])
def test_keys_must_be_lowercase_slugs(key):
    with pytest.raises(ValidationError):
        schema.RuleDoc.model_validate(_rule(key=key))


def test_variable_names_and_values_use_the_same_rules_as_the_host_page():
    ok = {"opnexus_config": 1, "variables": {"fw": {"LAN_NET": "10.0.0.0/24"}}}
    assert schema.ConfigDoc.model_validate(ok).variables == {"fw": {"LAN_NET": "10.0.0.0/24"}}
    for bad in ({"lan": "x"}, {"LAN": "{{X}}"}, {"LAN": "a\nb"}, {"LAN": ""}):
        with pytest.raises(ValidationError):
            schema.ConfigDoc.model_validate({"opnexus_config": 1, "variables": {"fw": bad}})


def test_a_missing_section_is_not_an_empty_section():
    doc = schema.ConfigDoc.model_validate({"opnexus_config": 1, "aliases": []})
    assert doc.aliases == [] and doc.rules is None


def test_checked_in_json_schema_is_current():
    path = Path(schema.__file__).with_name(schema.SCHEMA_FILE)
    assert path.read_text(encoding="utf-8") == schema.json_schema_text(), (
        "Schema geändert: `docker compose run --rm --no-deps -v \"$PWD/backend:/app\" backend python -m app.config_code schema "
        f"> backend/app/config_code/{schema.SCHEMA_FILE}`"
    )
