"""Health gate of a firmware rollout (1.25): is a host, after its update, as healthy as before?

``baseline(db, host)`` is taken right before a wave starts, ``evaluate(...)`` after the soak time
(and before each node of an already touched cluster, and on resume). Every check of every host
yields ``{"result": ok|warn|fail|skip, "code": <machine code>, "params": {...}}``; the UI translates
the code (rollouts.gate.code.*), so no German prose travels through here.

What the checks look at (all read-only on the firewall, except that the compliance check downloads
the configuration like the 6-hourly security check does):

* online     — enabled, the API answers, firmware job idle (``running == ready``).
* version    — the installed version is the rollout's target version.
* carp       — per cluster: every node online, exactly one MASTER per VHID, no node in CARP
               maintenance mode, no CARP auto-revert timer running.
* gateways   — gateways that were up in the baseline are up again; a gateway that was already bad
               before only warns (1.24 ``get_gateway_health``).
* services   — services that ran in the baseline run again.
* locks      — no open maintenance operation, restore or unconfirmed deployment in the host/cluster.
* compliance — security score not lower than before: warn (default) or fail. A weak signal: an
               update may legitimately change defaults.

A reading that cannot be taken is never "ok": unreadable now but readable in the baseline fails,
unreadable in both is "skip" (nothing to compare; e.g. an API key without that permission).
"""
import asyncio
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.carp_maintenance_window import CarpMaintenanceWindow
from app.models.compliance_result import ComplianceResult
from app.models.host import OpnsenseHost
from app.services import compliance_service, gateway_health_service, opnsense_client as client
from app.services.host_maintenance_service import _shared_change_blockers, peers
from app.services.host_status_service import get_statuses
from app.services.resource_monitor_service import _services_compact

CHECKS = ("online", "version", "carp", "gateways", "services", "locks", "compliance")
DEFAULT_CHECKS = {"online": True, "version": True, "carp": True, "gateways": True, "services": True, "locks": True,
                  "compliance": False}
ALWAYS_ON = ("online", "version", "locks")  # a rollout without these would be blind
_RANK = {"skip": 0, "ok": 0, "warn": 1, "fail": 2}


def _r(result: str, code: str, **params) -> dict:
    return {"result": result, "code": code, "params": params}


async def _optional(coro):
    try:
        return await coro
    except client.OpnsenseAPIError:
        return None


async def _gateways(host) -> dict | None:
    gws = await _optional(gateway_health_service.get_gateway_health(host))
    return None if gws is None else {g.name: g.state for g in gws}


async def _services(host) -> dict | None:
    listing = await _optional(client.search_services(host))
    return None if listing is None else _services_compact(listing)


async def baseline(db: AsyncSession, host: OpnsenseHost) -> dict:
    info, gateways, services = await asyncio.gather(
        _optional(client.get_firmware_info(host)), _gateways(host), _services(host))
    score = await db.scalar(select(ComplianceResult.score).where(ComplianceResult.host_id == host.id))
    return {
        "version": (info or {}).get("product_version"),
        "gateways": gateways,
        "services": services,
        "compliance_score": score,
    }


def _check_gateways(before: dict | None, now: dict | None) -> dict:
    if now is None:
        return _r("skip", "unreadable_both") if before is None else _r("fail", "unreadable")
    if before is None:
        bad = sorted(n for n, s in now.items() if s == "offline")
        return _r("warn", "gateway_down", names=", ".join(bad)) if bad else _r("ok", "no_baseline")
    down = sorted(n for n, s in before.items() if s in ("online", "unknown") and now.get(n) == "offline")
    gone = sorted(n for n in before if n not in now)
    degraded = sorted(n for n, s in now.items() if s == "degraded" and before.get(n) != "degraded")
    still_bad = sorted(n for n, s in before.items() if s in ("offline", "degraded") and now.get(n) in ("offline", "degraded"))
    # "unknown" (not monitored) going to "offline" is a real change too: monitoring just started and reports down.
    if down:
        return _r("fail", "gateway_down", names=", ".join(down))
    if gone:
        return _r("warn", "gateway_missing", names=", ".join(gone))
    if degraded:
        return _r("warn", "gateway_degraded", names=", ".join(degraded))
    if still_bad:
        return _r("warn", "gateway_bad_before", names=", ".join(still_bad))
    return _r("ok", "ok")


def _check_services(before: dict | None, now: dict | None) -> dict:
    if now is None:
        return _r("skip", "unreadable_both") if before is None else _r("fail", "unreadable")
    if before is None:
        return _r("ok", "no_baseline")
    stopped = sorted(name.split("/")[0] for name, running in before.items() if running == 1 and now.get(name) != 1)
    if stopped:
        return _r("fail", "service_stopped", names=", ".join(dict.fromkeys(stopped)))
    return _r("ok", "ok")


async def _check_carp(db: AsyncSession, fleet: list[OpnsenseHost], statuses: dict) -> dict:
    offline = sorted(h.name for h in fleet if h.enabled and statuses.get(h.id, {}).get("status") != "online")
    if offline:
        return _r("fail", "node_offline", names=", ".join(offline))
    online = [h for h in fleet if statuses.get(h.id, {}).get("status") == "online"]
    demoted = sorted(h.name for h in online if statuses[h.id].get("carp_maintenance_mode"))
    if demoted:
        return _r("fail", "maintenance_mode", names=", ".join(demoted))
    timers = (await db.scalars(select(CarpMaintenanceWindow.host_name).where(
        CarpMaintenanceWindow.host_id.in_([h.id for h in fleet]), CarpMaintenanceWindow.status == "active"))).all()
    if timers:
        return _r("fail", "auto_revert_active", names=", ".join(sorted(timers)))
    masters: dict[str, int] = defaultdict(int)
    broken: set[str] = set()
    for h in online:
        for vhid, state in (statuses[h.id].get("carp_vips") or {}).items():
            masters[vhid] += state == "MASTER"
            if state not in ("MASTER", "BACKUP"):
                broken.add(vhid)
    wrong = sorted(set(v for v, n in masters.items() if n != 1) | broken, key=lambda v: (len(v), v))
    if wrong:
        return _r("fail", "vhid_roles", vhids=", ".join(wrong))
    if not masters and not any(statuses[h.id].get("carp_status") == "MASTER" for h in online):
        return _r("fail", "no_master")
    return _r("ok", "ok")


async def evaluate(db: AsyncSession, hosts: list[OpnsenseHost], baselines: dict, *, target_version: str | None,
                   checks: dict | None = None, compliance_mode: str = "warn") -> dict:
    """{"hosts": {host_id: {check: result}}, "verdict": ok|warn|fail}. ``baselines`` is keyed by str(host.id)."""
    enabled = {**DEFAULT_CHECKS, **(checks or {})}
    for name in ALWAYS_ON:
        enabled[name] = True
    results: dict[str, dict] = {}
    fleets: dict[str, list[OpnsenseHost]] = {}
    for host in hosts:
        fleets[str(host.id)] = await peers(db, host) if host.cluster_name else [host]
    every = {h.id: h for fleet in fleets.values() for h in fleet}
    statuses = await get_statuses(list(every.values()))
    carp_cache: dict[str, dict] = {}
    for host in hosts:
        hid = str(host.id)
        before = baselines.get(hid) or {}
        row: dict[str, dict] = {}
        st = statuses.get(host.id, {})
        running = await _optional(client.get_firmware_running(host)) if st.get("status") == "online" else None
        if not host.enabled:
            row["online"] = _r("fail", "disabled")
        elif st.get("status") != "online":
            row["online"] = _r("fail", "offline")
        elif (running or {}).get("status") != "ready":
            row["online"] = _r("fail", "firmware_busy")
        else:
            row["online"] = _r("ok", "ok")
        reachable = row["online"]["result"] == "ok"

        if enabled["version"]:
            info = await _optional(client.get_firmware_info(host)) if reachable else None
            version = (info or {}).get("product_version")
            if not target_version:
                row["version"] = _r("skip", "no_target")
            elif version is None:
                row["version"] = _r("fail", "unreadable")
            elif version != target_version:
                row["version"] = _r("fail", "version_mismatch", expected=target_version, actual=version)
            else:
                row["version"] = _r("ok", "ok", actual=version)

        if enabled["carp"]:
            if not host.cluster_name:
                row["carp"] = _r("skip", "no_cluster")
            else:
                if host.cluster_name not in carp_cache:
                    carp_cache[host.cluster_name] = await _check_carp(db, fleets[hid], statuses)
                row["carp"] = carp_cache[host.cluster_name]

        if enabled["gateways"]:
            row["gateways"] = _check_gateways(before.get("gateways"), await _gateways(host) if reachable else None)
        if enabled["services"]:
            row["services"] = _check_services(before.get("services"), await _services(host) if reachable else None)

        blockers = await _shared_change_blockers(db, fleets[hid])
        row["locks"] = _r("fail", "open_locks", count=len(blockers)) if blockers else _r("ok", "ok")

        if enabled["compliance"]:
            base_score = before.get("compliance_score")
            if base_score is None:
                row["compliance"] = _r("skip", "no_baseline")
            elif not reachable:
                row["compliance"] = _r("fail", "unreadable")
            else:
                fresh = await compliance_service.check_host(db, host)
                if fresh.score is None:
                    row["compliance"] = _r("warn", "unreadable")
                elif fresh.score < base_score:
                    row["compliance"] = _r("fail" if compliance_mode == "fail" else "warn", "score_lower",
                                           before=base_score, after=fresh.score)
                else:
                    row["compliance"] = _r("ok", "ok", before=base_score, after=fresh.score)
        results[hid] = row
    worst = max((_RANK[c["result"]] for row in results.values() for c in row.values()), default=0)
    return {"hosts": results, "verdict": ("ok", "warn", "fail")[worst]}


def failed_checks(gate: dict) -> list[str]:
    """Flat "host-id:check" list of everything that failed (for notifications and the audit trail)."""
    return [f"{hid}:{name}" for hid, row in (gate.get("hosts") or {}).items() for name, c in row.items() if c["result"] == "fail"]
