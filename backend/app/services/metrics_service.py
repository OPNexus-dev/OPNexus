"""Prometheus exposition of the fleet state (GET /api/metrics).

Deliberately database-only, like the dashboard summary: a scrape must never
fan out live calls to every firewall (an offline host would stall it past the
scraper's timeout and a 15 s scrape interval would hammer the fleet). The
inputs are what OPNexus' own pollers already store:

* host reachability, resources, gateway health and service state — the 60 s resource sampler (a host that
  produced no sample for ``STALE_AFTER`` is reported down),
* CARP role — the latest observed transition (carp_events),
* open problems — the notification engine's problem table,
* pending actions — the same list the dashboard shows.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.carp_event import CarpEvent
from app.models.compliance_result import ComplianceResult
from app.models.host import OpnsenseHost
from app.models.notification import DEFAULT_SERVICE_WATCHLIST, NotificationProblem
from app.models.resource_sample import HostResourceSample
from app.models.rollout import RolloutRun
from app.models.scheduled_maintenance import ScheduledMaintenance
from app.routers.dashboard import collect_pending_actions
from app.services import compliance_service, gateway_health_service, notification_settings_service
from app.version import APP_VERSION

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
# The sampler runs every 60 s; three missed rounds mean the host is not reachable.
STALE_AFTER = timedelta(seconds=180)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Exposition:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def family(self, name: str, help_text: str, kind: str = "gauge") -> None:
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {kind}")

    def sample(self, name: str, value: float, **labels: str) -> None:
        rendered = ",".join(f'{key}="{_escape(str(val))}"' for key, val in labels.items() if val is not None)
        number = repr(float(value)) if not float(value).is_integer() else str(int(value))
        self.lines.append(f"{name}{{{rendered}}} {number}" if rendered else f"{name} {number}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


async def render(db: AsyncSession) -> str:
    now = datetime.now(timezone.utc)
    out = _Exposition()

    out.family("opnexus_info", "OPNexus version (always 1).")
    out.sample("opnexus_info", 1, version=APP_VERSION)

    hosts = (await db.scalars(select(OpnsenseHost).order_by(OpnsenseHost.name))).all()
    latest_sample = (
        select(HostResourceSample.host_id, func.max(HostResourceSample.sampled_at).label("at"))
        .group_by(HostResourceSample.host_id)
        .subquery()
    )
    samples = {
        sample.host_id: sample
        for sample in (
            await db.scalars(
                select(HostResourceSample).join(
                    latest_sample,
                    (HostResourceSample.host_id == latest_sample.c.host_id)
                    & (HostResourceSample.sampled_at == latest_sample.c.at),
                )
            )
        ).all()
    }
    latest_event = (
        select(CarpEvent.host_id, func.max(CarpEvent.detected_at).label("at")).group_by(CarpEvent.host_id).subquery()
    )
    carp_state = {
        event.host_id: event.to_status
        for event in (
            await db.scalars(
                select(CarpEvent).join(
                    latest_event, (CarpEvent.host_id == latest_event.c.host_id) & (CarpEvent.detected_at == latest_event.c.at)
                )
            )
        ).all()
    }

    out.family("opnexus_hosts", "Number of managed firewalls.")
    out.sample("opnexus_hosts", len(hosts))
    out.family("opnexus_host_enabled", "1 if monitoring of the host is enabled.")
    out.family("opnexus_host_up", "1 if the host answered the resource sampler within the last 3 minutes.")
    out.family("opnexus_host_last_sample_timestamp_seconds", "Unix time of the last successful resource sample.")
    out.family("opnexus_host_memory_used_percent", "Memory in use, percent (last sample).")
    out.family("opnexus_host_load1", "1-minute load average (last sample).")
    out.family("opnexus_host_disk_used_percent", "Fullest disk, percent (last sample).")
    out.family("opnexus_host_swap_used_percent", "Swap in use, percent (last sample; absent without swap).")
    out.family("opnexus_host_gateway_up", "1 if the gateway is not down (online, degraded or not monitored), 0 if OPNsense reports it down.")
    out.family("opnexus_host_gateway_loss_percent", "Packet loss to the gateway monitor address, percent (last sample).")
    out.family("opnexus_host_gateway_delay_seconds", "Round-trip time to the gateway monitor address (last sample).")
    out.family("opnexus_host_gateway_stddev_seconds", "Round-trip time deviation to the gateway monitor address (last sample).")
    out.family("opnexus_host_service_running", "1 if a watched service (Settings > Notifications) runs, 0 if it is stopped (last sample).")
    out.family("opnexus_host_api_rights_missing", "1 if the firewall refused a side reading (read=gateways, services, swap, cpu) because the API key lacks the right (last sample); absent otherwise.")
    configured = (await notification_settings_service.get_settings(db)).service_watchlist
    watchlist = set(DEFAULT_SERVICE_WATCHLIST if configured is None else configured)
    out.family("opnexus_host_carp_master", "1 if the host's last observed CARP role is MASTER, 0 if BACKUP; absent if unknown.")
    for host in hosts:
        labels = {"host": host.name, "cluster": host.cluster_name or ""}
        sample = samples.get(host.id)
        up = bool(host.enabled and sample is not None and now - sample.sampled_at <= STALE_AFTER)
        out.sample("opnexus_host_enabled", 1 if host.enabled else 0, **labels)
        out.sample("opnexus_host_up", 1 if up else 0, **labels)
        if sample is not None:
            out.sample("opnexus_host_last_sample_timestamp_seconds", sample.sampled_at.timestamp(), **labels)
            for name, value in (
                ("opnexus_host_memory_used_percent", sample.mem_used_pct),
                ("opnexus_host_load1", sample.load_avg_1m),
                ("opnexus_host_disk_used_percent", sample.disk_used_pct),
            ):
                if value is not None:
                    out.sample(name, value, **labels)
        if sample is not None:
            raw = sample.raw or {}
            if raw.get("swap_used_pct") is not None:
                out.sample("opnexus_host_swap_used_percent", raw["swap_used_pct"], **labels)
            for gw in gateway_health_service.from_compact(raw.get("gateways")):
                gw_labels = {**labels, "gateway": gw.name}
                out.sample("opnexus_host_gateway_up", 0 if gw.state == "offline" else 1, **gw_labels)
                for name, value, factor in (
                    ("opnexus_host_gateway_loss_percent", gw.loss, 1), ("opnexus_host_gateway_delay_seconds", gw.delay, 0.001),
                    ("opnexus_host_gateway_stddev_seconds", gw.stddev, 0.001),
                ):
                    if value is not None:
                        out.sample(name, round(value * factor, 6), **gw_labels)
            for read in sorted(str(r) for r in raw.get("denied") or []):
                out.sample("opnexus_host_api_rights_missing", 1, **labels, read=read)
            for key, running in sorted((raw.get("services") or {}).items()):
                if key.split("/")[0] in watchlist:
                    out.sample("opnexus_host_service_running", running, **labels, service=key.split("/")[0], instance=key.split("/", 1)[1] if "/" in key else None)
        if carp_state.get(host.id) in ("MASTER", "BACKUP"):
            out.sample("opnexus_host_carp_master", 1 if carp_state[host.id] == "MASTER" else 0, **labels)

    out.family("opnexus_host_compliance_score", "Security posture score 0-100 of the last check (absent if never checked).")
    out.family("opnexus_host_compliance_findings", "Failed security checks of the last check, by severity.")
    results = {r.host_id: r for r in (await db.scalars(select(ComplianceResult))).all()}
    for host in hosts:
        row = results.get(host.id)
        if row is None or row.score is None:
            continue
        labels = {"host": host.name, "cluster": host.cluster_name or ""}
        out.sample("opnexus_host_compliance_score", row.score, **labels)
        for severity, count in compliance_service.counts(row.findings).items():
            out.sample("opnexus_host_compliance_findings", count, severity=severity, **labels)

    out.family("opnexus_pending_actions", "Deployments and operations that need attention, by kind and status.")
    counts: dict[tuple[str, str], int] = {}
    for action in await collect_pending_actions(db):
        counts[(action.kind, action.status)] = counts.get((action.kind, action.status), 0) + 1
    for (kind, status), count in sorted(counts.items()):
        out.sample("opnexus_pending_actions", count, kind=kind, status=status)

    out.family("opnexus_problems_open", "Open problems known to the notification engine, by type.")
    rows = (
        await db.execute(
            select(NotificationProblem.problem_type, func.count())
            .where(NotificationProblem.status == "open")
            .group_by(NotificationProblem.problem_type)
        )
    ).all()
    for problem_type, count in sorted(rows):
        out.sample("opnexus_problems_open", count, type=problem_type)

    out.family("opnexus_scheduled_maintenance_pending", "Scheduled maintenance jobs that have not run yet.")
    pending = await db.scalar(select(func.count()).select_from(ScheduledMaintenance).where(ScheduledMaintenance.status == "pending"))
    out.sample("opnexus_scheduled_maintenance_pending", pending or 0)

    out.family("opnexus_rollout_runs", "Fleet rollout runs by status (waiting_window, wave_running, paused, succeeded, ...).")
    for status, count in sorted((await db.execute(select(RolloutRun.status, func.count()).group_by(RolloutRun.status))).all()):
        out.sample("opnexus_rollout_runs", count, status=status)

    return out.text()
