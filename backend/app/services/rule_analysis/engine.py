"""Which rule decides a packet, following pf semantics.

pf walks the loaded ruleset top to bottom; the *last* matching rule wins unless a matching
rule is ``quick``, which ends the walk. OPNsense orders rules by ``sort_order`` (system,
floating, groups, interfaces). The analysis looks at the first packet of a new connection.

Confidence:
- ``rule_confidence`` is "sure" only when no rule that could still change the outcome
  matched with an unknown result;
- ``confidence`` (the verdict) is "sure" when every such unknown rule has the same action as
  the deciding rule (then the verdict holds either way), "unknown" otherwise;
- notes about guesses (interface guessed from the address, NAT nearby, pf ruleset missing,
  TCP flags, rate limits) lower a "sure" to "probable".
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from app.services.rule_analysis import resolve
from app.services.rule_analysis.context import Context
from app.services.rule_analysis.model import IPAddr, NormRule, Outcome, t_and, tri_str
from app.services.rule_analysis.normalize import norm_proto, normalize

SURE, PROBABLE, UNKNOWN = "sure", "probable", "unknown"
_RANK = {SURE: 2, PROBABLE: 1, UNKNOWN: 0}
DOWNGRADING_NOTES = {
    "IFACE_GUESSED", "EGRESS_GUESSED", "NAT_NEARBY", "TCP_FLAGS", "RATE_LIMIT", "ORDER_GUESSED",
    "ORDER_MISMATCH", "PF_RULESET_UNAVAILABLE", "NAT_UNAVAILABLE", "QUICK_UNKNOWN",
}
_PORT_PROTOS = {"6", "17"}
_ICMP_PROTOS = {"1", "58"}
ICMP_NAMES = {
    4: {"0": "echorep", "3": "unreach", "4": "squench", "5": "redir", "8": "echoreq", "9": "routeradv",
        "10": "routersol", "11": "timex", "12": "paramprob", "13": "timereq", "14": "timerep"},
    6: {"1": "unreach", "2": "toobig", "3": "timex", "4": "paramprob", "128": "echoreq", "129": "echorep",
        "133": "routersol", "134": "routeradv", "135": "neighbrsol", "136": "neighbradv", "137": "redir"},
}


@dataclass
class Query:
    src: IPAddr
    dst: IPAddr
    proto: str                 # normalized ("6", "17", "1", "58", other number or name)
    proto_label: str
    sport: int | None = None
    dport: int | None = None
    icmptype: str | None = None
    iface: str | None = None   # None = guess from the source address
    direction: str = "in"
    egress: str | None = "none"  # "none" = only the first leg, None = guess, else identifier

    @property
    def af(self) -> int:
        return self.src.version


@dataclass
class LegResult:
    direction: str
    iface: str | None
    iface_source: str
    action: str | None
    decisive: NormRule | None
    confidence: str
    rule_confidence: str
    notes: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)


def _min_conf(*values: str) -> str:
    return min(values, key=lambda v: _RANK[v])


def _max_conf(*values: str) -> str:
    return max(values, key=lambda v: _RANK[v])


def _icmp_name(q: Query) -> str | None:
    if q.icmptype is None:
        return None
    return ICMP_NAMES[q.af].get(q.icmptype, q.icmptype)


def rule_fields(r: NormRule, q: Query, direction: str, iface: str, ctx: Context,
                tags: set[str], first_leg_iface: str | None) -> dict[str, Outcome]:
    fields: dict[str, Outcome] = {
        "interface": resolve.iface_match(r.interfaces, r.if_not, iface, ctx),
        "direction": Outcome(r.direction in ("any", direction)),
        "family": Outcome(q.af in r.af),
        "protocol": Outcome(True if r.protos is None else q.proto in r.protos),
        "source": resolve.endpoint_match(r.src, q.src, ctx),
        "destination": resolve.endpoint_match(r.dst, q.dst, ctx),
    }
    if q.proto in _PORT_PROTOS:
        fields["source_port"] = resolve.port_match(r.sport, q.sport, ctx, ephemeral_default=True)
        fields["destination_port"] = resolve.port_match(r.dport, q.dport, ctx)
    elif r.sport or r.dport:
        fields["ports"] = Outcome(False)  # pf only generates port rules for tcp/udp
    if r.icmptypes is not None and q.proto in _ICMP_PROTOS:
        name = _icmp_name(q)
        fields["icmp_type"] = Outcome(None, ["ICMP_TYPE_NOT_GIVEN"]) if name is None else \
            Outcome(name in r.icmptypes or q.icmptype in r.icmptypes)
    if r.tagged:
        fields["tagged"] = Outcome(True) if r.tagged in tags else Outcome(None, ["TAGGED"])
    if r.received_on:
        if direction == "in":
            fields["received_on"] = Outcome(r.received_on == iface)
        elif first_leg_iface:
            fields["received_on"] = Outcome(r.received_on == first_leg_iface)
        else:
            fields["received_on"] = Outcome(None, ["RECEIVED_ON"])
    if r.action is None:
        fields["action"] = Outcome(None, ["ACTION_UNKNOWN"])
    return fields


def match_rule(r: NormRule, q: Query, ctx: Context, *, iface: str | None = None,
               source: Outcome | None = None, overrides: dict[str, Outcome] | None = None) -> Outcome:
    """Could the rule's own fields match the packet ``q`` arriving on ``iface``? Order, ``quick``, other rules and
    whether the rule is enabled or loaded play no part: this is the question "is this rule about such a packet at
    all", which the cleanup asks about the way the admin reaches the firewall (DESIGN-1.31 §3.2).

    ``iface`` defaults to ``q.iface`` and then to the interface guessed from the address; an interface that cannot
    be resolved is UNKNOWN. ``source`` replaces the source field (the caller judged it for a whole network, see
    rule_analysis/protection.py); ``overrides`` replaces further fields by name (e.g. ``interface`` for a packet
    arriving on a device that is not an assigned interface). Tags set by earlier rules are not known here, so ``tagged`` is UNKNOWN. Only
    ``False`` is a proof that the rule does not match; ``None`` (unknown) must be treated like ``True``."""
    iface = iface or q.iface or guess_iface(q.src if q.direction == "in" else q.dst, ctx)
    if iface is None:
        return Outcome(None, ["IFACE_UNRESOLVED"])
    fields = rule_fields(r, q, q.direction, iface, ctx, set(), None)
    if source is not None:
        fields["source"] = source
    for name, outcome in (overrides or {}).items():
        if name in fields or name == "interface":
            fields[name] = outcome
    value = t_and(*(f.value for f in fields.values()))
    reasons = sorted({reason for f in fields.values() if f.value is None for reason in f.reasons}) if value is None else []
    return Outcome(value, reasons)


def _step(r: NormRule, fields: dict[str, Outcome], result, effect: str) -> dict:
    return {
        "rule": r.summary(), "result": tri_str(result), "effect": effect,
        "fields": {k: tri_str(v.value) for k, v in fields.items()},
        "reasons": sorted({reason for v in fields.values() if v.value is None for reason in v.reasons}),
    }


def eval_leg(rules: list[NormRule], q: Query, ctx: Context, direction: str, iface: str,
             first_leg_iface: str | None = None) -> LegResult:
    steps: list[dict] = []
    candidate: NormRule | None = None
    possible: list[tuple[NormRule, bool]] = []   # (rule, came after the current candidate)
    tags: set[str] = set()
    final: NormRule | None = None
    for r in rules:
        if not r.enabled:
            steps.append({"rule": r.summary(), "result": "false", "effect": "disabled", "fields": {}, "reasons": []})
            continue
        if r.loaded is False:
            steps.append({"rule": r.summary(), "result": "false", "effect": "not_loaded", "fields": {}, "reasons": []})
            continue
        fields = rule_fields(r, q, direction, iface, ctx, tags, first_leg_iface)
        result = t_and(*(f.value for f in fields.values()))
        if result is False:
            steps.append(_step(r, fields, result, "no_match"))
            continue
        if result is None or r.quick is None:
            possible.append((r, True))
            steps.append(_step(r, fields, result, "possible"))
            continue
        if r.quick:
            final = r
            for s in steps:
                if s["effect"] == "candidate":
                    s["effect"] = "overridden"
            steps.append(_step(r, fields, result, "final"))
            break
        if candidate is not None:
            for s in steps:
                if s["effect"] == "candidate":
                    s["effect"] = "overridden"
        candidate = r
        possible = [(p, False) for p, _ in possible]
        if r.tag:
            tags.add(r.tag)
        steps.append(_step(r, fields, result, "candidate"))
    decisive = final or candidate
    if final is None:
        for s in steps:
            if s["effect"] == "candidate":
                s["effect"] = "decisive"
    else:
        steps[-1]["effect"] = "decisive"
    action = decisive.action if decisive else "pass"  # pf without any matching rule passes
    # Unknown matches that could still change the outcome: quick (or quick-unknown) ones always,
    # non-quick ones only after the deciding candidate (an earlier one is overridden anyway).
    relevant = [p for p, after in possible if p.quick is not False or (final is None and after)]
    rule_conf = SURE if not relevant else PROBABLE
    conf = SURE if all(p.action == action for p in relevant) else UNKNOWN
    unknowns = sorted({reason for s in steps if s["effect"] == "possible" for reason in s["reasons"]})
    if any(p.quick is None for p, _ in possible):
        unknowns = sorted({*unknowns, "QUICK_UNKNOWN"})
    notes: list[str] = []
    if decisive is None:
        notes.append("PF_DEFAULT_PASS")
    else:
        notes += decisive.extra_notes
        if decisive.gateway and action == "pass":
            notes.append("POLICY_ROUTE")
        if decisive.statetype.lower() == "none" or "no state" in getattr(decisive.pf_line, "text", ""):
            notes.append("STATELESS")
        if decisive.sched:
            notes.append("SCHEDULE_NOW")
    if any(s["effect"] == "not_loaded" and s["rule"]["loaded"] is False for s in steps):
        notes.append("RULES_NOT_LOADED")
    return LegResult(direction=direction, iface=iface, iface_source="given", action=action, decisive=decisive,
                     confidence=conf, rule_confidence=rule_conf, notes=notes, unknowns=unknowns, steps=steps)


def guess_iface(ip: IPAddr, ctx: Context) -> str | None:
    """The interface whose connected network holds ``ip`` (longest prefix), else the one with
    the default gateway."""
    best, best_len = None, -1
    for info in ctx.interfaces.values():
        if info.identifier == "lo0":
            continue
        for net in info.nets:
            if net.version == ip.version and ip in net and net.prefixlen > best_len and not net.is_link_local:
                best, best_len = info.identifier, net.prefixlen
    if best:
        return best
    gateways = [i.identifier for i in ctx.interfaces.values() if i.has_gateway]
    return gateways[0] if len(gateways) == 1 else None


def _nat_nearby(q: Query, iface: str, ctx: Context) -> bool:
    for row in ctx.port_forwards or []:
        if str(row.get("disabled") or "0") == "1" or str(row.get("enabled", "1")) == "0":
            continue
        if str(row.get("nordr") or "0") == "1":  # "no rdr" exclusions (anti-lockout) redirect nothing
            continue
        if q.dport is not None and resolve.port_match(str(row.get("destination.port") or ""), q.dport, ctx).value is False:
            continue
        ifaces = {i.strip().lower() for i in str(row.get("interface") or "").split(",") if i.strip()}
        if ifaces and iface not in ifaces:
            continue
        proto = str(row.get("protocol") or "").lower()
        if proto and proto not in ("any", "tcp/udp") and norm_proto(proto) != q.proto:
            continue
        return True
    return False


def evaluate(ctx: Context, q: Query) -> dict:
    rules, order_notes = normalize(ctx)
    context_notes = [*ctx.notes, *order_notes]
    legs: list[LegResult] = []
    iface, iface_source = q.iface, "given"
    if iface is None:
        iface, iface_source = guess_iface(q.src if q.direction == "in" else q.dst, ctx), "guessed"
    if iface is None:
        return _result(q, legs, context_notes + ["IFACE_UNRESOLVED"], unresolved=True)
    first = eval_leg(rules, q, ctx, q.direction, iface)
    first.iface_source = iface_source
    if iface_source == "guessed":
        first.notes.append("IFACE_GUESSED")
    if q.direction == "in" and (ctx.port_forwards is None or _nat_nearby(q, iface, ctx)):
        first.notes.append("NAT_NEARBY" if ctx.port_forwards is not None else "NAT_UNAVAILABLE")
    legs.append(first)
    if q.direction == "in" and q.egress != "none":
        egress, source = q.egress, "given"
        if egress is None:
            egress, source = guess_iface(q.dst, ctx), "guessed"
        if egress is None:
            context_notes.append("EGRESS_UNRESOLVED")
        else:
            out = eval_leg(rules, q, ctx, "out", egress, first_leg_iface=iface)
            out.iface_source = source
            if source == "guessed":
                out.notes.append("EGRESS_GUESSED")
            legs.append(out)
    return _result(q, legs, context_notes)


def _leg_conf(leg: LegResult, context_notes: list[str]) -> tuple[str, str]:
    downgrade = any(n in DOWNGRADING_NOTES for n in (*leg.notes, *context_notes))
    conf, rule_conf = leg.confidence, leg.rule_confidence
    if downgrade:
        conf = _min_conf(conf, PROBABLE)
        rule_conf = _min_conf(rule_conf, PROBABLE)
    return conf, rule_conf


def _result(q: Query, legs: list[LegResult], context_notes: list[str], unresolved: bool = False) -> dict:
    leg_dicts, confs = [], []
    for leg in legs:
        conf, rule_conf = _leg_conf(leg, context_notes)
        confs.append((leg, conf, rule_conf))
        leg_dicts.append({
            "direction": leg.direction, "interface": leg.iface, "interface_source": leg.iface_source,
            "action": leg.action, "confidence": conf, "rule_confidence": rule_conf,
            "decisive_rule": leg.decisive.summary() if leg.decisive else None,
            "notes": sorted(set(leg.notes)), "unknowns": leg.unknowns,
        })
    if unresolved or not legs:
        action, conf, rule_conf, decisive = None, UNKNOWN, UNKNOWN, None
    else:
        blocking = [c for c in confs if c[0].action != "pass"]
        if blocking:
            leg, _, rule_conf = blocking[0]
            action, decisive = leg.action, leg.decisive
            # Blocked if any blocking leg is right: the best of their confidences counts.
            conf = _max_conf(*(c[1] for c in blocking))
            if conf == SURE and any(c[0].action != action for c in blocking):
                conf = PROBABLE
        else:
            leg, _, rule_conf = confs[0]
            action, decisive = "pass", leg.decisive
            conf = _min_conf(*(c[1] for c in confs))
    return {
        "query": {
            "src": str(q.src), "dst": str(q.dst), "proto": q.proto_label, "sport": q.sport, "dport": q.dport,
            "icmptype": q.icmptype, "direction": q.direction,
            "assumptions": ["FIRST_PACKET"] + (["SRC_PORT_EPHEMERAL"] if q.sport is None and q.proto in _PORT_PROTOS else []),
        },
        "action": action, "confidence": conf, "rule_confidence": rule_conf,
        "decisive_rule": decisive.summary() if decisive else None,
        "notes": sorted({*context_notes, *(n for leg in legs for n in leg.notes)}),
        "unknowns": sorted({u for leg in legs for u in leg.unknowns}),
        "legs": leg_dicts,
        "trace": [{"leg": i, "direction": leg.direction, "interface": leg.iface, "steps": leg.steps}
                  for i, leg in enumerate(legs)],
    }


def parse_ip(value: str) -> IPAddr:
    return ipaddress.ip_address(value.strip())
