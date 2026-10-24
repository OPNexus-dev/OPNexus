"""Periodic host resource sampling (CPU load, memory, disk, interface
counters) — polled by `_resource_poll_loop` in main.py, same "one host's
failure must not block the others" principle as log_ingest_service.py's
poll_all_hosts. v1 deliberately has no admin-editable poll interval (see
config.py) — just a fixed constant, to keep this feature's first cut
small.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.resource_sample import HostResourceSample
from app.services import gateway_health_service
from app.services import opnsense_client as client
from app.services.opnsense_client import OpnsenseAPIError, OpnsenseForbiddenError

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mem_used_pct(resources: dict) -> float | None:
    memory = resources.get("memory") or {}
    try:
        total = int(memory["total"])
        used = int(memory["used"])
    except (KeyError, TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return round(used / total * 100, 1)


def _load_avg_1m(system_time: dict) -> float | None:
    raw = system_time.get("loadavg")
    if not raw:
        return None
    try:
        return float(str(raw).split(",")[0].strip())
    except ValueError:
        return None


def _disk_used_pct(disk: dict) -> float | None:
    for device in disk.get("devices", []):
        if device.get("mountpoint") == "/":
            try:
                return float(device["used_pct"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _swap_used_pct(swap) -> float | None:
    """systemSwap: {"swap": [{"device", "total", "used"}]} in KiB; None when there is no swap at all."""
    try:
        rows = swap.get("swap") or []
        total = sum(int(r["total"]) for r in rows)
        used = sum(int(r["used"]) for r in rows)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return round(used / total * 100, 1) if total > 0 else None


_CPU_THREADS = re.compile(r"(\d+)\s+threads?", re.IGNORECASE)
_CPU_CORES = re.compile(r"(\d+)\s+cores?", re.IGNORECASE)


def _cpu_threads(cpu_type) -> int | None:
    """getCPUType: ["<model> (6 cores, 6 threads)"]."""
    text = " ".join(str(x) for x in cpu_type) if isinstance(cpu_type, list) else str(cpu_type or "")
    match = _CPU_THREADS.search(text) or _CPU_CORES.search(text)
    return int(match.group(1)) if match else None


def _services_compact(listing) -> dict[str, int]:
    """{"unbound": 1, "openvpn/<uuid>": 0}: 1 = running. Only services an admin can stop (not 'locked')."""
    result: dict[str, int] = {}
    for row in (listing.get("rows") if isinstance(listing, dict) else None) or []:
        if not isinstance(row, dict) or not row.get("name") or str(row.get("locked")) in ("1", "True", "true"):
            continue
        name, id_ = str(row["name"]), str(row.get("id") or row["name"])
        result[name if id_ == name else f"{name}/{id_}"] = 1 if str(row.get("running")) in ("1", "True", "true") else 0
    return result


async def _optional(operation, name: str, denied: list[str], ok: list[str]):
    """The extra readings must never cost the base sample (older firmware, an endpoint that is missing).
    A refusal by the firewall (403: the API key lacks the privilege) is recorded in ``denied`` so the
    missing right becomes visible; a reading that went through is recorded in ``ok`` (the proof that a right is
    back, whatever the answer contains); any other failure stays "unknown" and is no finding about rights."""
    try:
        result = await operation
        ok.append(name)
        return result
    except OpnsenseForbiddenError:
        denied.append(name)
        logger.debug("Optionale Messung %s: API-Recht fehlt", name)
        return None
    except Exception as exc:
        logger.debug("Optionale Messung fehlgeschlagen: %s", type(exc).__name__)
        return None


async def sample_host(db: AsyncSession, host: OpnsenseHost) -> None:
    denied: list[str] = []
    ok: list[str] = []
    try:
        resources = await client.get_system_resources(host)
        system_time = await client.get_system_time(host)
        disk = await client.get_system_disk(host)
        interfaces = await client.get_interface_statistics(host)
        ok.append("base")
    except OpnsenseForbiddenError:
        # Dem API-Schlüssel fehlen schon die Basis-Rechte (Dashboard): ohne Messwerte gäbe es nie einen Messpunkt und
        # der Rechte-Alarm könnte nicht auslösen. Ein leerer Punkt trägt den Befund ("base" in denied); die
        # Schwellenwert-Alarme sehen darin "unbekannt", nicht "in Ordnung".
        db.add(HostResourceSample(host_id=host.id, sampled_at=_now(), mem_used_pct=None, load_avg_1m=None,
                                  disk_used_pct=None, raw={"denied": ["base"], "ok": []}))
        logger.warning("Resource-Sampling für Host %s: dem API-Schlüssel fehlen die Basis-Rechte (403)", host.name)
        return
    swap, cpu_type, gateways, services = await asyncio.gather(
        _optional(client.get_system_swap(host), "swap", denied, ok),
        _optional(client.get_cpu_type(host), "cpu", denied, ok),
        _optional(gateway_health_service.get_gateway_health(host), "gateways", denied, ok),
        _optional(client.search_services(host), "services", denied, ok),
    )
    db.add(
        HostResourceSample(
            host_id=host.id,
            sampled_at=_now(),
            mem_used_pct=_mem_used_pct(resources),
            load_avg_1m=_load_avg_1m(system_time),
            disk_used_pct=_disk_used_pct(disk),
            raw={
                "memory": resources.get("memory"), "disk": disk.get("devices"), "interfaces": interfaces.get("statistics"),
                # None = not readable this round (the alert checks treat that as "unknown", not as "fine").
                "swap_used_pct": _swap_used_pct(swap) if swap is not None else None,
                "cpu_threads": _cpu_threads(cpu_type) if cpu_type is not None else None,
                "gateways": gateway_health_service.compact(gateways) if gateways is not None else None,
                "services": _services_compact(services) if services is not None else None,
                # Readings the firewall refused (403) because the API key lacks the right; always a list.
                "denied": sorted(denied),
                "ok": sorted(ok),
            },
        )
    )


async def poll_all_hosts(db: AsyncSession) -> dict:
    hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)))).all())
    sampled = 0
    failed = 0
    for host in hosts:
        try:
            await sample_host(db, host)
            sampled += 1
        except OpnsenseAPIError:
            failed += 1
            logger.warning("Resource-Sampling für Host %s fehlgeschlagen (nicht erreichbar?)", host.name)
        except Exception:
            failed += 1
            logger.exception("Resource-Sampling für Host %s fehlgeschlagen", host.name)
    await db.commit()
    return {"hosts_polled": len(hosts), "hosts_failed": failed, "sampled": sampled}


async def prune_old_samples(db: AsyncSession, retention_days: int) -> int:
    cutoff = _now() - timedelta(days=retention_days)
    result = await db.execute(delete(HostResourceSample).where(HostResourceSample.sampled_at < cutoff))
    await db.commit()
    return result.rowcount or 0
