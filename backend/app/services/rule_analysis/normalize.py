"""Turn ``searchRule`` rows (+ the loaded pf ruleset) into ``NormRule`` objects.

Format pitfalls seen live on OPNsense 26.7:
- automatic rows carry the interface *description* (``"LAN"``), MVC rows the identifier;
- automatic rows often have ``quick``/``action`` empty; the pf lines with the same label
  know the truth (``pass in quick on vtnet1_vlan10 ...``);
- empty source/destination means any; address lists are comma separated;
- unmigrated legacy rules have a 32-hex label instead of a UUID.

Order is ``sort_order`` (``prio.0seq`` from FilterRuleField::getPriority: 1/5 system,
10000 force gateway, 200000 floating, 300000+n groups, 400000 interface, 500000 automatic
NAT, 600000 = interface missing, never loaded).
"""
from __future__ import annotations

from app.services.rule_analysis.context import Context
from app.services.rule_analysis.model import (
    SECTION_ANTISPOOF, SECTION_FLOATING, SECTION_GROUP, SECTION_INTERFACE, SECTION_INVALID,
    SECTION_NAT_AUTO, SECTION_OTHER, SECTION_SYSTEM, Endpoint, NormRule,
)
from app.services.rule_analysis.pf_ruleset import PfLine

PROTO_NUMBERS = {
    "icmp": "1", "igmp": "2", "ggp": "3", "ipencap": "4", "tcp": "6", "egp": "8", "udp": "17",
    "ipv6": "41", "gre": "47", "esp": "50", "ah": "51", "ipv6-icmp": "58", "icmp6": "58",
    "icmpv6": "58", "ospf": "89", "pim": "103", "vrrp": "112", "carp": "112", "sctp": "132", "pfsync": "240",
}


def norm_proto(value: str) -> str:
    value = str(value).strip().lower()
    return PROTO_NUMBERS.get(value, value)


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _section(prio: int) -> str:
    if prio < 10001:
        return SECTION_SYSTEM
    if prio == 200000:
        return SECTION_FLOATING
    if 300000 <= prio < 400000:
        return SECTION_GROUP
    if prio == 400000:
        return SECTION_INTERFACE
    if prio == 500000:
        return SECTION_NAT_AUTO
    if prio >= 600000:
        return SECTION_INVALID
    return SECTION_OTHER


def _order(row: dict) -> tuple[tuple[int, int], bool]:
    raw = str(row.get("sort_order") or "")
    if "." in raw:
        head, _, tail = raw.partition(".")
        if head.isdigit() and tail.isdigit():
            return (int(head), int(tail)), True
    # Older releases without sort_order: best effort from prio_group/sequence (confidence drops).
    prio = str(row.get("prio_group") or row.get("#priority") or "")
    seq = str(row.get("sequence") or "")
    return (int(prio) if prio.isdigit() else 400000, int(seq) if seq.isdigit() else 0), False


def _endpoint(row: dict, side: str) -> Endpoint:
    raw = str(row.get(f"{side}_net") or "").strip()
    items = [] if raw.lower() in ("", "any") else [i.strip() for i in raw.split(",") if i.strip()]
    return Endpoint(raw=raw, negated=_truthy(row.get(f"{side}_not")), items=items)


def _af(value: str) -> set[int]:
    value = str(value or "").strip().lower()
    return {4} if value == "inet" else ({6} if value == "inet6" else {4, 6})


def _protos(value: str) -> set[str] | None:
    value = str(value or "").strip().lower()
    if value in ("", "any"):
        return None
    return {norm_proto(p) for p in value.split("/") if p}


def _pf_interfaces(lines: list[PfLine], ctx: Context) -> tuple[set[str], bool] | None:
    devices = {(line.on_device, line.on_negated) for line in lines}
    if any(device is None for device, _ in devices):
        return set(), False
    if len({neg for _, neg in devices}) != 1:
        return None
    names: set[str] = set()
    for device, _ in devices:
        info = ctx.by_device(device)
        if info is not None:
            names.add(info.identifier)
        elif ctx.groups is not None and device in ctx.groups:
            names.add(device)
        else:
            return None
    return names, next(iter(devices))[1]


def normalize_row(row: dict, ctx: Context) -> NormRule:
    order, order_known = _order(row)
    label = str(row.get("uuid") or "")
    automatic = _truthy(row.get("is_automatic"))
    lines = ctx.pf.by_label.get(label, []) if ctx.pf else []
    rule = NormRule(
        uuid=label, label=label, description=str(row.get("description") or ""),
        order=order, order_known=order_known, section=_section(order[0]),
        enabled=_truthy(row.get("enabled", "1")),
        action=(str(row.get("action") or "").lower() or None),
        quick=None, interfaces=set(), if_not=_truthy(row.get("interfacenot")),
        direction=(str(row.get("direction") or "in").lower() or "in"),
        af=_af(row.get("ipprotocol")), protos=_protos(row.get("protocol")),
        src=_endpoint(row, "source"), dst=_endpoint(row, "destination"),
        sport=str(row.get("source_port") or "").strip(), dport=str(row.get("destination_port") or "").strip(),
        tag=str(row.get("tag") or ""), tagged=str(row.get("tagged") or ""),
        received_on=str(row.get("received-on") or row.get("received_on") or ""),
        gateway=str(row.get("gateway") or ""), statetype=str(row.get("statetype") or ""),
        sched=str(row.get("sched") or ""), automatic=automatic, legacy=_truthy(row.get("legacy")),
        pf_numbers=[line.number for line in lines],
    )
    if rule.action not in ("pass", "block", "reject"):
        rule.action = None
    if row.get("quick") not in (None, ""):
        rule.quick = _truthy(row.get("quick"))
    raw_if = str(row.get("interface") or "").strip()
    names = {n.strip() for n in raw_if.split(",") if n.strip()}
    # Automatic and unmigrated legacy rows carry descriptions ("LAN"): map them to identifiers.
    # Identifiers and group names stay as they are; the pf "on" clause decides when available.
    mapped = set()
    for name in names:
        info = None if name in ctx.interfaces or name in (ctx.groups or {}) else ctx.by_label(name)
        mapped.add(info.identifier if info else name)
    names = mapped
    rule.interfaces = names
    if ctx.pf is not None:
        rule.loaded = bool(lines) and rule.enabled
        if lines:
            actions = {line.action for line in lines}
            if len(actions) == 1 and next(iter(actions)) in ("pass", "block", "reject"):
                pf_action = next(iter(actions))
                if rule.action is None or automatic:
                    rule.action = pf_action
            quick = {line.quick for line in lines}
            rule.quick = next(iter(quick)) if len(quick) == 1 else None
            if automatic or rule.legacy:
                on = _pf_interfaces(lines, ctx)
                if on is not None:
                    rule.interfaces, rule.if_not = on
                directions = {line.direction for line in lines}
                if len(directions) == 1:
                    rule.direction = next(iter(directions))
            families = {line.af for line in lines}
            if None not in families:
                rule.af &= families
            types = {t for line in lines for t in line.icmptypes}
            if types:
                rule.icmptypes = types
            if any(line.flags not in (None, "S/SA", "any") for line in lines):
                rule.extra_notes.append("TCP_FLAGS")
            if any(word in line.text for line in lines for word in ("max-src-", "overload", "max-pkt-rate")):
                rule.extra_notes.append("RATE_LIMIT")
    elif rule.section == SECTION_INVALID:
        rule.loaded = False
    if rule.icmptypes is None and str(row.get("icmptype") or "").strip():
        rule.icmptypes = {t.strip() for t in str(row["icmptype"]).split(",") if t.strip()}
    return rule


def antispoof_rule(line: PfLine, order: tuple[int, int]) -> NormRule:
    """An unlabeled pf line OPNsense generated itself (antispoof / own-address block)."""
    return NormRule(
        uuid=f"pf@{line.number}", label="", description=line.text, order=order, order_known=True,
        section=SECTION_ANTISPOOF, enabled=True, action=line.action if line.action != "other" else None,
        quick=line.quick, interfaces=set(), if_not=False, direction=line.direction,
        af={line.af} if line.af else {4, 6}, protos=None,
        src=Endpoint(raw="" if line.src == "any" else line.src, items=[] if line.src == "any" else [line.src]),
        dst=Endpoint(raw="" if line.dst == "any" else line.dst, items=[] if line.dst == "any" else [line.dst]),
        sport="", dport="", automatic=True, loaded=True, pf_numbers=[line.number], pf_line=line,
    )


def normalize(ctx: Context) -> tuple[list[NormRule], list[str]]:
    """All rules in evaluation order plus context notes (order problems)."""
    notes: list[str] = []
    rules = [normalize_row(row, ctx) for row in ctx.rows]
    if any(not r.order_known for r in rules):
        notes.append("ORDER_GUESSED")
    rules.sort(key=lambda r: (r.order, r.uuid))
    if ctx.pf is not None:
        loaded = [r for r in rules if r.loaded and r.pf_numbers]
        firsts = [min(r.pf_numbers) for r in loaded]
        if firsts != sorted(firsts):
            notes.append("ORDER_MISMATCH")
        # Unlabeled pf lines: placed before the first configured rule that pf evaluates later.
        extra: list[tuple[tuple, NormRule]] = []
        for line in ctx.pf.unlabeled:
            after = next((r for r in loaded if min(r.pf_numbers) > line.number), None)
            key = after.order if after else (999999, 9999999)
            spoof = antispoof_rule(line, key)
            if line.on_device:
                info = ctx.by_device(line.on_device)
                spoof.interfaces = {info.identifier if info else line.on_device}
                spoof.if_not = line.on_negated
            extra.append(((key, 0, line.number), spoof))
        keyed = [((r.order, 1, 0), r) for r in rules] + extra
        keyed.sort(key=lambda item: item[0])
        rules = [r for _, r in keyed]
    return rules, notes
