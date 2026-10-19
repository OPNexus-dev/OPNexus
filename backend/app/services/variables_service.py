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


def ensure_resolvable(builder: Callable, entity, hosts: list[OpnsenseHost]) -> None:
    """Raise MissingVariables (listing every affected host) before anything is written."""
    missing: dict[str, list[str]] = {}
    for host in hosts:
        try:
            builder(entity, host)
        except MissingVariables as exc:
            missing.update(exc.missing)
    if missing:
        raise MissingVariables(missing)


def payload_or_none(builder: Callable, entity, host: OpnsenseHost):
    try:
        return builder(entity, host)
    except MissingVariables:
        return None


def preview_row_missing(host: OpnsenseHost, names: list[str], dep) -> dict:
    return {
        "host_id": str(host.id), "host_name": host.name, "action": "missing_variable",
        "status": dep.status if dep else None,
        "message": "Variable fehlt: " + ", ".join(names), "can_remove": False,
        "can_retry_rollback": False, "changes": [],
    }
