"""Monitoring alerts (1.24): gateway down, resource thresholds, stopped services.

Same contract as notification_checks.py (each check returns {dedup_key: context} of what is
bad right now), with one addition: they never judge a single reading. The inputs are the
60-second samples the resource sampler stores (resource_monitor_service, HostResourceSample),
and a problem needs the last N samples to agree before it opens AND before it closes again:

* all N bad            -> problem,
* all N good           -> no problem,
* anything in between  -> keep the current state (open stays open, closed stays closed).

So a single spike never alerts, a flapping link does not open and close the alert every few
minutes, and a sample that could not be read ("unknown") neither raises nor clears anything.
A host without a fresh sample is not judged at all (check_host_offline reports it); its open
problems are kept.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.notification import DEFAULT_SERVICE_WATCHLIST, NotificationProblem
from app.models.resource_sample import HostResourceSample
from app.services import gateway_health_service, notification_settings_service
from app.services.notification_dedup import Problems

# The sampler runs every 60 s; three missed rounds mean the host is not reachable (same limit as metrics_service).
STALE_AFTER = timedelta(seconds=180)
# How far back a service must have been seen running before its absence is reported (samples, ~2 h at 60 s).
SERVICE_EVIDENCE_SAMPLES = 120
_DISK_TYPES = {"ufs", "zfs"}


def verdict(flags: list[bool | None], confirm: int, is_open: bool) -> bool:
    """flags: newest first; True = bad, False = good, None = unknown."""
    window = flags[:confirm]
    if len(window) >= confirm:
        if all(f is True for f in window):
            return True
        if all(f is False for f in window):
            return False
    return is_open


async def _enabled_hosts(db: AsyncSession) -> list[OpnsenseHost]:
    return list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)))).all())


async def _open_keys(db: AsyncSession, problem_type: str) -> set[str]:
    rows = await db.scalars(
        select(NotificationProblem.dedup_key).where(
            NotificationProblem.problem_type == problem_type, NotificationProblem.status == "open"
        )
    )
    return set(rows.all())


async def _recent(db: AsyncSession, host_id, limit: int, *raw_keys: str) -> list[dict]:
    """The newest ``limit`` samples of a host, newest first, with just the JSON keys asked for."""
    columns = [HostResourceSample.sampled_at, HostResourceSample.mem_used_pct, HostResourceSample.load_avg_1m]
    columns += [HostResourceSample.raw[key].as_json() for key in raw_keys]
    rows = (
        await db.execute(
            select(*columns).where(HostResourceSample.host_id == host_id).order_by(HostResourceSample.sampled_at.desc()).limit(limit)
        )
    ).all()
    return [
        {"sampled_at": r[0], "mem_used_pct": r[1], "load_avg_1m": r[2], **{k: r[3 + i] for i, k in enumerate(raw_keys)}}
        for r in rows
    ]


def _fresh(samples: list[dict]) -> bool:
    return bool(samples) and datetime.now(timezone.utc) - samples[0]["sampled_at"] <= STALE_AFTER


def _keys_of_host(open_keys: set[str], host: OpnsenseHost) -> set[str]:
    prefix = f"{host.id}:"
    return {k[len(prefix):] for k in open_keys if k.startswith(prefix)}


# ── Gateways ────────────────────────────────────────────────────────────────

def _gateway_reason(g) -> str:
    if g.state == "offline":
        return "Gateway offline"
    parts = []
    if g.loss is not None:
        parts.append(f"Paketverlust {g.loss:g} %")
    if g.delay is not None:
        parts.append(f"Latenz {g.delay:g} ms")
    return ", ".join(parts) or g.status


async def check_gateway_down(db: AsyncSession) -> dict[str, dict]:
    cfg = await notification_settings_service.get_settings(db)
    confirm = max(1, cfg.alert_confirm_samples)
    loss_limit, delay_limit = cfg.gateway_loss_threshold_pct, cfg.gateway_delay_threshold_ms
    open_keys = await _open_keys(db, "gateway_down")
    problems = Problems()
    for host in await _enabled_hosts(db):
        samples = await _recent(db, host.id, confirm, "gateways")
        if not _fresh(samples):
            problems.preserve_prefixes.add(f"{host.id}:")
            continue
        parsed = [
            None if s["gateways"] is None else {g.name: g for g in gateway_health_service.from_compact(s["gateways"])}
            for s in samples
        ]
        names = {name for p in parsed if p for name in p} | _keys_of_host(open_keys, host)
        for name in sorted(names):
            flags: list[bool | None] = []
            for p in parsed:
                if p is None:
                    flags.append(None)
                else:
                    g = p.get(name)  # a gateway that no longer exists is "fine": its problem resolves
                    flags.append(False if g is None else gateway_health_service.is_bad(g, loss_limit, delay_limit))
            key = f"{host.id}:{name}"
            if not verdict(flags, confirm, key in open_keys):
                continue
            latest = next((p[name] for p in parsed if p and name in p), None)
            problems[key] = {
                "host_id": str(host.id), "host_name": host.name, "gateway": name,
                "grund": _gateway_reason(latest) if latest else "keine aktuellen Messwerte",
                "status": latest.status if latest else None,
                "paketverlust_prozent": latest.loss if latest else None,
                "latenz_ms": latest.delay if latest else None,
            }
    return problems


# ── Resources ───────────────────────────────────────────────────────────────

def disk_groups(devices) -> dict[str, tuple[str, float]]:
    """{group: (label, used %)} — one entry per ZFS pool / UFS device, the fullest dataset of a pool wins."""
    groups: dict[str, tuple[str, float]] = {}
    for dev in devices or []:
        if not isinstance(dev, dict) or dev.get("type") not in _DISK_TYPES:
            continue
        try:
            pct = float(dev["used_pct"])
        except (KeyError, TypeError, ValueError):
            continue
        device = str(dev.get("device") or dev.get("mountpoint") or "?")
        group = device.split("/")[0] if dev["type"] == "zfs" else device
        label = group if dev["type"] == "zfs" else str(dev.get("mountpoint") or device)
        if group not in groups or pct > groups[group][1]:
            groups[group] = (label, pct)
    return groups


async def check_resource_threshold(db: AsyncSession) -> dict[str, dict]:
    cfg = await notification_settings_service.get_settings(db)
    confirm = max(1, cfg.resource_confirm_samples)
    open_keys = await _open_keys(db, "resource_threshold")
    problems = Problems()

    def over(value, limit) -> bool | None:
        if not limit or value is None:
            return None if value is None else False
        return value >= limit

    for host in await _enabled_hosts(db):
        samples = await _recent(db, host.id, confirm, "disk", "swap_used_pct", "cpu_threads")
        if not _fresh(samples):
            problems.preserve_prefixes.add(f"{host.id}:")
            continue
        base = {"host_id": str(host.id), "host_name": host.name}
        was_open = _keys_of_host(open_keys, host)

        # Disk: one problem per pool/device.
        disk = [None if s["disk"] is None else disk_groups(s["disk"]) for s in samples]
        group_names = {g for d in disk if d for g in d} | {k[5:] for k in was_open if k.startswith("disk:")}
        for group in sorted(group_names):
            flags = [None if d is None else (over(d[group][1], cfg.disk_threshold_pct) if group in d else False) for d in disk]
            if verdict(flags, confirm, f"disk:{group}" in was_open):
                label, pct = next((d[group] for d in disk if d and group in d), (group, None))
                problems[f"{host.id}:disk:{group}"] = {
                    **base, "ressource": f"Festplatte {label}", "wert_prozent": pct, "schwelle_prozent": cfg.disk_threshold_pct,
                }

        def simple(key: str, label: str, flags: list, value, threshold, unit: str) -> None:
            if verdict(flags, confirm, key in was_open):
                problems[f"{host.id}:{key}"] = {**base, "ressource": label, "wert": f"{value}{unit}", "schwelle": f"{threshold}{unit}"}

        latest = samples[0]
        simple("memory", "Arbeitsspeicher", [over(s["mem_used_pct"], cfg.memory_threshold_pct) for s in samples],
               latest["mem_used_pct"], cfg.memory_threshold_pct, " %")
        simple("swap", "Swap", [over(s["swap_used_pct"], cfg.swap_threshold_pct) for s in samples],
               latest["swap_used_pct"], cfg.swap_threshold_pct, " %")

        def per_cpu(s):
            if s["load_avg_1m"] is None or not s["cpu_threads"]:
                return None  # thread count unknown (older firmware): the load cannot be judged
            return s["load_avg_1m"] / s["cpu_threads"]

        simple("cpu", "CPU-Last (pro Thread)", [over(per_cpu(s), cfg.load_per_cpu_threshold) for s in samples],
               None if per_cpu(latest) is None else round(per_cpu(latest), 2), cfg.load_per_cpu_threshold, "")
    return problems


# ── Services ────────────────────────────────────────────────────────────────

async def check_service_down(db: AsyncSession) -> dict[str, dict]:
    cfg = await notification_settings_service.get_settings(db)
    confirm = max(1, cfg.alert_confirm_samples)
    watch = set(cfg.service_watchlist if cfg.service_watchlist is not None else DEFAULT_SERVICE_WATCHLIST)
    open_keys = await _open_keys(db, "service_down")
    problems = Problems()
    for host in await _enabled_hosts(db):
        samples = await _recent(db, host.id, confirm + SERVICE_EVIDENCE_SAMPLES, "services")
        if not _fresh(samples):
            problems.preserve_prefixes.add(f"{host.id}:")
            continue
        listings = [s["services"] for s in samples]
        was_open = _keys_of_host(open_keys, host)
        candidates = {k for k in (listings[0] or {}) if k.split("/")[0] in watch} | was_open
        for svc in sorted(candidates):
            if svc.split("/")[0] not in watch:
                continue  # taken off the watchlist: the problem resolves
            flags = [None if sv is None else (sv[svc] == 0 if svc in sv else False) for sv in listings]
            bad = verdict(flags, confirm, svc in was_open)
            # Only a service that was seen running before counts: one that never ran (enabled but idle,
            # e.g. DHCPv6 without a prefix) or was stopped on purpose long ago is not a failure.
            if bad and svc not in was_open and not any(sv and sv.get(svc) == 1 for sv in listings[confirm:]):
                bad = False
            if bad:
                problems[f"{host.id}:{svc}"] = {
                    "host_id": str(host.id), "host_name": host.name, "dienst": svc.split("/")[0],
                    "hinweis": "Dienst läuft nicht mehr; absichtlich gestoppt? Dann aus der Überwachungsliste nehmen.",
                }
    return problems
