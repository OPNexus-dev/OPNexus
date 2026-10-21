"""The ruleset pf has actually loaded (``diagnostics/firewall/pfStatistics/rules``).

Each key looks like ``@76 pass in quick on vtnet1_vlan10 inet from (...) to any flags S/SA
keep state label "<uuid>"``. One configured rule can expand to several pf lines (address
families, ICMP types, port lists); they share one label. Lines without a label are generated
by OPNsense itself (antispoof: ``block drop in log on ! vtnet1_vlan10 inet from 192.0.2.0/24
to any``). Only what the analysis needs is parsed: position, action, quick, direction, the
``on`` interface, address family, ICMP types and a few pf options that make the answer
uncertain.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEAD = re.compile(r"^@(\d+)\s+(.*)$")
_LABEL = re.compile(r'label "([^"]+)"')
_ON = re.compile(r"\bon (!\s*)?([A-Za-z0-9_.]+)")
_ICMP = re.compile(r"\bicmp6?-type (\S+)")
_FROM = re.compile(r"\bfrom (\S+) to (\S+)")
_FLAGS = re.compile(r"\bflags (\S+)")


@dataclass
class PfLine:
    number: int
    text: str
    action: str           # pass | block | reject | match | scrub | other
    quick: bool
    direction: str        # in | out | any
    on_negated: bool
    on_device: str | None
    af: int | None        # 4, 6 or None (both)
    label: str | None
    icmptypes: list[str] = field(default_factory=list)
    src: str = "any"
    dst: str = "any"
    flags: str | None = None


def parse_line(number: int, text: str) -> PfLine:
    words = text.split()
    first = words[0] if words else ""
    action = first if first in ("pass", "match", "scrub") else "other"
    if first == "block":
        action = "reject" if len(words) > 1 and words[1].startswith("return") else "block"
    direction = "in" if " in " in f" {text} " else ("out" if " out " in f" {text} " else "any")
    on = _ON.search(text)
    af = 4 if re.search(r"\binet\b", text) else (6 if re.search(r"\binet6\b", text) else None)
    label = _LABEL.search(text)
    endpoints = _FROM.search(text)
    flags = _FLAGS.search(text)
    return PfLine(
        number=number, text=text, action=action, quick=" quick " in f" {text} ",
        direction=direction, on_negated=bool(on and on.group(1)), on_device=on.group(2) if on else None,
        af=af, label=label.group(1) if label else None, icmptypes=_ICMP.findall(text),
        src=endpoints.group(1) if endpoints else ("any" if " all" in f" {text}" else "?"),
        dst=endpoints.group(2) if endpoints else ("any" if " all" in f" {text}" else "?"),
        flags=flags.group(1) if flags else None,
    )


@dataclass
class PfRuleset:
    lines: list[PfLine]
    by_label: dict[str, list[PfLine]]

    @property
    def unlabeled(self) -> list[PfLine]:
        return [line for line in self.lines if line.label is None and line.action in ("pass", "block", "reject")]


def parse(stats: dict | None) -> PfRuleset | None:
    """None when the answer does not look like a pf ruleset at all."""
    rules = (stats or {}).get("rules") if isinstance(stats, dict) else None
    filter_rules = rules.get("filter rules") if isinstance(rules, dict) else None
    if not isinstance(filter_rules, dict):
        return None
    lines: list[PfLine] = []
    for key in filter_rules:
        head = _HEAD.match(str(key).strip())
        if not head:
            continue
        line = parse_line(int(head.group(1)), head.group(2))
        if line.action == "scrub":
            continue
        lines.append(line)
    lines.sort(key=lambda line: line.number)
    by_label: dict[str, list[PfLine]] = {}
    for line in lines:
        if line.label:
            by_label.setdefault(line.label, []).append(line)
    return PfRuleset(lines=lines, by_label=by_label)
