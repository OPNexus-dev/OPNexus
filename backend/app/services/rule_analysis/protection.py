"""May a rule be switched off without touching the way the admin reaches the firewall? (DESIGN-1.31 §3.2/§3.3)

Pure functions on ``NormRule`` and the analysis ``Context``; no I/O.

Management access (§3.2). For every configured management source (IP or network), every address of the
firewall (the address in ``host.base_url`` plus every interface address and VIP) and every management port
(the API port of ``base_url``, 443, 80, 22) and every interface the packet could arrive on, the rule's own
fields are compared with that packet (``engine.match_rule``). A rule is safe only when EVERY such comparison is
``False``. ``True`` or unknown means the rule is about management traffic: it is protected.

Why that is enough without simulating "the ruleset afterwards": a rule that does not match a packet takes no part
in its evaluation at all - it neither decides it nor tags it (pf only tags on a match) - so removing it cannot
change what happens to that packet. This holds whatever the verdict for the packet is, even when the analysis
of the whole ruleset is only "probable". The packet's ingress interface is a routing question OPNexus cannot prove
(asymmetric routes, VPNs, failover), so every interface is tried: that only ever protects more.

A management source that is a network is judged for all of its addresses at once: up to 256 addresses are
enumerated (exact, with every resolver rule of the analysis), larger networks are compared as address ranges
with the rule's literal/alias/interface addresses; anything that cannot be resolved is unknown = protected.

Infrastructure (§3.3, reason ``infrastructure``): CARP (IP protocol 112) and pfsync (240) rules, every rule that
could match CARP advertisements (to 224.0.0.18 / ff02::12 from the interface's own networks) on an interface with
a CARP VIP, and every rule that could match pfsync traffic on the pfsync interface (System > High Availability,
``core/hasync/get``; when that cannot be read on a cluster node, on every interface). The second leash of the
design ("(self) with port 22/80/443/API" when §3.2 is not False) is part of §3.2 already: such a rule is
protected there.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from app.services.rule_analysis import coverage, resolve
from app.services.rule_analysis.context import BUILTIN_GROUP_DEVICES, Context
from app.services.rule_analysis.engine import Query, match_rule
from app.services.rule_analysis.model import Endpoint, IPAddr, IPNet, NormRule, Outcome, o_or, t_not

MANAGEMENT_PORTS = (443, 80, 22)
# A management network up to this size is checked address by address (exact); larger ones as address ranges.
ENUMERATE_LIMIT = 256
CARP_PROTO, PFSYNC_PROTO = "112", "240"
CARP_GROUPS = {4: ipaddress.ip_address("224.0.0.18"), 6: ipaddress.ip_address("ff02::12")}
PFSYNC_GROUP = ipaddress.ip_address("224.0.0.240")
_LOOPBACK = {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}


def parse_sources(values) -> list[IPNet]:
    """The configured management sources as networks (an address becomes a /32 or /128). Invalid entries are
    dropped; the caller treats an empty result as "not configured" (the cleanup is then locked)."""
    nets: list[IPNet] = []
    for value in values or []:
        try:
            net = ipaddress.ip_network(str(value).strip(), strict=False)
        except ValueError:
            continue
        if net not in nets:
            nets.append(net)
    return nets


def management_targets(base_url: str, ctx: Context, extra_ports=()) -> tuple[list[IPAddr], list[int]]:
    """Firewall addresses and ports the management traffic goes to; ``extra_ports`` = the admin's
    ``cleanup_management_ports`` (an SSH or GUI port moved away from the defaults)."""
    parts = urlsplit(base_url or "")
    addrs: list[IPAddr] = []
    try:
        addrs.append(ipaddress.ip_address((parts.hostname or "").strip("[]")))
    except ValueError:
        pass  # a DNS name: the interface addresses below cover the address it resolves to on the firewall
    for addr in ctx.self_addrs:
        if addr not in _LOOPBACK and addr not in addrs:
            addrs.append(addr)
    try:
        api_port = parts.port or 443
    except ValueError:
        api_port = 443
    ports = [api_port]
    for port in (*MANAGEMENT_PORTS, *extra_ports):
        if isinstance(port, int) and 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return addrs, ports


def _interfaces(ctx: Context) -> list[str]:
    return sorted(i for i in ctx.interfaces if i != "lo0")


# A packet can also arrive on a device that is not an assigned interface: a WireGuard instance (wg0), an OpenVPN
# server (ovpns1), IPsec (enc0). They have no identifier, so they are not in ``ctx.interfaces``, and the built-in
# groups that stand for them (``wireguard``, ``openvpn``, ``enc0``) list no members in ``firewall/group/searchItem``
# (live fixture tests/fixtures/rule_analysis_fw.json: members ""). One extra, virtual ingress is checked per query:
UNASSIGNED = "(unassigned)"


def unassigned_interface(rule: NormRule, ctx: Context) -> Outcome:
    """Could the rule's interface field match a packet arriving on an unassigned device? An assigned interface or a
    user-defined group (its members are assigned interfaces) cannot; a built-in group or an unknown name might
    (members unknown = protected); no interface (floating "any") always does."""
    if not rule.interfaces:
        return Outcome(True)
    results = []
    for name in rule.interfaces:
        if name in ctx.interfaces:
            results.append(Outcome(False))
        elif name in BUILTIN_GROUP_DEVICES or ctx.groups is None or name not in ctx.groups:
            results.append(Outcome(None, ["IFACE_MEMBERS_UNKNOWN"]))
        else:
            results.append(Outcome(False))
    result = o_or(results)
    return Outcome(t_not(result.value), result.reasons) if rule.if_not else result


def _ingress(rule: NormRule, ctx: Context) -> list[tuple[str, dict | None]]:
    """(interface, field overrides) for every ingress a management packet could take."""
    virtual = {"interface": unassigned_interface(rule, ctx)}
    if rule.received_on:
        virtual["received_on"] = Outcome(None, ["RECEIVED_ON"])
    return [(i, None) for i in _interfaces(ctx)] + [(UNASSIGNED, virtual)]


def _item_ranges(token: str, ctx: Context) -> list[coverage.Range] | None:
    """Address ranges an endpoint item stands for; None when that is not static."""
    ranges, _alias = coverage._ranges_of(token, ctx)
    if ranges is not None:
        return ranges
    name = token.strip().lower()
    if name in ("(self)", "self", "this firewall"):
        return [(a.version, int(a), int(a)) for a in ctx.self_addrs]
    if name.endswith("ip") and name[:-2] in ctx.interfaces:
        info = ctx.interfaces[name[:-2]]
        return [(a.version, int(a), int(a)) for a in (*info.addrs, *info.vips)]
    if name.startswith("__") and name.endswith("_network"):
        name = name[2:-len("_network")]
    if name in ctx.interfaces:
        return [(n.version, int(n.network_address), int(n.broadcast_address)) for n in ctx.interfaces[name].nets]
    return None


def _covers_all(ranges: list[coverage.Range], version: int, low: int, high: int) -> bool:
    cursor = low
    for _v, a, b in sorted(r for r in ranges if r[0] == version):
        if a > cursor:
            return False
        cursor = max(cursor, b + 1)
        if cursor > high:
            return True
    return cursor > high


def source_outcome(endpoint: Endpoint, net: IPNet, ctx: Context) -> Outcome:
    """Does the endpoint match at least one address of ``net``? (False is a proof for every address.)"""
    if endpoint.is_any:
        return Outcome(True)
    if net.num_addresses <= ENUMERATE_LIMIT:
        return o_or([resolve.endpoint_match(endpoint, ip, ctx) for ip in net])
    ranges: list[coverage.Range] = []
    for item in endpoint.items:
        got = _item_ranges(item, ctx)
        if got is None:
            return Outcome(None, ["SOURCE_NOT_RESOLVABLE"])
        ranges += got
    low, high = int(net.network_address), int(net.broadcast_address)
    if endpoint.negated:
        return Outcome(not _covers_all(ranges, net.version, low, high))
    return Outcome(any(v == net.version and a <= high and b >= low for v, a, b in ranges))


def _query(src: IPAddr, dst: IPAddr, proto: str, label: str, dport: int | None) -> Query:
    return Query(src=src, dst=dst, proto=proto, proto_label=label, dport=dport, direction="in", egress="none")


def management_check(rule: NormRule, ctx: Context, sources: list[IPNet], targets: list[IPAddr],
                     ports: list[int]) -> dict:
    """{"result": "false" | "true" | "unknown", "queries": n, "reasons": [...], "match": {...} | None}.
    Only "false" makes the rule removable."""
    reasons: set[str] = set()
    queries = 0
    unknown_at = None
    interfaces = _ingress(rule, ctx)
    for net in sources:
        src = source_outcome(rule.src, net, ctx)
        if src.value is False:
            dsts = [d for d in targets if d.version == net.version]
            queries += len(dsts) * len(ports) * len(interfaces)
            continue
        for dst in (d for d in targets if d.version == net.version):
            for port in ports:
                q = _query(net.network_address, dst, "6", "tcp", port)
                for iface, overrides in interfaces:
                    queries += 1
                    out = match_rule(rule, q, ctx, iface=iface, source=src, overrides=overrides)
                    where = {"source": str(net), "destination": str(dst), "port": port, "interface": iface}
                    if out.value is True:
                        return {"result": "true", "queries": queries, "reasons": [], "match": where}
                    if out.value is None:
                        reasons.update(out.reasons)
                        unknown_at = unknown_at or where
    if not queries:
        # No address of the firewall in the family of any source: nothing could be proven.
        return {"result": "unknown", "queries": 0, "reasons": ["NO_MANAGEMENT_QUERY"], "match": None}
    if unknown_at is not None:
        return {"result": "unknown", "queries": queries, "reasons": sorted(reasons), "match": unknown_at}
    return {"result": "false", "queries": queries, "reasons": [], "match": None}


def _hasync_interface(hasync: dict | None) -> str | None:
    """The pfsync interface from ``core/hasync/get`` ("" = HA sync disabled); None when not readable."""
    node = (hasync or {}).get("hasync") if isinstance(hasync, dict) else None
    if not isinstance(node, dict) or "pfsyncinterface" not in node:
        return None
    value = node.get("pfsyncinterface")
    if isinstance(value, dict):
        return next((k for k, o in value.items() if isinstance(o, dict) and str(o.get("selected")) == "1"), "")
    return str(value or "")


def _hasync_peer(hasync: dict | None) -> IPAddr | None:
    node = (hasync or {}).get("hasync") if isinstance(hasync, dict) else None
    try:
        return ipaddress.ip_address(str((node or {}).get("pfsyncpeerip") or "").strip())
    except ValueError:
        return None


def infrastructure_reason(rule: NormRule, ctx: Context, *, hasync: dict | None, clustered: bool) -> str | None:
    """'infrastructure' when the rule could carry CARP or pfsync traffic, else None."""
    if rule.protos is not None and rule.protos & {CARP_PROTO, PFSYNC_PROTO}:
        return "infrastructure"
    for info in ctx.interfaces.values():
        if info.identifier == "lo0" or not info.vips:
            continue
        for net in info.nets:
            group = CARP_GROUPS.get(net.version)
            out = match_rule(rule, _query(net.network_address, group, CARP_PROTO, "carp", None), ctx,
                             iface=info.identifier, source=source_outcome(rule.src, net, ctx))
            if out.value is not False:
                return "infrastructure"
    pfsync_if = _hasync_interface(hasync)
    if pfsync_if == "" or (pfsync_if is None and not clustered):
        return None  # HA sync off (or a single firewall whose HA settings are not readable)
    peer = _hasync_peer(hasync)
    candidates = [ctx.interfaces[pfsync_if]] if pfsync_if in ctx.interfaces else \
        [i for i in ctx.interfaces.values() if i.identifier != "lo0"]  # unknown: every interface
    for info in candidates:
        for net in info.nets:
            if peer is not None and peer.version == net.version:
                src = Outcome(resolve.endpoint_match(rule.src, peer, ctx).value)
                src_ip = peer
            else:
                src, src_ip = source_outcome(rule.src, net, ctx), net.network_address
            for dst in [*(a for a in info.addrs if a.version == net.version)] + ([PFSYNC_GROUP] if net.version == 4 else []):
                out = match_rule(rule, _query(src_ip, dst, PFSYNC_PROTO, "pfsync", None), ctx,
                                 iface=info.identifier, source=src)
                if out.value is not False:
                    return "infrastructure"
    return None
