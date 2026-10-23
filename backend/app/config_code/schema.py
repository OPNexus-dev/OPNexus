"""Schema v1 der Konfigurationsdatei. Strikt: unbekannte Felder, falsche Typen und Steuerzeichen sind Fehler.

Die Grenzen entsprechen den API-Schemas der jeweiligen Objekte (schemas/rule.py usw.); `tests/test_config_code_schema.py`
hält beide gleich. Steuerzeichen sind überall verboten, außer Tab und Zeilenumbruch in Beschreibungen (die UI erlaubt
mehrzeilige Beschreibungen). Namen und Schlüssel (die Identität eines Objekts) dürfen keine umgebenden Leerzeichen haben;
alle anderen Werte säubert der Planer mit demselben API-Schema wie die UI (plan._check_api_model).
"""
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from app.models.config_key import CONFIG_KEY_RE
from app.services.variables_service import NAME_RE as VARIABLE_NAME_RE, VALUE_RE as VARIABLE_VALUE_RE

SCHEMA_VERSION = 1
MAX_SCALAR = 8000

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_TEXT = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # tab and newline allowed


def _clean(value: str) -> str:
    """Names and keys: they identify an object, so no control characters and no surrounding whitespace."""
    if _CONTROL.search(value):
        raise ValueError("control_character")
    if value != value.strip():
        raise ValueError("surrounding_whitespace")
    return value


def _clean_value(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("control_character")
    return value


def _clean_text(value: str) -> str:
    if _CONTROL_TEXT.search(value):
        raise ValueError("control_character")
    return value


def _port(value):
    # YAML liest `53` als Zahl; Ports sind in OPNsense Text (auch Bereiche wie "8000-8100"). bool bleibt ein Typfehler.
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


Text = Annotated[str, AfterValidator(_clean)]
Key = Annotated[str, StringConstraints(pattern=CONFIG_KEY_RE.pattern), AfterValidator(_clean)]
Description = Annotated[str, Field(max_length=255), AfterValidator(_clean_text)]
RuleDescription = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(_clean_text)]
GroupDescription = Annotated[str, Field(max_length=2000), AfterValidator(_clean_text)]
Interface = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_clean_value)]
Protocol = Annotated[str, Field(min_length=1, max_length=16), AfterValidator(_clean_value)]
Net = Annotated[str, Field(min_length=1, max_length=128), AfterValidator(_clean_value)]
PortText = Annotated[str, Field(max_length=32), AfterValidator(_clean_value)]
Port = Annotated[PortText | None, BeforeValidator(_port)]
GroupName = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_clean)]
HostName = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_clean)]
AliasName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")]
AliasEntry = Annotated[str, Field(min_length=1, max_length=MAX_SCALAR), AfterValidator(_clean_value)]

IpProtocol = Literal["inet", "inet6"]


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Targets(_Doc):
    hosts: list[HostName] = Field(default_factory=list)
    groups: list[GroupName] = Field(default_factory=list)


class TargetsWithHierarchy(Targets):
    hierarchy_group: GroupName | None = None


class DeviceGroupDoc(_Doc):
    name: GroupName
    description: GroupDescription = ""
    hosts: list[HostName] = Field(default_factory=list)


class HierarchyGroupDoc(_Doc):
    name: GroupName
    parent: GroupName | None = None
    description: GroupDescription = ""
    hosts: list[HostName] = Field(default_factory=list)


class AliasDoc(_Doc):
    name: AliasName
    type: Literal["host", "network", "port", "networkgroup", "mac", "asn"] = "host"
    description: Description = ""
    enabled: bool = True
    entries: list[AliasEntry] = Field(default_factory=list)
    targets: TargetsWithHierarchy = Field(default_factory=TargetsWithHierarchy)


class RuleDoc(_Doc):
    key: Key
    description: RuleDescription
    interface: Interface
    direction: Literal["in", "out"] = "in"
    ipprotocol: IpProtocol = "inet"
    protocol: Protocol = "any"
    action: Literal["pass", "block", "reject"] = "pass"
    source_net: Net = "any"
    source_port: Port = None
    destination_net: Net = "any"
    destination_port: Port = None
    enabled: bool = True
    log: bool = False
    targets: Targets = Field(default_factory=Targets)


class PortForwardDoc(_Doc):
    key: Key
    description: RuleDescription
    interface: Interface
    ipprotocol: IpProtocol = "inet"
    protocol: Protocol = "tcp"
    source_net: Net = "any"
    source_port: Port = None
    destination_net: Net = "any"
    destination_port: Port = None
    target: Net
    local_port: Port = None
    natreflection: Literal["", "purenat", "disable"] = ""
    enabled: bool = True
    targets: TargetsWithHierarchy = Field(default_factory=TargetsWithHierarchy)


class OneToOneDoc(_Doc):
    key: Key
    description: RuleDescription
    interface: Interface
    type: Literal["binat", "nat"] = "binat"
    external: Net
    source_net: Net = "any"
    destination_net: Net = "any"
    source_not: bool = False
    destination_not: bool = False
    natreflection: Literal["", "enable", "disable"] = ""
    log: bool = False
    enabled: bool = True
    targets: TargetsWithHierarchy = Field(default_factory=TargetsWithHierarchy)


class OutboundNatDoc(_Doc):
    key: Key
    description: RuleDescription
    interface: Interface
    ipprotocol: IpProtocol = "inet"
    protocol: Protocol = "any"
    source_net: Net = "any"
    source_port: Port = None
    destination_net: Net = "any"
    destination_port: Port = None
    source_not: bool = False
    destination_not: bool = False
    target: Annotated[str, Field(max_length=128), AfterValidator(_clean_value)] = ""
    target_port: Port = None
    enabled: bool = True
    targets: TargetsWithHierarchy = Field(default_factory=TargetsWithHierarchy)


class HierarchyRuleDoc(_Doc):
    key: Key
    group: GroupName
    rulebase: Literal["pre", "post"] = "pre"
    description: RuleDescription
    interface: Interface
    direction: Literal["in", "out"] = "in"
    ipprotocol: IpProtocol = "inet"
    protocol: Protocol = "any"
    action: Literal["pass", "block", "reject"] = "pass"
    source_net: Net = "any"
    source_port: Port = None
    destination_net: Net = "any"
    destination_port: Port = None
    enabled: bool = True


def _variable_name(value: str) -> str:
    if not VARIABLE_NAME_RE.match(value):
        raise ValueError("bad_variable_name")
    return value


def _variable_value(value: str) -> str:
    if not VARIABLE_VALUE_RE.match(value):
        raise ValueError("bad_variable_value")
    return value


VariableName = Annotated[str, AfterValidator(_variable_name)]
VariableValue = Annotated[str, AfterValidator(_variable_value)]


class ConfigDoc(_Doc):
    """Eine Datei. Ein fehlender Abschnitt lässt die Art unberührt (None); ein leerer Abschnitt ist ein leerer Sollzustand."""

    opnexus_config: Literal[1]
    device_groups: list[DeviceGroupDoc] | None = None
    policy_hierarchy_groups: list[HierarchyGroupDoc] | None = None
    aliases: list[AliasDoc] | None = None
    rules: list[RuleDoc] | None = None
    port_forwards: list[PortForwardDoc] | None = None
    one_to_one: list[OneToOneDoc] | None = None
    outbound_nat: list[OutboundNatDoc] | None = None
    policy_hierarchy_rules: list[HierarchyRuleDoc] | None = None
    variables: dict[HostName, dict[VariableName, VariableValue]] | None = None


# Abschnittsfolge der Datei = Reihenfolge, in der Export und Plan die Arten behandeln.
KINDS = (
    "device_groups",
    "policy_hierarchy_groups",
    "aliases",
    "rules",
    "port_forwards",
    "one_to_one",
    "outbound_nat",
    "policy_hierarchy_rules",
    "variables",
)


SCHEMA_FILE = "opnexus-config-v1.schema.json"


def json_schema_text() -> str:
    """JSON Schema of the file for editors and CI; checked in next to this module and compared by a test."""
    import json

    schema = ConfigDoc.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "OPNexus-Konfiguration (Schema v1)"
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
