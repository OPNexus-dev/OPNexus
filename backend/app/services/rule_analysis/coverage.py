"""Does an earlier rule make a later one pointless? (shadowed / duplicate)

Pure functions on ``NormRule``; no I/O. Used by the security check (``compliance_service``, rules taken
from config.xml) and by the cleanup candidates (rules taken from ``searchRule``).

pf evaluates all rules in order, the last matching rule wins unless a matching rule is ``quick``. So a later
rule b can only be made dead by an earlier rule a when

* a is ``quick`` (a non-quick match is overridden by later rules and decides nothing by itself),
* a is in force (enabled, not known to be unloaded) and has no condition we cannot compare
  (schedule, tags, ``received-on``, TCP flags, rate limits), and
* every packet b matches is also matched by a: set coverage per field (interfaces, direction, address
  family, protocols, source/destination, ports, ICMP types).

The order is ``sort_order`` (floating 200000 before groups 300000+ before interfaces 400000, then the
sequence); nothing is re-implemented here, ``NormRule.order`` already holds it.

Every comparison answers ``"sure"`` (literal values or identical names), ``"probable"`` (the result relies
on the static content of aliases or on a guessed order) or ``None`` (not provable, so no claim is made).
Whatever is not proven is never "sure": the weakest link decides.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from app.services.rule_analysis.context import Context
from app.services.rule_analysis.model import (
    SECTION_ANTISPOOF, SECTION_FLOATING, SECTION_GROUP, SECTION_INTERFACE, SECTION_INVALID, SECTION_NAT_AUTO,
    Endpoint, NormRule,
)
from app.services.rule_analysis.normalize import _af, _protos
from app.services.rule_analysis.resolve import ADDRESS_TYPES, MAX_ALIAS_DEPTH, port_ranges

SURE, PROBABLE = "sure", "probable"
_RANK = {None: 0, PROBABLE: 1, SURE: 2}


def weakest(*values: str | None) -> str | None:
    """The lowest confidence of the arguments (None beats everything: one unproven part spoils the claim)."""
    return min(values, key=lambda v: _RANK[v]) if values else SURE


def _empty_context() -> Context:
    return Context(rows=[], pf=None, interfaces={}, groups=None, aliases={}, port_forwards=None)


# ── Addresses ─────────────────────────────────────────────────────────────

Range = tuple[int, int, int]  # (ip version, low, high)


def _literal(token: str) -> Range | None:
    """CIDR, single address or ``low-high`` range as an integer interval; None for names."""
    token = token.strip()
    try:
        if "-" in token and not token.startswith("-"):
            low, _, high = token.partition("-")
            lo, hi = ipaddress.ip_address(low.strip()), ipaddress.ip_address(high.strip())
            return (lo.version, int(lo), int(hi)) if lo.version == hi.version and lo <= hi else None
        net = ipaddress.ip_network(token, strict=False)
    except ValueError:
        return None
    return (net.version, int(net.network_address), int(net.broadcast_address))


def _alias_ranges(name: str, ctx: Context, depth: int = 0, seen: tuple = ()) -> list[Range] | None:
    """Static content of a host/network alias as intervals; None when anything about it is not static
    (tables, URLs, FQDNs, exclusions, unknown names, cycles)."""
    alias = ctx.aliases.get(name)
    if alias is None or name in seen or depth > MAX_ALIAS_DEPTH:
        return None
    if str(alias.get("type") or "").lower() not in ADDRESS_TYPES:
        return None
    if str(alias.get("enabled", "1")).strip().lower() in ("0", "false", "no", "off"):
        return None  # a disabled alias is an empty table in pf: its configured content proves nothing
    result: list[Range] = []
    for entry in (e.strip() for e in re.split(r"[\n,]", str(alias.get("content") or "")) if e.strip()):
        if entry.startswith("!"):
            return None
        literal = _literal(entry)
        if literal is not None:
            result.append(literal)
            continue
        nested = _alias_ranges(entry, ctx, depth + 1, (*seen, name)) if entry in ctx.aliases else None
        if nested is None:
            return None
        result += nested
    return result or None


def _ranges_of(token: str, ctx: Context) -> tuple[list[Range] | None, bool]:
    """(intervals, came_from_alias)"""
    literal = _literal(token)
    if literal is not None:
        return [literal], False
    return _alias_ranges(token, ctx), True


def _covered(inner: list[Range], outer: list[Range]) -> bool:
    return all(any(o[0] == i[0] and o[1] <= i[1] and i[2] <= o[2] for o in outer) for i in inner)


def _item_covers(x: str, y: str, ctx: Context) -> str | None:
    """Does the address item x contain everything the item y contains?"""
    x, y = x.strip(), y.strip()
    if x == y:
        return SURE
    outer, outer_alias = _ranges_of(x, ctx)
    inner, inner_alias = _ranges_of(y, ctx)
    if outer is None or inner is None or not _covered(inner, outer):
        return None
    return PROBABLE if (outer_alias or inner_alias) else SURE


def endpoint_covers(a: Endpoint, b: Endpoint, ctx: Context) -> str | None:
    if a.is_any and not a.negated:
        return SURE
    if b.is_any and not b.negated:
        return None  # a is narrower than "any"
    if a.negated != b.negated:
        return None
    if not a.negated:  # every item of b lies inside some item of a
        parts = []
        for y in b.items:
            best = None
            for x in a.items:
                got = _item_covers(x, y, ctx)
                if _RANK[got] > _RANK[best]:
                    best = got
            parts.append(best)
        return weakest(*parts)
    # "!A" covers "!B" when A lies inside B (outside of B is a subset of outside of A)
    parts = []
    for x in a.items:
        best = None
        for y in b.items:
            got = _item_covers(y, x, ctx)
            if _RANK[got] > _RANK[best]:
                best = got
        parts.append(best)
    return weakest(*parts)


# ── Ports, interfaces, the rest ───────────────────────────────────────────

def _norm_ports(spec: str) -> str:
    return re.sub(r"\s+", "", spec or "").lower()


def ports_cover(a: str, b: str, ctx: Context) -> str | None:
    if _norm_ports(a) in ("", "any"):
        return SURE
    if _norm_ports(b) in ("", "any"):
        return None
    if _norm_ports(a) == _norm_ports(b):
        return SURE
    outer, _ = port_ranges(a, ctx)
    inner, _ = port_ranges(b, ctx)
    if outer is None or inner is None:
        return None
    if not all(any(lo <= il and ih <= hi for lo, hi in outer) for il, ih in inner):
        return None
    alias_involved = any(part.strip() in ctx.aliases for spec in (a, b) for part in re.split(r"[\n,]", spec))
    return PROBABLE if alias_involved else SURE


def _expand(names: set[str], ctx: Context) -> set[str]:
    result: set[str] = set()
    for name in names:
        result |= (ctx.groups[name] | {name}) if ctx.groups and name in ctx.groups else {name}
    return result


def interfaces_cover(a: NormRule, b: NormRule, ctx: Context) -> str | None:
    if not a.interfaces and not a.if_not:
        return SURE  # every interface
    if a.if_not or b.if_not:
        return SURE if (a.interfaces, a.if_not) == (b.interfaces, b.if_not) else None
    if not b.interfaces:
        return None  # b applies everywhere, a only to some interfaces
    return SURE if _expand(b.interfaces, ctx) <= _expand(a.interfaces, ctx) else None


def _conditions_ok(a: NormRule) -> bool:
    """a must not depend on something we cannot compare: schedule, tags, received-on, TCP flags, limits."""
    return not (a.sched or a.tagged or a.received_on or a.extra_notes)


def covers_fields(a: NormRule, b: NormRule, ctx: Context | None = None) -> str | None:
    """Every packet rule b matches is already matched by rule a (order and enabled state not considered)."""
    ctx = ctx or _empty_context()
    if a.quick is not True or not _conditions_ok(a):
        return None
    if a.direction != "any" and a.direction != b.direction:
        return None
    if not b.af <= a.af:
        return None
    if a.protos is not None and (b.protos is None or not b.protos <= a.protos):
        return None
    if a.icmptypes is not None and (b.icmptypes is None or not b.icmptypes <= a.icmptypes):
        return None
    parts = [
        interfaces_cover(a, b, ctx),
        endpoint_covers(a.src, b.src, ctx), endpoint_covers(a.dst, b.dst, ctx),
        ports_cover(a.sport, b.sport, ctx), ports_cover(a.dport, b.dport, ctx),
    ]
    return weakest(*parts)


# ── Order-aware analysis ──────────────────────────────────────────────────

def _in_force(rule: NormRule) -> bool:
    return rule.enabled and rule.loaded is not False and rule.section not in (SECTION_INVALID, SECTION_NAT_AUTO)


# normalize() notes after which the evaluation order is not trustworthy: findings stay at "probable" at most.
ORDER_DOUBT = frozenset({"ORDER_GUESSED", "ORDER_MISMATCH"})


def order_doubtful(notes) -> bool:
    return bool(ORDER_DOUBT & set(notes))


@dataclass
class Overlap:
    rule: NormRule        # the later rule that is made pointless
    by: NormRule          # the earlier rule that already decides everything the later one matches
    kind: str             # "shadowed" | "duplicate"
    confidence: str       # "sure" | "probable"


def analyse(rules: list[NormRule], ctx: Context | None = None, *, order_guessed: bool = False) -> list[Overlap]:
    """``rules`` in evaluation order. Each later rule is reported at most once, with the first earlier rule that
    covers it. Duplicate = same action and the later rule also covers the earlier one."""
    ctx = ctx or _empty_context()
    result: list[Overlap] = []
    for index, later in enumerate(rules):
        if not later.enabled or later.section in (SECTION_ANTISPOOF, SECTION_INVALID, SECTION_NAT_AUTO):
            continue
        if later.action is None:
            continue
        for earlier in rules[:index]:
            if not _in_force(earlier) or earlier.action is None:
                continue
            confidence = covers_fields(earlier, later, ctx)
            if confidence is None:
                continue
            # (Equal sort_order without pf line numbers always comes with loaded=None, i.e. "probable" here.)
            if order_guessed or not (earlier.order_known and later.order_known) or earlier.loaded is None:
                confidence = weakest(confidence, PROBABLE)
            duplicate = earlier.action == later.action and covers_fields(later, earlier, ctx) is not None
            if duplicate:
                confidence = weakest(confidence, covers_fields(later, earlier, ctx))
            result.append(Overlap(later, earlier, "duplicate" if duplicate else "shadowed", confidence or PROBABLE))
            break
    return result


# ── Rules read from a config.xml (security check) ─────────────────────────

def config_order(rule: dict) -> tuple[int, int]:
    """sort_order of a rule from config.xml in OPNsense's terms: floating 200000, interface group 300000,
    single interface 400000, each by sequence."""
    return (200000 if rule["floating"] else 300000 if rule.get("group") else 400000, rule["order"])


def _config_endpoint(raw: str, negated: bool) -> Endpoint:
    raw = (raw or "").strip()
    items = [] if raw.lower() in ("", "any") else [i.strip() for i in raw.split(",") if i.strip()]
    return Endpoint(raw=raw, negated=negated, items=items)


def from_config_rule(rule: dict) -> NormRule:
    """Adapter for the rule dicts of compliance_service (MVC and legacy shapes already merged). Nothing is known
    about the loaded pf ruleset there, so ``loaded`` stays None (coverage then caps at "probable")."""
    order = config_order(rule)
    section = SECTION_FLOATING if rule["floating"] else SECTION_GROUP if rule.get("group") else SECTION_INTERFACE
    return NormRule(
        uuid="", label="", description=rule["descr"], order=order, order_known=True, section=section,
        enabled=rule["enabled"], action=rule["action"] if rule["action"] in ("pass", "block", "reject") else None,
        quick=bool(rule["quick"]), interfaces=set(rule["interfaces"]), if_not=bool(rule.get("if_not")),
        direction=rule["direction"] or "in", af=_af(rule["ipprotocol"]), protos=_protos(rule["protocol"]),
        src=_config_endpoint(rule["src"], rule["src_not"]), dst=_config_endpoint(rule["dst"], rule["dst_not"]),
        sport=rule["sport"] or "", dport=rule["dport"] or "",
        icmptypes={t.strip() for t in (rule.get("icmptype") or "").split(",") if t.strip()} or None,
        tagged=rule.get("tagged") or "", received_on=rule.get("received_on") or "", sched=rule.get("sched") or "",
        extra_notes=["RATE_LIMIT"] if rule.get("limited") else [],
    )
