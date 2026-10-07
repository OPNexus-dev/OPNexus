"""Periodic host resource sampling (CPU load, memory, disk, interface
counters) — polled by `_resource_poll_loop` in main.py, same "one host's
failure must not block the others" principle as log_ingest_service.py's
poll_all_hosts. v1 deliberately has no admin-editable poll interval (see
config.py) — just a fixed constant, to keep this feature's first cut
small.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.resource_sample import HostResourceSample
from app.services import opnsense_client as client
from app.services.opnsense_client import OpnsenseAPIError

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


async def sample_host(db: AsyncSession, host: OpnsenseHost) -> None:
    resources = await client.get_system_resources(host)
    system_time = await client.get_system_time(host)
    disk = await client.get_system_disk(host)
    interfaces = await client.get_interface_statistics(host)
    db.add(
        HostResourceSample(
            host_id=host.id,
            sampled_at=_now(),
            mem_used_pct=_mem_used_pct(resources),
            load_avg_1m=_load_avg_1m(system_time),
            disk_used_pct=_disk_used_pct(disk),
            raw={"memory": resources.get("memory"), "disk": disk.get("devices"), "interfaces": interfaces.get("statistics")},
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
