"""Site variables: ``{{NAME}}`` placeholders in centrally authored objects, filled per host.

A rule, alias or NAT entry can say ``{{LAN_NET}}`` instead of a literal network; every host that object is
deployed to supplies its own value (Host page > Variables). Resolution happens when the payload for one host is
built, so preview, deploy, the change counters and the audit all see the host's real values.

Fail closed: a placeholder without a value for a target host is never pushed. Preview marks that host
``missing_variable``, the deploy is refused before anything is written, and as a last line of defence
``opnsense_client`` refuses to send any payload that still contains a placeholder.
"""
import re
from typing import Callable

from app.models.host import OpnsenseHost

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")
VALUE_RE = re.compile(r"^[\w .:/,@+\-]{1,255}$")  # networks, addresses, hostnames, names, lists: no braces, quotes or newlines
PLACEHOLDER = re.compile(r"\{\{\s*([A-Z][A-Z0-9_]{0,39})\s*\}\}")

# Interface roles: a variable named IF_<NAME> whose value is this host's OPNsense interface identifier. Used as the whole
# interface field of a central object ({{IF_GUEST}}). Unlike other variables a missing role is not an error: the host simply
# does not take part (it has no such interface), see applicable_hosts().
ROLE_PREFIX = "IF_"
ROLE_NAME_RE = re.compile(r"^IF_[A-Z0-9][A-Z0-9_]{0,36}$")
INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")  # an OPNsense interface identifier (lan, opt3, vlan0.20, ...)


class MissingVariables(Exception):
    """One or more hosts lack values for placeholders the object uses. ``missing`` maps host name -> names."""

    def __init__(self, missing: dict[str, list[str]]):
        self.missing = missing
        super().__init__("; ".join(f"{host}: {', '.join(names)}" for host, names in missing.items()))


def names_in(value) -> set[str]:
    return set(PLACEHOLDER.findall(value)) if isinstance(value, str) else set()


def names_in_payload(payload: dict) -> set[str]:
    found: set[str] = set()
    for value in payload.values():
        found |= names_in(value)
    return found


def host_values(host: OpnsenseHost) -> dict[str, str]:
    return {v.name: v.value for v in host.variables}


def resolve(payload: dict, host: OpnsenseHost) -> dict:
    """The payload with this host's values filled in; raises MissingVariables if one is not defined."""
    values = host_values(host)
    missing = sorted(n for n in names_in_payload(payload) if n not in values)
    if missing:
        raise MissingVariables({host.name: missing})
    return {k: PLACEHOLDER.sub(lambda m: values[m.group(1)], v) if isinstance(v, str) else v for k, v in payload.items()}


def is_role(name: str) -> bool:
    return name.startswith(ROLE_PREFIX)


def skips_host(names) -> bool:
    """A host that lacks an interface role is skipped (not an error), whatever else is missing there."""
    return any(is_role(n) for n in names)


def is_skipped(builder: Callable, entity, host: OpnsenseHost) -> bool:
    try:
        builder(entity, host)
    except MissingVariables as exc:
        return skips_host(exc.missing.get(host.name, []))
    return False


def applicable_hosts(builder: Callable, entity, hosts: list[OpnsenseHost]) -> list[OpnsenseHost]:
    """The hosts the object applies to: those that are not missing one of its interface roles."""
    return [h for h in hosts if not is_skipped(builder, entity, h)]


def interface_for(raw: str, host: OpnsenseHost) -> str | None:
    """The interface identifier a central object's interface field means on `host` (a literal stays as it is);
    None when the field is a role the host has not assigned."""
    if not names_in(raw):
        return raw
    try:
        return resolve({"interface": raw}, host)["interface"]
    except MissingVariables:
        return None


def validate_interface_field(value: str) -> str:
    """The interface of a central object is a literal identifier or exactly one role, never a mix or another variable."""
    if "{{" in value or "}}" in value:
        match = PLACEHOLDER.fullmatch(value)
        if not match or not is_role(match.group(1)):
            raise ValueError("Interface: entweder ein Bezeichner (z. B. lan, opt3) oder genau eine Rolle {{IF_NAME}}")
        return "{{" + match.group(1) + "}}"
    return value


def ensure_resolvable(builder: Callable, entity, hosts: list[OpnsenseHost]) -> None:
    """Raise MissingVariables (listing every affected host) before anything is written. Hosts missing an interface
    role are skipped here (callers filter them with applicable_hosts)."""
    missing: dict[str, list[str]] = {}
    for host in hosts:
        try:
            builder(entity, host)
        except MissingVariables as exc:
            if not skips_host(exc.missing.get(host.name, [])):
                missing.update(exc.missing)
    if missing:
        raise MissingVariables(missing)


def payload_or_none(builder: Callable, entity, host: OpnsenseHost):
    try:
        return builder(entity, host)
    except MissingVariables:
        return None


def preview_row_missing(host: OpnsenseHost, names: list[str], dep) -> dict:
    roles = [n[len(ROLE_PREFIX):] for n in names if is_role(n)]
    if roles:
        return {
            "host_id": str(host.id), "host_name": host.name, "action": "not_applicable",
            "status": dep.status if dep else None,
            "message": "Schnittstellen-Rolle nicht zugeordnet: " + ", ".join(roles), "can_remove": False,
            "can_retry_rollback": False, "changes": [],
        }
    return {
        "host_id": str(host.id), "host_name": host.name, "action": "missing_variable",
        "status": dep.status if dep else None,
        "message": "Variable fehlt: " + ", ".join(names), "can_remove": False,
        "can_retry_rollback": False, "changes": [],
    }
