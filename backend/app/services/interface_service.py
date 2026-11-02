"""Interface identifiers/labels for populating the rule-form dropdown —
sourced live from each host via `getRule`'s template response, so it always
matches that host's actual interface assignments (lan/wan/optN, VLANs,
custom names) instead of a guessed static list.
"""
from typing import Callable

from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.schemas.host import InterfaceOption
from app.services import opnsense_client, variables_service
from app.services.opnsense_client import OpnsenseAPIError


def _template_options(data: dict, field: str) -> list[InterfaceOption]:
    options = data.get("rule", {}).get(field, {}) if isinstance(data, dict) else {}
    return [InterfaceOption(name=name, label=meta.get("value") or name) for name, meta in options.items()]


async def get_interfaces(host: OpnsenseHost) -> list[InterfaceOption]:
    return _template_options(await opnsense_client.get_rule_template(host), "interface")


async def get_protocols(host: OpnsenseHost) -> list[InterfaceOption]:
    """Unlike interfaces, protocol names are IANA-standard constants baked
    into OPNsense itself, not per-host config — same list on every box (bar
    version drift), so callers don't need the cross-host consistency
    dance get_interfaces/check_interface_consistency exist for."""
    return _template_options(await opnsense_client.get_rule_template(host), "protocol")


async def check_interface_consistency(hosts: list[OpnsenseHost], interface_name: str) -> dict[str, str]:
    """For a rule pushed to more than one host: resolves what
    `interface_name` actually means on each host right now, live, and
    returns {host_name: label} for hosts where it resolved (silently
    skipping hosts that fail to answer — they surface their own, more
    specific error later in the actual deploy).

    The same identifier (e.g. "opt6") can be assigned to a completely
    different physical/VLAN interface on different boxes — OPNsense's
    addRule/setRule accepts it either way since it's syntactically valid
    on each host individually, so nothing else catches this. Callers
    should only bother calling this when len(hosts) > 1 — a single target
    host can never conflict with itself.
    """
    labels: dict[str, str] = {}
    if variables_service.names_in(interface_name):
        return labels  # an interface role: it deliberately means a different interface per host (checked by role_targets)
    for host in hosts:
        try:
            options = await get_interfaces(host)
        except OpnsenseAPIError:
            continue
        match = next((o.label for o in options if o.name == interface_name), None)
        if match is not None:
            labels[host.name] = match
    return labels


async def role_targets(builder: Callable, entity, hosts: list[OpnsenseHost]) -> list[OpnsenseHost]:
    """The hosts a central object is really deployed to when its interface is a role ({{IF_NAME}}).

    Hosts without that role are left out (the preview names them). If no host is left, nothing is deployed. For every
    remaining host the role's interface must exist on the firewall right now (live); a host that does not answer is
    left to the deploy itself, which reports the error more specifically.
    """
    if not variables_service.names_in(getattr(entity, "interface", "") or ""):
        return hosts
    targets = variables_service.applicable_hosts(builder, entity, hosts)
    if not targets:
        raise api_error(400, ErrorCode.ROLE_NOT_APPLICABLE_ANYWHERE, interface=entity.interface)
    unknown: dict[str, str] = {}
    for host in targets:
        identifier = builder(entity, host)["interface"]
        try:
            options = await get_interfaces(host)
        except OpnsenseAPIError:
            continue
        if not any(o.name == identifier for o in options):
            unknown[host.name] = identifier
    if unknown:
        raise api_error(409, ErrorCode.ROLE_INTERFACE_UNKNOWN, interface=entity.interface,
                        detail=", ".join(f"{name}={ident}" for name, ident in sorted(unknown.items())))
    return targets
