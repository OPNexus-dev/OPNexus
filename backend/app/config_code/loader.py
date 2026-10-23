"""Strikter YAML-Lader für Konfigurationsdateien (Sicherheitsfläche: die Datei kommt von außen).

Erlaubt ist nur das, was der Exporter selbst schreibt: Mappings, Listen und Skalare. Verboten und mit eigenem Code
gemeldet: Anker/Aliase (Billion Laughs), Merge-Keys, explizite Tags (`!!python/...`, `!!binary`, `!`), Direktiven,
mehrere Dokumente, doppelte oder nicht-textuelle Mapping-Schlüssel. Implizite Typen nur wie im YAML-1.2-Core-Schema
eingeschränkt: `true/false`, `null/~`/leer und Dezimal-Ganzzahlen; `on`, `yes`, `1.5`, `2026-10-22`, `0755`, `1:30` bleiben
Text. Danach prüft das Schema (schema.ConfigDoc) Felder, Typen, Längen und Steuerzeichen.

Reines Python (`yaml.SafeLoader`, nicht `CSafeLoader`): nur dort greifen die Hooks unten.
"""
import hashlib
import re
from typing import NamedTuple

import yaml
from pydantic import ValidationError
from yaml.composer import Composer
from yaml.constructor import SafeConstructor
from yaml.events import AliasEvent, DocumentStartEvent, StreamEndEvent
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.parser import Parser
from yaml.reader import Reader
from yaml.resolver import BaseResolver
from yaml.scanner import Scanner

from app.config_code import schema

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 8
MAX_NODES = 100_000
MAX_SCALAR = schema.MAX_SCALAR
MAX_OBJECTS_PER_KIND = 5000
MAX_VARIABLE_HOSTS = 500
MAX_VARIABLES_PER_HOST = 200  # wie routers/variables.py
MAX_ALIAS_ENTRIES = 2000
MAX_ALIAS_CONTENT = 8000  # wie schemas/alias.py `content`

_STR = "tag:yaml.org,2002:str"


class ConfigError(Exception):
    """The file is not acceptable. `errors` is a list of {code, path, line, params}; `code` is the overall reason."""

    def __init__(self, code: str, errors: list[dict]):
        self.code = code
        self.errors = errors
        super().__init__(f"{code}: {errors[:3]}")


def _err(code: str, path: str = "", line: int | None = None, **params) -> dict:
    return {"code": code, "path": path, "line": line, "params": params}


def display(value, limit: int = 80) -> str:
    """A value for an error message: shortened and with every control character escaped (no ANSI from foreign files)."""
    text = value if isinstance(value, str) else repr(value)
    text = text.encode("unicode_escape").decode("ascii")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _path(parts) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


class _Rejected(yaml.YAMLError):
    def __init__(self, code: str, mark=None, path=(), **params):
        self.code, self.mark, self.path, self.params = code, mark, tuple(path), params
        super().__init__(code)


class StrictResolver(BaseResolver):
    yaml_implicit_resolvers: dict = {}


StrictResolver.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
# Dezimal ohne führende Null und höchstens 18 Stellen: int() großer Zahlen kostet Rechenzeit (und Python begrenzt die Länge).
StrictResolver.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^(?:0|-?[1-9][0-9]{0,17})$"), list("-0123456789"))
StrictResolver.add_implicit_resolver(
    "tag:yaml.org,2002:null", re.compile(r"^(?:~|null|Null|NULL|)$"), ["~", "n", "N", ""])


class StrictConstructor(SafeConstructor):
    yaml_constructors: dict = {}


for _tag, _fn in (
    ("tag:yaml.org,2002:null", SafeConstructor.construct_yaml_null),
    ("tag:yaml.org,2002:bool", SafeConstructor.construct_yaml_bool),
    ("tag:yaml.org,2002:int", SafeConstructor.construct_yaml_int),
    (_STR, SafeConstructor.construct_yaml_str),
    ("tag:yaml.org,2002:seq", SafeConstructor.construct_yaml_seq),
    ("tag:yaml.org,2002:map", SafeConstructor.construct_yaml_map),
):
    StrictConstructor.add_constructor(_tag, _fn)


class StrictLoader(Reader, Scanner, Parser, Composer, StrictConstructor, StrictResolver):
    def __init__(self, stream: str):
        Reader.__init__(self, stream)
        Scanner.__init__(self)
        Parser.__init__(self)
        Composer.__init__(self)
        StrictConstructor.__init__(self)
        StrictResolver.__init__(self)
        self._depth = 0
        self._nodes = 0
        self._path: list = []
        self.lines: dict[tuple, int] = {}  # path -> 1-based line, for messages from the schema check

    # -- document level ---------------------------------------------------------------------------------------------
    def get_single_node(self):
        self.get_event()  # StreamStart
        if self.check_event(StreamEndEvent):
            raise _Rejected("yaml_empty")
        start = self.peek_event()
        if isinstance(start, DocumentStartEvent) and (start.version is not None or start.tags):
            raise _Rejected("yaml_directive_forbidden", start.start_mark)
        node = self.compose_document()
        if not self.check_event(StreamEndEvent):
            raise _Rejected("yaml_multiple_documents", self.peek_event().start_mark)
        self.get_event()
        return node

    # -- node level ---------------------------------------------------------------------------------------------------
    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, AliasEvent):
            raise _Rejected("yaml_alias_forbidden", event.start_mark, self._path)
        if getattr(event, "anchor", None) is not None:
            raise _Rejected("yaml_anchor_forbidden", event.start_mark, self._path)
        if getattr(event, "tag", None) is not None:
            raise _Rejected("yaml_tag_forbidden", event.start_mark, self._path, tag=display(event.tag))
        self._nodes += 1
        if self._nodes > MAX_NODES:
            raise _Rejected("too_many_nodes", event.start_mark, self._path, max=MAX_NODES)

        if isinstance(index, ScalarNode):
            segment = index.value
        elif isinstance(index, int) and not isinstance(index, bool):
            segment = index
        else:
            segment = None  # a mapping key
        if segment is not None:
            self._path.append(segment)
            self.lines.setdefault(tuple(self._path), event.start_mark.line + 1)
        self._depth += 1
        try:
            if self._depth > MAX_DEPTH:
                raise _Rejected("too_deep", event.start_mark, self._path, max=MAX_DEPTH)
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1
            if segment is not None:
                self._path.pop()

    def compose_scalar_node(self, anchor):
        event = self.peek_event()
        if len(event.value) > MAX_SCALAR:
            raise _Rejected("scalar_too_long", event.start_mark, self._path, max=MAX_SCALAR)
        return super().compose_scalar_node(anchor)

    def compose_mapping_node(self, anchor):
        node = super().compose_mapping_node(anchor)
        seen: set[str] = set()
        for key, _value in node.value:
            if not isinstance(key, ScalarNode) or key.tag != _STR:
                raise _Rejected("non_string_key", key.start_mark, self._path)
            if key.value == "<<":
                raise _Rejected("yaml_merge_forbidden", key.start_mark, self._path)
            if key.value in seen:
                raise _Rejected("duplicate_key", key.start_mark, self._path, key=display(key.value))
            seen.add(key.value)
        return node


def load_yaml(raw: bytes | str) -> tuple[object, dict[tuple, int]]:
    """Raw bytes -> plain Python data (dict/list/str/int/bool/None) plus a path->line map. Raises ConfigError."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ConfigError("CONFIG_TOO_LARGE", [_err("too_large", max=MAX_BYTES)])
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConfigError("CONFIG_INVALID", [_err("bad_encoding", offset=exc.start)]) from None
    if text.startswith("﻿"):
        text = text[1:]
    loader = None
    try:
        loader = StrictLoader(text)  # the Reader already rejects unprintable characters here
        node = loader.get_single_node()
        data = loader.construct_document(node)
    except _Rejected as exc:
        line = exc.mark.line + 1 if exc.mark is not None else None
        raise ConfigError("CONFIG_INVALID", [_err(exc.code, _path(exc.path), line, **exc.params)]) from None
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = mark.line + 1 if mark is not None else None
        raise ConfigError("CONFIG_INVALID", [_err("yaml_syntax", "", line, problem=display(exc.problem or ""))]) from None
    except yaml.YAMLError as exc:  # e.g. ReaderError for unprintable characters
        raise ConfigError("CONFIG_INVALID", [_err("yaml_syntax", problem=display(str(exc)))]) from None
    finally:
        if loader is not None:
            loader.dispose()
    return data, loader.lines


def _line_for(lines: dict[tuple, int], loc: tuple) -> int | None:
    for end in range(len(loc), 0, -1):
        if loc[:end] in lines:
            return lines[loc[:end]]
    return None


_PYDANTIC_CODES = {
    "extra_forbidden": "unknown_field",
    "missing": "missing_field",
    "literal_error": "bad_value",
    "string_too_short": "too_short",
    "string_too_long": "too_long",
    "string_pattern_mismatch": "bad_format",
}
_OWN_CODES = {"control_character", "surrounding_whitespace", "bad_variable_name", "bad_variable_value"}


def _schema_errors(exc: ValidationError, lines: dict[tuple, int]) -> list[dict]:
    out = []
    for error in exc.errors(include_url=False):
        loc = tuple(error["loc"])
        code = _PYDANTIC_CODES.get(error["type"])
        if code is None and error["type"] == "value_error":
            message = str(error.get("ctx", {}).get("error", ""))
            code = message if message in _OWN_CODES else "bad_value"
        if code is None:
            code = "bad_type"
        params = {}
        if error["type"] == "extra_forbidden":
            params["field"] = display(loc[-1]) if loc else ""
        elif code not in ("missing_field",) and "input" in error and not isinstance(error["input"], (dict, list)):
            params["value"] = display(error["input"])
        out.append(_err(code, _path(loc), _line_for(lines, loc), **params))
    return out


def _check_counts(data: dict, lines: dict[tuple, int]) -> list[dict]:
    errors = []
    for kind in schema.KINDS:
        section = data.get(kind)
        if kind == "variables":
            if isinstance(section, dict):
                if len(section) > MAX_VARIABLE_HOSTS:
                    errors.append(_err("too_many_objects", kind, _line_for(lines, (kind,)), max=MAX_VARIABLE_HOSTS))
                for host, values in section.items():
                    if isinstance(values, dict) and len(values) > MAX_VARIABLES_PER_HOST:
                        errors.append(_err("too_many_objects", _path((kind, host)), _line_for(lines, (kind, host)),
                                           max=MAX_VARIABLES_PER_HOST))
            continue
        if isinstance(section, list) and len(section) > MAX_OBJECTS_PER_KIND:
            errors.append(_err("too_many_objects", kind, _line_for(lines, (kind,)), max=MAX_OBJECTS_PER_KIND))
    aliases = data.get("aliases")
    for i, alias in enumerate(aliases if isinstance(aliases, list) else []):  # any other shape is for the schema check
        entries = alias.get("entries") if isinstance(alias, dict) else None
        if isinstance(entries, list):
            loc = ("aliases", i, "entries")
            if len(entries) > MAX_ALIAS_ENTRIES:
                errors.append(_err("too_many_entries", _path(loc), _line_for(lines, loc), max=MAX_ALIAS_ENTRIES))
            elif len("\n".join(str(e) for e in entries)) > MAX_ALIAS_CONTENT:
                errors.append(_err("too_long", _path(loc), _line_for(lines, loc), max=MAX_ALIAS_CONTENT))
    return errors


class Parsed(NamedTuple):
    doc: schema.ConfigDoc
    sha256: str  # of the raw bytes, shown in the plan and stored in the audit
    lines: dict[tuple, int]  # path -> line, for messages of the planner


def parse(raw: bytes | str) -> Parsed:
    """Load, limit and validate a file. Raises ConfigError."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()
    data, lines = load_yaml(raw)
    if not isinstance(data, dict):
        raise ConfigError("CONFIG_INVALID", [_err("not_a_mapping", line=1)])
    version = data.get("opnexus_config")
    if type(version) is not int or version != schema.SCHEMA_VERSION:  # `true == 1` in Python
        code = "CONFIG_UNSUPPORTED_VERSION" if isinstance(version, int) and not isinstance(version, bool) else "CONFIG_INVALID"
        raise ConfigError(code, [_err("unsupported_version" if code != "CONFIG_INVALID" else "missing_version",
                                      "opnexus_config", _line_for(lines, ("opnexus_config",)),
                                      **({"value": display(version)} if version is not None else {}))])
    errors = _check_counts(data, lines)
    if errors:
        raise ConfigError("CONFIG_INVALID", errors)
    try:
        doc = schema.ConfigDoc.model_validate(data)
    except ValidationError as exc:
        raise ConfigError("CONFIG_INVALID", _schema_errors(exc, lines)[:100]) from None
    return Parsed(doc, sha256, lines)
