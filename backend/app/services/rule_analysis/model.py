"""Shared types of the rule analysis: three-valued logic and the normalized rule.

``Tri`` is Kleene logic with ``True``/``False``/``None`` (unknown). An unknown never turns
into a definite answer by negation, and an AND is only False when one side is definitely
False. This is what keeps the engine from claiming "sure" for something it cannot prove.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Optional

Tri = Optional[bool]

IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNet = ipaddress.IPv4Network | ipaddress.IPv6Network


def t_and(*values: Tri) -> Tri:
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    return True


def t_or(*values: Tri) -> Tri:
    if any(v is True for v in values):
        return True
    if any(v is None for v in values):
        return None
    return False


def t_not(value: Tri) -> Tri:
    return None if value is None else not value


def tri_str(value: Tri) -> str:
    return "unknown" if value is None else ("true" if value else "false")


class Outcome:
    """A Tri plus the reason codes that made it unknown (or noteworthy)."""
    __slots__ = ("value", "reasons")

    def __init__(self, value: Tri, reasons: tuple[str, ...] | list[str] = ()):
        self.value = value
        self.reasons = list(reasons)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Outcome({self.value!r}, {self.reasons!r})"


def o_or(outcomes: list[Outcome]) -> Outcome:
    value = t_or(*(o.value for o in outcomes)) if outcomes else False
    reasons = [] if value is True else [r for o in outcomes if o.value is None for r in o.reasons]
    return Outcome(value, reasons)


SECTION_SYSTEM, SECTION_FLOATING, SECTION_GROUP, SECTION_INTERFACE = "system", "floating", "group", "interface"
SECTION_NAT_AUTO, SECTION_INVALID, SECTION_ANTISPOOF, SECTION_OTHER = "nat_auto", "invalid", "antispoof", "other"


@dataclass
class Endpoint:
    raw: str
    negated: bool = False
    items: list[str] = field(default_factory=list)  # empty = any

    @property
    def is_any(self) -> bool:
        return not self.items


@dataclass
class NormRule:
    uuid: str
    label: str
    description: str
    order: tuple[int, int]
    order_known: bool
    section: str
    enabled: bool
    action: str | None             # pass | block | reject | None (unknown)
    quick: bool | None             # None = unknown (automatic rule without pf ruleset)
    interfaces: set[str]           # identifiers or group names; empty = every interface
    if_not: bool
    direction: str                 # in | out | any
    af: set[int]                   # {4}, {6} or {4, 6}
    protos: set[str] | None        # None = any; normalized names/numbers as strings
    src: Endpoint
    dst: Endpoint
    sport: str
    dport: str
    icmptypes: set[str] | None = None   # None = no restriction
    tag: str = ""
    tagged: str = ""
    received_on: str = ""
    gateway: str = ""
    statetype: str = ""
    sched: str = ""
    automatic: bool = False
    legacy: bool = False
    loaded: bool | None = None     # None = pf ruleset not available
    pf_numbers: list[int] = field(default_factory=list)
    extra_notes: list[str] = field(default_factory=list)   # e.g. TCP_FLAGS, RATE_LIMIT from pf text
    pf_line: object = None         # set for unlabeled pf lines (antispoof)

    def summary(self) -> dict:
        return {
            "uuid": self.uuid, "label": self.label, "description": self.description,
            "section": self.section, "sort_order": f"{self.order[0]:06d}.{self.order[1]:07d}" if self.order_known else None,
            "pf_numbers": self.pf_numbers, "action": self.action, "quick": self.quick,
            "interfaces": sorted(self.interfaces), "interface_not": self.if_not, "direction": self.direction,
            "automatic": self.automatic, "legacy": self.legacy, "loaded": self.loaded,
            "editable": not self.automatic and not self.legacy and self.section != SECTION_ANTISPOOF,
            "source": ("!" if self.src.negated else "") + (self.src.raw or "any"),
            "destination": ("!" if self.dst.negated else "") + (self.dst.raw or "any"),
            "source_port": self.sport or None, "destination_port": self.dport or None,
            "gateway": self.gateway or None, "schedule": self.sched or None,
        }
