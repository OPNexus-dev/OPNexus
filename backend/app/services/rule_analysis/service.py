"""Entry point used by the API: validate a query, load the firewall's context, evaluate."""
from __future__ import annotations

import ipaddress
import logging

from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.services.rule_analysis import context as ctx_mod
from app.services.rule_analysis.engine import Query, evaluate
from app.services.rule_analysis.normalize import PROTO_NUMBERS, norm_proto

logger = logging.getLogger(__name__)


def _ip(value: str | None, field: str):
    try:
        return ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        raise api_error(422, ErrorCode.ANALYSE_ADRESSE_UNGUELTIG, field=field, value=str(value or "")[:64]) from None


def _port(value: str | int | None, field: str) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text.isdigit() or int(text) > 65535:
        raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field=field, value=text[:32])
    return int(text)


def build_query(*, src: str, dst: str, proto: str = "tcp", sport=None, dport=None, icmptype: str | None = None,
                iface: str | None = None, direction: str = "in", egress: str | None = None) -> Query:
    source, destination = _ip(src, "src"), _ip(dst, "dst")
    if source.version != destination.version:
        raise api_error(422, ErrorCode.ANALYSE_FAMILIEN_MISCHUNG)
    label = str(proto or "tcp").strip().lower()
    if label == "icmp" and source.version == 6:
        label = "ipv6-icmp"
    number = norm_proto(label)
    if not (number.isdigit() and int(number) <= 255) and label not in PROTO_NUMBERS:
        raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="proto", value=label[:32])
    q = Query(src=source, dst=destination, proto=number, proto_label=label,
              sport=_port(sport, "sport"), dport=_port(dport, "dport"))
    if (q.sport is not None or q.dport is not None) and number not in ("6", "17"):
        raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="dport", value=str(dport or sport))
    if icmptype not in (None, ""):
        if number not in ("1", "58"):
            raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="icmptype", value=str(icmptype)[:32])
        q.icmptype = str(icmptype).strip().lower()
    if direction not in ("in", "out"):
        raise api_error(422, ErrorCode.ANALYSE_PARAMETER_UNGUELTIG, field="direction", value=str(direction)[:8])
    q.direction = direction
    q.iface = None if str(iface or "").strip().lower() in ("", "auto") else str(iface).strip()
    egress_value = str(egress or "").strip().lower()
    q.egress = "none" if egress_value in ("", "none") else (None if egress_value == "auto" else str(egress).strip())
    return q


def check_interfaces(q: Query, ctx: ctx_mod.Context) -> None:
    for name in (q.iface, None if q.egress in (None, "none") else q.egress):
        if name is not None and name not in ctx.interfaces:
            raise api_error(422, ErrorCode.ANALYSE_INTERFACE_UNBEKANNT, interface=name[:64],
                            known=", ".join(sorted(i for i in ctx.interfaces if i != "lo0")))


async def analyze_host(host: OpnsenseHost, q: Query, *, refresh: bool = False) -> dict:
    """Raises api_error for a bad interface; OpnsenseAPIError/httpx errors when the firewall
    cannot be read (callers map that)."""
    ctx = await ctx_mod.load_context(host, use_cache=not refresh)
    check_interfaces(q, ctx)
    await ctx_mod.ensure_refs(host, ctx, [str(q.src), str(q.dst)])
    result = evaluate(ctx, q)
    if any(ctx.refs.get(ip) is None for ip in (str(q.src), str(q.dst))):
        result["notes"] = sorted({*result["notes"], "ALIAS_REFERENCES_UNAVAILABLE"})
    return result
