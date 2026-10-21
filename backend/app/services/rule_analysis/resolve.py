"""Does a rule field match the queried packet? Addresses, interfaces, aliases, ports.

Every function answers with an ``Outcome`` (True/False/None + reason codes). Whatever cannot
be proven from the firewall's own data is ``None`` (unknown), never a guess.
"""
from __future__ import annotations

import ipaddress
import re

from app.services.rule_analysis.context import Context
from app.services.rule_analysis.model import IPAddr, Outcome, o_or, t_not

MAX_ALIAS_DEPTH = 10
TABLE_ONLY_TYPES = {"url", "urltable", "geoip", "asn", "mac", "dynipv6host", "authgroup", "external", "internal"}
ADDRESS_TYPES = {"host", "network", "networkgroup"}

SERVICES = {
    "ftp-data": 20, "ftp": 21, "ssh": 22, "telnet": 23, "smtp": 25, "domain": 53, "dns": 53, "bootps": 67,
    "bootpc": 68, "tftp": 69, "http": 80, "www": 80, "kerberos": 88, "pop3": 110, "ntp": 123, "imap": 143,
    "snmp": 161, "snmptrap": 162, "ldap": 389, "https": 443, "smb": 445, "microsoft-ds": 445, "isakmp": 500,
    "syslog": 514, "submission": 587, "ldaps": 636, "imaps": 993, "pop3s": 995, "openvpn": 1194,
    "ms-sql-s": 1433, "radius": 1812, "radius-acct": 1813, "nfs": 2049, "mysql": 3306, "rdp": 3389,
    "ipsec-nat-t": 4500, "sip": 5060, "postgresql": 5432, "dhcpv6-client": 546, "dhcpv6-server": 547,
    "http-alt": 8080, "wireguard": 51820,
}
EPHEMERAL = (1024, 65535)


def _ip_in(token: str, ip: IPAddr) -> bool | None:
    """Literal IP / CIDR / range; None when the token is not a literal."""
    token = token.strip()
    if "-" in token and not token.startswith("-"):
        low, _, high = token.partition("-")
        try:
            low_ip, high_ip = ipaddress.ip_address(low.strip()), ipaddress.ip_address(high.strip())
        except ValueError:
            return None
        return low_ip.version == ip.version and low_ip <= ip <= high_ip
    try:
        net = ipaddress.ip_network(token, strict=False)
    except ValueError:
        return None
    return net.version == ip.version and ip in net


def _interface_ref(token: str, ip: IPAddr, ctx: Context) -> Outcome | None:
    name = token.strip()
    lower = name.lower()
    m = re.fullmatch(r"__(.+)_network", lower)
    if m and m.group(1) in ctx.interfaces:
        lower = m.group(1)
    info = ctx.interfaces.get(lower)
    if info is not None:  # "lan" = the interface's networks (pf: (dev:network), incl. VIP subnets)
        return Outcome(any(ip in net for net in info.nets))
    if lower.endswith("ip") and lower[:-2] in ctx.interfaces:  # "lanip" = the interface address
        info = ctx.interfaces[lower[:-2]]
        if ip in info.addrs:
            return Outcome(True)
        if ip in info.vips:
            return Outcome(None, ["VIP_AS_INTERFACE_ADDRESS"])
        return Outcome(False)
    return None


def addr_item(token: str, ip: IPAddr, ctx: Context, *, depth: int = 0, seen: tuple = ()) -> Outcome:
    token = token.strip()
    if token.lower() in ("any", ""):
        return Outcome(True)
    if token.lower() in ("(self)", "self", "this firewall"):
        return Outcome(ip in ctx.self_addrs)
    literal = _ip_in(token, ip)
    if literal is not None:
        return Outcome(literal)
    ref = _interface_ref(token, ip, ctx)
    if ref is not None:
        return ref
    return alias_contains(token, ip, ctx, depth=depth, seen=seen)


def alias_contains(name: str, ip: IPAddr, ctx: Context, *, depth: int = 0, seen: tuple = ()) -> Outcome:
    if name in seen:
        return Outcome(None, ["ALIAS_CYCLE"])
    if depth > MAX_ALIAS_DEPTH:
        return Outcome(None, ["ALIAS_TOO_DEEP"])
    alias = ctx.aliases.get(name)
    refs = ctx.refs.get(str(ip))
    if alias is None:
        # pf knows tables the alias list may not show (e.g. internal ones): trust a positive hit.
        if refs is not None and name in refs:
            return Outcome(True)
        return Outcome(None, ["ALIAS_MISSING"])
    kind = str(alias.get("type") or "").lower()
    if depth == 0 and refs is not None:
        # pf's own table test: authoritative for every alias type (nesting, "!" entries, FQDNs).
        return Outcome(name in refs)
    if kind in ADDRESS_TYPES:
        return _alias_content(alias, ip, ctx, depth, (*seen, name))
    if refs is not None:
        return Outcome(name in refs)
    return Outcome(None, ["ALIAS_TABLE_UNAVAILABLE"])


def _alias_content(alias: dict, ip: IPAddr, ctx: Context, depth: int, seen: tuple) -> Outcome:
    entries = [e.strip() for e in re.split(r"[\n,]", str(alias.get("content") or "")) if e.strip()]
    if any(e.startswith("!") for e in entries):
        return Outcome(None, ["ALIAS_EXCLUSION"])
    results = []
    for entry in entries:
        literal = _ip_in(entry, ip)
        if literal is not None:
            results.append(Outcome(literal))
        elif entry in ctx.aliases or entry in seen:
            results.append(alias_contains(entry, ip, ctx, depth=depth + 1, seen=seen))
        elif "." in entry or ":" in entry:
            results.append(Outcome(None, ["FQDN_UNRESOLVED"]))
        else:
            results.append(Outcome(None, ["ALIAS_MISSING"]))
    return o_or(results)


def endpoint_match(endpoint, ip: IPAddr, ctx: Context) -> Outcome:
    if endpoint.is_any:
        return Outcome(True)
    result = o_or([addr_item(item, ip, ctx) for item in endpoint.items])
    if endpoint.negated:
        return Outcome(t_not(result.value), result.reasons)
    return result


# ── Ports ─────────────────────────────────────────────────────────────────

def port_ranges(spec: str, ctx: Context, *, depth: int = 0, seen: tuple = ()) -> tuple[list[tuple[int, int]] | None, list[str]]:
    """The port ranges a spec stands for; (None, reasons) when that cannot be known."""
    ranges: list[tuple[int, int]] = []
    reasons: list[str] = []
    for part in [p.strip() for p in re.split(r"[\n,]", spec) if p.strip()]:
        m = re.fullmatch(r"(\w[\w-]*?)\s*[-:]\s*(\w[\w-]*)", part) if not part.isdigit() and part not in ctx.aliases else None
        if part.isdigit():
            ranges.append((int(part), int(part)))
        elif m and _port_number(m.group(1)) is not None and _port_number(m.group(2)) is not None:
            ranges.append((_port_number(m.group(1)), _port_number(m.group(2))))
        elif part.lower() in SERVICES:
            ranges.append((SERVICES[part.lower()],) * 2)
        elif part in ctx.aliases:
            if part in seen or depth >= MAX_ALIAS_DEPTH:
                return None, ["ALIAS_CYCLE" if part in seen else "ALIAS_TOO_DEEP"]
            alias = ctx.aliases[part]
            if str(alias.get("type") or "").lower() != "port":
                return None, ["PORT_ALIAS_UNRESOLVED"]
            sub, why = port_ranges(str(alias.get("content") or ""), ctx, depth=depth + 1, seen=(*seen, part))
            if sub is None:
                return None, why
            ranges += sub
        else:
            return None, ["PORT_ALIAS_UNRESOLVED" if part.isidentifier() else "PORT_UNPARSEABLE"]
    return ranges, reasons


def _port_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return SERVICES.get(token.lower())


def port_match(spec: str, port: int | None, ctx: Context, *, ephemeral_default: bool = False) -> Outcome:
    spec = (spec or "").strip()
    if spec.lower() in ("", "any"):
        return Outcome(True)
    ranges, reasons = port_ranges(spec, ctx)
    if ranges is None:
        return Outcome(None, reasons)
    if port is not None:
        return Outcome(any(low <= port <= high for low, high in ranges))
    if not ephemeral_default:
        return Outcome(None, ["PORT_NOT_GIVEN"])
    # No source port given: the query stands for an ephemeral client port (1024-65535).
    low_e, high_e = EPHEMERAL
    overlap = [(max(low, low_e), min(high, high_e)) for low, high in ranges if high >= low_e and low <= high_e]
    if not overlap:
        return Outcome(False)
    covered, cursor = True, low_e
    for low, high in sorted(overlap):
        if low > cursor:
            covered = False
            break
        cursor = max(cursor, high + 1)
    if covered and cursor > high_e:
        return Outcome(True)
    return Outcome(None, ["SRC_PORT_NOT_GIVEN"])


# ── Interfaces ────────────────────────────────────────────────────────────

def iface_match(interfaces: set[str], if_not: bool, iface: str, ctx: Context) -> Outcome:
    if not interfaces:
        return Outcome(True)
    results = []
    for name in interfaces:
        if name == iface:
            results.append(Outcome(True))
        elif name in ctx.interfaces:
            results.append(Outcome(False))
        elif ctx.groups is not None and name in ctx.groups:
            results.append(Outcome(iface in ctx.groups[name]))
        else:
            results.append(Outcome(None, ["IFACE_UNRESOLVED" if ctx.groups is not None else "GROUPS_UNAVAILABLE"]))
    result = o_or(results)
    if if_not:
        return Outcome(t_not(result.value), result.reasons)
    return result
