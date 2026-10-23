"""Network probes run FROM a firewall: ping, traceroute, TCP port probe, DNS lookup.

Admin-only by construction (every call is a POST, which the read-only role is
refused for) and audited with the target, because a firewall that probes
arbitrary addresses on request is also a scanning tool. Inputs are checked
here before they are sent (the firewall validates them again); results are
shown and never stored. Ping runs as a short job that is always stopped and
removed again, even when polling fails.
"""
import asyncio
import ipaddress
import re
import time

from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.services import audit_service, opnsense_client
from app.services.opnsense_client import OpnsenseAPIError, OpnsenseDiagnosticRejected

TARGET = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.:_-]{0,251}[A-Za-z0-9])?$")
# At most a few probes at a time per OPNexus process, so the page cannot be used to hammer a firewall.
_SLOTS = asyncio.Semaphore(4)


def clean_target(value: str, field: str = "target") -> str:
    value = (value or "").strip()
    if not TARGET.match(value):
        raise api_error(422, ErrorCode.NETWORK_FIELD_INVALID, field=field)
    return value


def clean_source(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise api_error(422, ErrorCode.NETWORK_FIELD_INVALID, field="source") from None


async def _audit(host: OpnsenseHost, tool: str, target: str, ok: bool, details: dict, message: str | None = None) -> None:
    await audit_service.persist(kind="diagnostic", action=f"diag.{tool}", status="ok" if ok else "failed",
                                entity_name=target, host_id=str(host.id), host_name=host.name, message=message, details=details)


def _rejected(exc: OpnsenseDiagnosticRejected):
    field = next(iter(exc.validations), "target").split(".")[-1]
    return api_error(422, ErrorCode.NETWORK_FIELD_INVALID, field=field)


async def _guarded(host: OpnsenseHost, tool: str, target: str, params: dict, call):
    async with _SLOTS:
        try:
            result = await call()
        except OpnsenseDiagnosticRejected as exc:
            await _audit(host, tool, target, False, params, "rejected: " + ", ".join(exc.validations))
            raise _rejected(exc) from exc
        except OpnsenseAPIError as exc:
            await _audit(host, tool, target, False, params, str(exc)[:300])
            raise api_error(502, ErrorCode.HOST_REQUEST_FAILED, host=host.name) from exc
    await _audit(host, tool, target, True, params)
    return result


def _with_silent_hops(hops: list[dict]) -> list[dict]:
    """The firewall leaves out hops that never answered (TTL jumps 1 -> 3, T5 of the 1.31.0 test run). Put them back as
    explicit rows so a gap reads as "no answer here", not as a missing hop. A route where nothing answered at all stays
    an empty list; the UI says so."""
    seen = {int(h["ttl"]): h for h in hops if h["ttl"].isdigit()}
    if not seen:
        return hops
    silent = {"address": "", "host": "", "as": "", "probes": "", "silent": True}
    return [seen.get(ttl) or {**silent, "ttl": str(ttl)} for ttl in range(1, max(seen) + 1)]


async def traceroute(host: OpnsenseHost, target: str, ipproto: str, protocol: str, source: str | None) -> dict:
    target, source = clean_target(target), clean_source(source)
    params = {"target": target, "ipproto": ipproto, "protocol": protocol, "source": source}

    async def call():
        data = await opnsense_client.diagnostic_set(
            host, "/diagnostics/traceroute/set", "traceroute",
            {"hostname": target, "ipproto": ipproto, "protocol": protocol, "source_address": source})
        resp = data.get("response") or {}
        hops = [{"ttl": str(r.get("ttl", "")), "address": str(r.get("address", "")), "host": str(r.get("host", "")),
                 "as": str(r.get("AS", "")), "probes": str(r.get("probes", "")), "silent": False}
                for r in resp.get("rows", []) if isinstance(r, dict)]
        return {"target": target, "notice": str(resp.get("notice", "")), "hops": _with_silent_hops(hops)}
    return await _guarded(host, "traceroute", target, params, call)


async def portprobe(host: OpnsenseHost, target: str, port: int, ipproto: str, source: str | None) -> dict:
    target, source = clean_target(target), clean_source(source)
    params = {"target": target, "port": port, "ipproto": ipproto, "source": source}

    async def call():
        data = await opnsense_client.diagnostic_set(
            host, "/diagnostics/portprobe/set", "portprobe",
            {"hostname": target, "port": str(port), "ipproto": ipproto, "source_address": source, "source_port": "", "showtext": "0"})
        message = str((data.get("response") or {}).get("message", ""))
        # Never forward the remote payload (a service banner): only whether the connection worked.
        return {"target": target, "port": port, "open": "succeeded" in message.lower(), "message": message}
    return await _guarded(host, "portprobe", target, params, call)


async def dns_lookup(host: OpnsenseHost, name: str, server: str | None) -> dict:
    name = clean_target(name, "name")
    server = clean_source(server)
    params = {"name": name, "server": server}

    async def call():
        data = await opnsense_client.diagnostic_set(host, "/diagnostics/dns_diagnostics/set", "dns", {"hostname": name, "server": server})
        out = {}
        for rtype, rec in (data.get("response") or {}).items():
            if isinstance(rec, dict):
                out[str(rtype)] = {"answers": [str(a) for a in rec.get("answers", [])], "query_time": str(rec.get("query_time", "")),
                                   "server": str(rec.get("server", ""))}
        return {"name": name, "records": out}
    return await _guarded(host, "dns", name, params, call)


async def ping(host: OpnsenseHost, target: str, ipproto: str, count: int, packetsize: int | None, source: str | None) -> dict:
    target, source = clean_target(target), clean_source(source)
    params = {"target": target, "ipproto": ipproto, "count": count, "packetsize": packetsize, "source": source}

    async def call():
        job = await opnsense_client.ping_job_start(host, {
            "hostname": target, "fam": "ip6" if ipproto == "inet6" else "ip", "source_address": source,
            "packetsize": str(packetsize or ""), "disable_frag": "0", "interval": "1", "description": "OPNexus"})
        try:
            state = None
            deadline = time.monotonic() + count + 6
            while time.monotonic() < deadline:
                await asyncio.sleep(1)
                state = await opnsense_client.ping_job_state(host, job)
                if state and int(state.get("send") or 0) >= count:
                    break
            await opnsense_client.ping_job_stop(host, job)
            await asyncio.sleep(0.5)
            state = await opnsense_client.ping_job_state(host, job) or state
        finally:
            # Never leave a job running on the firewall.
            try:
                await opnsense_client.ping_job_stop(host, job)
                await opnsense_client.ping_job_remove(host, job)
            except OpnsenseAPIError:
                pass
        if not state:
            raise OpnsenseAPIError("Ping: kein Ergebnis von der Firewall")
        return {"target": target, "sent": int(state.get("send") or 0), "received": int(state.get("received") or 0),
                "loss": str(state.get("loss", "")), "min": state.get("min"), "avg": state.get("avg"), "max": state.get("max"),
                "stddev": state.get("std-dev"), "error": state.get("last_error")}
    return await _guarded(host, "ping", target, params, call)
