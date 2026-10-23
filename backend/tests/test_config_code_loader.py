"""Strict YAML loader (1.32 P2): everything a hostile file could try is refused with a stable code."""
import time

import pytest

from app.config_code import loader
from app.config_code.loader import ConfigError, load_yaml, parse

HEAD = "opnexus_config: 1\n"


def _code(raw) -> tuple[str, str]:
    with pytest.raises(ConfigError) as info:
        parse(raw)
    return info.value.code, info.value.errors[0]["code"]


def _codes(raw) -> list[str]:
    with pytest.raises(ConfigError) as info:
        parse(raw)
    return [e["code"] for e in info.value.errors]


RULE = "rules:\n- key: r-1\n  description: d\n  interface: lan\n"


def test_a_plain_file_loads():
    doc, sha, _lines = parse(HEAD + RULE)
    assert doc.rules[0].key == "r-1" and len(sha) == 64


@pytest.mark.parametrize("raw,code", [
    # Billion laughs and every other use of anchors/aliases
    ("a: &x [1, 2]\nb: *x\n", "yaml_anchor_forbidden"),
    ("a: *x\n", "yaml_alias_forbidden"),
    ('lol1: &l1 ["x","x"]\nlol2: &l2 [*l1,*l1]\nlol3: [*l2,*l2]\n', "yaml_anchor_forbidden"),
    # merge keys
    ("a: {b: 1}\nc:\n  <<: {d: 1}\n", "yaml_merge_forbidden"),
    # explicit tags of any kind
    ("a: !!python/object/apply:os.system ['id']\n", "yaml_tag_forbidden"),
    ("a: !!python/name:os.system\n", "yaml_tag_forbidden"),
    ("a: !!binary aGVsbG8=\n", "yaml_tag_forbidden"),
    ("a: !!set {x: null}\n", "yaml_tag_forbidden"),
    ("a: !!str 1\n", "yaml_tag_forbidden"),
    ("a: ! x\n", "yaml_tag_forbidden"),
    ("a: !local x\n", "yaml_tag_forbidden"),
    ("!!map {a: 1}\n", "yaml_tag_forbidden"),
    # directives and multiple documents
    ("%YAML 1.1\n---\na: 1\n", "yaml_directive_forbidden"),
    ("%TAG !e! tag:example.com,2000:\n---\na: 1\n", "yaml_directive_forbidden"),
    ("a: 1\n---\nb: 2\n", "yaml_multiple_documents"),
    ("", "yaml_empty"),
    ("# only a comment\n", "yaml_empty"),
    # keys
    ("a: 1\na: 2\n", "duplicate_key"),
    ("rules:\n- key: x\n  key: y\n", "duplicate_key"),
    ("1: a\n", "non_string_key"),
    ("true: a\n", "non_string_key"),
    ("? [a]\n: b\n", "non_string_key"),
    ("null: a\n", "non_string_key"),
    # structure limits
    ("a: " + "[" * 20 + "]" * 20 + "\n", "too_deep"),
    ("a: x\0y\n", "yaml_syntax"),
    ("a: 'x\x1b[31m'\n", "yaml_syntax"),
    ("a: [1, 2\n", "yaml_syntax"),
    ("a:\n\t- b\n", "yaml_syntax"),
])
def test_hostile_yaml_is_refused(raw, code):
    assert _code(raw) == ("CONFIG_INVALID", code)


def test_errors_name_line_and_path():
    with pytest.raises(ConfigError) as info:
        parse(HEAD + RULE + "  targets:\n    hosts: [a]\n    hosts: [b]\n")
    error = info.value.errors[0]
    assert error["code"] == "duplicate_key" and error["line"] == 8 and error["path"] == "rules[0].targets"
    with pytest.raises(ConfigError) as info:
        parse(HEAD + RULE + "  destination_port: [1]\n  secret_ref: x\n")
    by_code = {e["code"]: e for e in info.value.errors}
    assert by_code["unknown_field"]["path"] == "rules[0].secret_ref" and by_code["unknown_field"]["line"] == 7
    assert by_code["unknown_field"]["params"] == {"field": "secret_ref"}
    assert by_code["bad_type"]["path"].startswith("rules[0].destination_port") and by_code["bad_type"]["line"] == 6


def test_yaml_1_1_surprises_stay_text_or_are_type_errors():
    data, _ = load_yaml("a: on\nb: yes\nc: 1.5\nd: 2026-10-22\ne: 0755\nf: 1:30\ng: 0x1F\nh: .inf\ni: Off\nj: 1e3\n")
    assert data == {"a": "on", "b": "yes", "c": "1.5", "d": "2026-10-22", "e": "0755", "f": "1:30", "g": "0x1F",
                    "h": ".inf", "i": "Off", "j": "1e3"}
    data, _ = load_yaml("a: true\nb: False\nc: null\nd: ~\ne:\nf: 53\ng: -1\nh: 0\n")
    assert data == {"a": True, "b": False, "c": None, "d": None, "e": None, "f": 53, "g": -1, "h": 0}
    assert "enabled" in _field_error(RULE + "  enabled: yes\n")  # strict bool: "yes" is not true


def _field_error(body: str) -> str:
    with pytest.raises(ConfigError) as info:
        parse(HEAD + body)
    return " ".join(e["path"] for e in info.value.errors)


def test_huge_integers_stay_text_and_do_not_hit_pythons_int_limit():
    data, _ = load_yaml("a: " + "9" * 5000 + "\n")
    assert data["a"] == "9" * 5000


@pytest.mark.parametrize("raw,code", [
    (b"a: \xff\xfe\n", "bad_encoding"),
    (b"a: \xc3\x28\n", "bad_encoding"),
])
def test_invalid_utf8_is_refused(raw, code):
    assert _code(raw) == ("CONFIG_INVALID", code)


def test_a_bom_is_accepted():
    doc = parse("\ufeff" + HEAD).doc
    assert doc.rules is None


def test_size_limits():
    assert _code(b"#" * (loader.MAX_BYTES + 1)) == ("CONFIG_TOO_LARGE", "too_large")
    assert _code("a: " + "x" * (loader.MAX_SCALAR + 1) + "\n") == ("CONFIG_INVALID", "scalar_too_long")
    many = "a: [" + ",".join(["1"] * (loader.MAX_NODES + 1)) + "]\n"
    assert _code(many) == ("CONFIG_INVALID", "too_many_nodes")


def test_object_and_entry_limits():
    rules = "rules:\n" + "".join(f"- {{key: r-{i}, description: d, interface: lan}}\n" for i in range(loader.MAX_OBJECTS_PER_KIND + 1))
    assert "too_many_objects" in _codes(HEAD + rules)
    entries = "aliases:\n- name: A\n  entries: [" + ",".join(f"10.0.{i // 250}.{i % 250}" for i in range(loader.MAX_ALIAS_ENTRIES + 1)) + "]\n"
    assert "too_many_entries" in _codes(HEAD + entries)
    long_entries = "aliases:\n- name: A\n  entries: [" + ",".join(["x" * 999] * 9) + "]\n"
    assert "too_long" in _codes(HEAD + long_entries)
    variables = "variables:\n  fw:\n" + "".join(f"    V{i}: x\n" for i in range(loader.MAX_VARIABLES_PER_HOST + 1))
    assert "too_many_objects" in _codes(HEAD + variables)


@pytest.mark.parametrize("raw,code,top", [
    ("- a\n", "not_a_mapping", "CONFIG_INVALID"),
    ("rules: []\n", "missing_version", "CONFIG_INVALID"),
    ("opnexus_config: '1'\n", "missing_version", "CONFIG_INVALID"),
    ("opnexus_config: true\n", "missing_version", "CONFIG_INVALID"),
    ("opnexus_config: 2\n", "unsupported_version", "CONFIG_UNSUPPORTED_VERSION"),
])
def test_version_and_shape(raw, code, top):
    assert _code(raw) == (top, code)


def test_error_values_are_shortened_and_escaped():
    with pytest.raises(ConfigError) as info:
        parse(HEAD + RULE + "  direction: \"" + "\\u001b[2J" + "x" * 200 + "\"\n")
    value = info.value.errors[0]["params"]["value"]
    assert len(value) <= 80 and "\x1b" not in value and value.startswith("\\x1b")


def test_one_mebibyte_parses_in_reasonable_time():
    """Pure-Python PyYAML; design risk (4). A full-size file of realistic rules must stay well under the request timeout."""
    line = "- {key: r-%05d, description: Regel %05d mit etwas Text, interface: lan, source_net: 10.0.0.0/24, destination_port: '443'}\n"
    body = HEAD + "rules:\n"
    i = 0
    while len(body) + len(line) < loader.MAX_BYTES - 200 and i < loader.MAX_OBJECTS_PER_KIND:
        body += line % (i, i)
        i += 1
    body += "aliases:\n- name: BIG\n  entries:\n" + "".join(f"  - 10.{n // 65536}.{n // 256 % 256}.{n % 256}\n" for n in range(loader.MAX_ALIAS_ENTRIES))
    raw = body.encode()[: loader.MAX_BYTES]
    started = time.monotonic()
    try:
        parse(raw)
    except ConfigError:
        pass  # truncation may cut the last line; the parse time is what counts
    elapsed = time.monotonic() - started
    print(f"parse {len(raw)} bytes: {elapsed:.2f}s")
    assert elapsed < 8


WRONG_SHAPES = ["42", '"text"', "true", "{a: 1}", "[1, 2]", "[[1]]", "[{entries: 5}]", "[{entries: [1, [2]]}]", "null"]


@pytest.mark.parametrize("shape", WRONG_SHAPES)
@pytest.mark.parametrize("kind", __import__("app.config_code.schema", fromlist=["KINDS"]).KINDS)
def test_every_section_with_the_wrong_shape_is_a_config_error_never_another_exception(kind, shape):
    """Valid YAML of the wrong form used to end in a TypeError before the schema check (audit AUD-07)."""
    try:
        parse(f"{HEAD}{kind}: {shape}\n")
    except ConfigError:
        pass  # the expected, orderly refusal (null parses as an empty section)
