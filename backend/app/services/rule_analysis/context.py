"""Everything about one firewall the analysis needs, fetched read-only and cached briefly.

Sources (see the evidence block in opnsense_client.py): ``firewall/filter/searchRule`` (all
rules incl. automatic ones, with ``sort_order``), ``diagnostics/firewall/pfStatistics/rules``
(what pf has loaded), ``interfaces/overview/interfacesInfo`` (identifier, device, addresses
incl. CARP VIPs, gateways), ``firewall/group/searchItem`` (interface groups),
``firewall/alias/searchItem`` (alias definitions), ``firewall/d_nat/searchRule`` (port
forwards, only to warn about NAT) and, per queried IP, ``firewall/alias_util/findReferences``.
Optional sources may fail; the analysis then lowers its confidence instead of guessing.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass, field

from app.models.host import OpnsenseHost
from app.services import opnsense_client
from app.services.rule_analysis import pf_ruleset
from app.services.rule_analysis.model import IPAddr, IPNet

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
# Built-in interface groups whose members are the matching dynamic devices.
BUILTIN_GROUP_DEVICES = {"wireguard": ("wg",), "openvpn": ("ovpns", "ovpnc"), "enc0": ("enc0",)}


@dataclass
class IfInfo:
    identifier: str
    device: str
    description: str
    nets: list[IPNet] = field(default_factory=list)
    addrs: list[IPAddr] = field(default_factory=list)      # primary addresses
    vips: list[IPAddr] = field(default_factory=list)       # CARP / alias VIPs on this interface
    has_gateway: bool = False


@dataclass
class Context:
    rows: list[dict]
    pf: pf_ruleset.PfRuleset | None
    interfaces: dict[str, IfInfo]
    groups: dict[str, set[str]] | None            # name -> member identifiers; None = unknown
    aliases: dict[str, dict]
    port_forwards: list[dict] | None
    refs: dict[str, list[str] | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    loaded_at: float = 0.0

    @property
    def self_addrs(self) -> list[IPAddr]:
        addrs = [a for i in self.interfaces.values() for a in (*i.addrs, *i.vips)]
        return addrs + [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]

    def by_device(self, device: str) -> IfInfo | None:
        return next((i for i in self.interfaces.values() if i.device == device), None)

    def by_label(self, label: str) -> IfInfo | None:
        lowered = label.strip().lower()
        return self.interfaces.get(lowered) or next(
            (i for i in self.interfaces.values() if i.description.lower() == lowered), None)


def _interfaces(info: dict) -> dict[str, IfInfo]:
    result: dict[str, IfInfo] = {}
    for row in (info or {}).get("rows", []) or []:
        identifier = str(row.get("identifier") or "").strip()
        if not identifier:
            continue
        item = IfInfo(identifier=identifier, device=str(row.get("device") or ""),
                      description=str(row.get("description") or identifier),
                      has_gateway=bool(row.get("gateways")) or "default" in (row.get("routes") or []))
        for entry in [*(row.get("ipv4") or []), *(row.get("ipv6") or [])]:
            try:
                iface = ipaddress.ip_interface(str(entry.get("ipaddr")).split("%")[0])
            except ValueError:
                continue
            (item.vips if entry.get("vhid") or entry.get("vip") else item.addrs).append(iface.ip)
            if iface.network not in item.nets:
                item.nets.append(iface.network)
        result[identifier] = item
    return result


def _groups(data: dict | None, interfaces: dict[str, IfInfo]) -> dict[str, set[str]] | None:
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    groups: dict[str, set[str]] = {}
    for row in data["rows"]:
        name = str(row.get("ifname") or "")
        members = {m.strip() for m in str(row.get("members") or "").split(",") if m.strip()}
        prefixes = BUILTIN_GROUP_DEVICES.get(name)
        if prefixes:
            members |= {i.identifier for i in interfaces.values() if i.device.startswith(prefixes)}
        if name:
            groups[name] = members
    return groups


async def _optional(coro, note: str, notes: list[str]):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - optional source: degrade, never fail the analysis
        logger.info("rule analysis: optional source failed (%s): %s", note, exc)
        notes.append(note)
        return None


_CACHE: dict[str, Context] = {}


async def load_context(host: OpnsenseHost, *, use_cache: bool = True) -> Context:
    key = str(host.id)
    cached = _CACHE.get(key)
    if use_cache and cached and time.monotonic() - cached.loaded_at < CACHE_SECONDS:
        return cached
    notes: list[str] = []
    rules, info = await asyncio.gather(
        opnsense_client.search_rules(host, all_rows=True), opnsense_client.get_interfaces_info(host))
    stats, groups, aliases, forwards = await asyncio.gather(
        _optional(opnsense_client.get_pf_rule_statistics(host), "PF_RULESET_UNAVAILABLE", notes),
        _optional(opnsense_client.search_interface_groups(host), "GROUPS_UNAVAILABLE", notes),
        _optional(opnsense_client._request(host, "GET", "/firewall/alias/searchItem", params={"rowCount": -1}),
                  "ALIASES_UNAVAILABLE", notes),
        _optional(opnsense_client.search_port_forward_rules(host), "NAT_UNAVAILABLE", notes),
    )
    interfaces = _interfaces(info)
    pf = pf_ruleset.parse(stats)
    if stats is not None and pf is None:
        notes.append("PF_RULESET_UNAVAILABLE")
    ctx = Context(
        rows=list((rules or {}).get("rows") or []), pf=pf, interfaces=interfaces,
        groups=_groups(groups, interfaces),
        aliases={str(a.get("name")): a for a in ((aliases or {}).get("rows") or []) if a.get("name")},
        port_forwards=None if forwards is None else list(forwards.get("rows") or []),
        notes=notes, loaded_at=time.monotonic(),
    )
    _CACHE[key] = ctx
    return ctx


async def ensure_refs(host: OpnsenseHost, ctx: Context, ips: list[str]) -> None:
    """Ask pf which tables contain each IP (cached in the context)."""
    for ip in ips:
        if ip in ctx.refs:
            continue
        try:
            ctx.refs[ip] = await opnsense_client.find_alias_references(host, ip)
        except Exception as exc:  # noqa: BLE001 - table membership becomes "unknown"
            logger.info("rule analysis: findReferences failed: %s", exc)
            ctx.refs[ip] = None


def clear_cache() -> None:
    _CACHE.clear()
