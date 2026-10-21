import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from app.errors import ErrorCode

from app import models  # noqa: F401 - registers models with Base.metadata
from app.auth_middleware import AuthMiddleware
from app.security_middleware import OriginCheckMiddleware, SecurityHeadersMiddleware
from app.config import settings
from app.database import SessionLocal
from app import migrate
from app.routers import auth, hosts, logs, rules, groups, aliases, port_forward, search as search_router, users as users_router, tokens as tokens_router, metrics as metrics_router, mcp as mcp_router, compliance as compliance_router, audit_export as audit_export_router, backups as backups_router, oidc as oidc_router, governance as governance_router, overlay as overlay_router, variables as variables_router
from app.routers import one_to_one
from app.services.one_to_one_deploy_service import sweep_expired_one_to_one_deployments
from app.routers import outbound_nat
from app.services.outbound_nat_deploy_service import sweep_expired_outbound_nat_deployments
from app.services import auth_service, audit_service
from app.routers import audit, maintenance, settings as settings_router, notifications as notifications_router
from app.routers import system as system_router
from app.services.rule_deploy_service import sweep_expired_deployments
from app.services.alias_deploy_service import sweep_expired_alias_deployments
from app.services.port_forward_deploy_service import sweep_expired_port_forward_deployments
from app.services import notification_service, notification_settings_service
from app.routers import policy_hierarchy, policy_hierarchy_groups
from app.services.policy_hierarchy_deploy_service import sweep_expired_hierarchy_deployments
from app.services import log_ingest_service, log_settings_service
from app.routers import ipsec as ipsec_router
from app.routers import openvpn as openvpn_router
from app.routers import wireguard as wireguard_router
from app.routers import certificates as certificates_router
from app.routers import network as network_router
from app.routers import snapshots as snapshots_router
from app.routers import network_drift as network_drift_router
from app.routers import diagnostics as diagnostics_router
from app.routers import restore as restore_router
from app.version import APP_VERSION
from app.routers import dashboard as dashboard_router
from app.routers import cluster as cluster_router
from app.routers import services as services_router
from app.routers import gateways as gateways_router
from app.routers import resource_samples as resource_samples_router
from app.services import resource_monitor_service
from app.routers import scheduled_maintenance as scheduled_maintenance_router
from app.routers import maintenance_windows as maintenance_windows_router
from app.routers import rollouts as rollouts_router
from app.routers import rule_analysis as rule_analysis_router
from app.services import scheduled_maintenance_service
from app.services import rollout_engine
from app.services import compliance_service
from app.services import audit_export_service
from app.services import overlay_service, variables_service
from app.services import backup_schedule_service
from app.services import carp_event_service, carp_maintenance_window_service

# Without this, Python's root logger defaults to WARNING with no handler,
# so every logger.info() call in the 5 background loops below (CARP role
# changes, log-ingest counts, sweep results, ...) is silently dropped —
# `docker compose logs backend` then shows only uvicorn's own access log,
# even after days of the loops actually running. Only warning/exception
# calls survived before this (e.g. the one-time generated-admin-password
# banner), which made debugging "is X even running" needlessly hard.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 10
# Prune retention-expired log rows once every N poll cycles rather than
# every tick — cleanup has no reason to run at poll cadence (default
# 30s), and a DELETE scanning by timestamp on every cycle would be wasted
# work once the table is non-trivially sized.
_LOG_PRUNE_EVERY_N_CYCLES = 20
# Same rationale as _LOG_PRUNE_EVERY_N_CYCLES, but the resource-sampling
# loop's own cadence (settings.resource_poll_interval_seconds, default 60s).
_RESOURCE_PRUNE_EVERY_N_CYCLES = 20


async def _deploy_revert_sweep_loop():
    """Background safety net for the deploy lockout protection: reverts any
    RuleDeployment whose confirm window passed without a human confirming
    (see rule_deploy_service.sweep_expired_deployments)."""
    while True:
        try:
            async with SessionLocal() as db:
                reverted = await sweep_expired_deployments(db)
            if reverted:
                logger.info("Auto-reverted %d unconfirmed rule deployment(s)", len(reverted))
        except Exception:
            logger.exception("Deploy-revert sweep failed")
        try:
            async with SessionLocal() as db:
                reverted = await sweep_expired_alias_deployments(db)
            if reverted:
                logger.info("Auto-reverted %d unconfirmed alias deployment(s)", len(reverted))
        except Exception:
            logger.exception("Alias deploy-revert sweep failed")
        try:
            async with SessionLocal() as db:
                reverted = await sweep_expired_port_forward_deployments(db)
            if reverted:
                logger.info("Auto-reverted %d unconfirmed port-forward deployment(s)", len(reverted))
        except Exception:
            logger.exception("Port-forward deploy-revert sweep failed")
        try:
            async with SessionLocal() as db:
                reverted = await sweep_expired_outbound_nat_deployments(db)
            if reverted:
                logger.info("Auto-reverted %d unconfirmed outbound-nat deployment(s)", len(reverted))
        except Exception:
            logger.exception("Outbound NAT deploy-revert sweep failed")
        try:
            async with SessionLocal() as db:
                reverted = await sweep_expired_one_to_one_deployments(db)
            if reverted:
                logger.info("Auto-reverted %d unconfirmed one-to-one deployment(s)", len(reverted))
        except Exception:
            logger.exception("1:1 NAT deploy-revert sweep failed")
        try:
            async with SessionLocal() as db:
                reverted = await carp_maintenance_window_service.sweep_expired(db)
            if reverted:
                logger.info("CARP-Auto-Revert: %d Zeitfenster verarbeitet", len(reverted))
        except Exception:
            logger.exception("CARP-Auto-Revert-Sweep fehlgeschlagen")
        try:
            async with SessionLocal() as db:
                reverted = await sweep_expired_hierarchy_deployments(db)
            if reverted:
                logger.info("Auto-reverted %d unconfirmed policy-hierarchy deployment(s)", len(reverted))
        except Exception:
            logger.exception("Policy-hierarchy deploy-revert sweep failed")
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)


async def _notification_poll_loop():
    """Separate, slower loop from the deploy-revert sweep above: this one
    makes live HTTP calls to every real firewall per cycle (host status,
    certificates, live rules for drift), so it must not run at the sweep's
    10s cadence — that cadence is a security control for the deploy-confirm
    window, not applicable here. Interval is admin-configurable via
    NotificationSettings.poll_interval_seconds, floored by
    settings.notification_poll_min_interval_seconds."""
    while True:
        interval = settings.notification_poll_min_interval_seconds
        try:
            async with SessionLocal() as db:
                await notification_service.run_notification_checks(db)
                row = await notification_settings_service.get_settings(db)
                interval = max(row.poll_interval_seconds, settings.notification_poll_min_interval_seconds)
        except Exception:
            logger.exception("Notification poll failed")
        try:
            async with SessionLocal() as db:
                events = await carp_event_service.detect_and_record(db)
            for event in events:
                logger.info(
                    "CARP-Rollenwechsel auf %s: %s -> %s", event.host_name, event.from_status, event.to_status,
                )
        except Exception:
            logger.exception("CARP-Ereignis-Erkennung fehlgeschlagen")
        await asyncio.sleep(interval)


async def _log_ingest_poll_loop():
    """Third background loop, mirroring _notification_poll_loop's shape:
    a live GET call (diagnostics/firewall/log) to every enabled host per
    cycle, so it runs on its own admin-configurable cadence rather than
    the 10s deploy-revert sweep's. Interval comes from
    LogSettings.poll_interval_seconds, floored by
    settings.log_poll_min_interval_seconds. Retention pruning runs only
    every _LOG_PRUNE_EVERY_N_CYCLES cycles (see constant above)."""
    cycle = 0
    while True:
        interval = settings.log_poll_min_interval_seconds
        cycle += 1
        try:
            async with SessionLocal() as db:
                row = await log_settings_service.get_settings(db)
                interval = max(row.poll_interval_seconds, settings.log_poll_min_interval_seconds)
                enabled = row.enabled
                retention_days = row.retention_days
                max_size_gb = row.max_size_gb
                await db.commit()

                if enabled:
                    result = await log_ingest_service.poll_all_hosts(db)
                    if result["inserted"]:
                        logger.info(
                            "Log-Ingest: %d neue Einträge über %d Host(s) (%d fehlgeschlagen)",
                            result["inserted"], result["hosts_polled"], result["hosts_failed"],
                        )

                if cycle % _LOG_PRUNE_EVERY_N_CYCLES == 0:
                    deleted = await log_ingest_service.prune_old_entries(db, retention_days)
                    if deleted:
                        logger.info("Log-Retention: %d alte Einträge gelöscht", deleted)
                    trimmed = await log_ingest_service.enforce_size_limit(db, max_size_gb)
                    if trimmed:
                        logger.info("Log-Größenlimit (%.1f GB): %d älteste Einträge gelöscht", max_size_gb, trimmed)
        except Exception:
            logger.exception("Log-Ingest-Poll fehlgeschlagen")
        await asyncio.sleep(interval)


async def _resource_poll_loop():
    """Fourth background loop, same shape as _log_ingest_poll_loop but with
    a fixed interval (settings.resource_poll_interval_seconds) — v1
    deliberately has no admin-editable settings row for this, see
    resource_monitor_service.py's module docstring."""
    cycle = 0
    while True:
        cycle += 1
        try:
            async with SessionLocal() as db:
                result = await resource_monitor_service.poll_all_hosts(db)
                if result["hosts_failed"]:
                    logger.warning(
                        "Resource-Sampling: %d von %d Host(s) fehlgeschlagen",
                        result["hosts_failed"], result["hosts_polled"],
                    )
                if cycle % _RESOURCE_PRUNE_EVERY_N_CYCLES == 0:
                    deleted = await resource_monitor_service.prune_old_samples(db, settings.resource_sample_retention_days)
                    if deleted:
                        logger.info("Resource-Sample-Retention: %d alte Einträge gelöscht", deleted)
        except Exception:
            logger.exception("Resource-Poll fehlgeschlagen")
        await asyncio.sleep(settings.resource_poll_interval_seconds)


_SCHEDULED_MAINTENANCE_POLL_SECONDS = 60
_RUNNING_MAINTENANCE_POLL_SECONDS = 15


async def _scheduled_maintenance_poll_loop():
    """Fifth background loop: fires due one-shot ScheduledMaintenance rows.
    A fixed 60s cadence is plenty — these are human-picked timestamps, not
    a sub-minute-precision cron."""
    while True:
        try:
            async with SessionLocal() as db:
                result = await scheduled_maintenance_service.fire_due(db)
                if result["due"]:
                    logger.info(
                        "Geplante Wartung: %d fällig, %d ausgeführt, %d blockiert/fehlgeschlagen",
                        result["due"], result["fired"], result["blocked_or_failed"],
                    )
                in_progress = await scheduled_maintenance_service.advance_running(db)
        except Exception:
            logger.exception("Geplante-Wartung-Poll fehlgeschlagen")
            in_progress = 0
        # Firmware rollouts (1.25) only ever create schedules; the next tick of the lines above fires them.
        try:
            async with SessionLocal() as db:
                in_progress += await rollout_engine.advance_runs(db)
        except Exception:
            logger.exception("Rollout-Poll fehlgeschlagen")
        # A handover run is stepped quickly (the partner should take over within a minute or two);
        # otherwise the relaxed cadence is plenty.
        await asyncio.sleep(_RUNNING_MAINTENANCE_POLL_SECONDS if in_progress else _SCHEDULED_MAINTENANCE_POLL_SECONDS)


_COMPLIANCE_FIRST_RUN_SECONDS = 120
_COMPLIANCE_INTERVAL_SECONDS = 6 * 3600


async def _compliance_poll_loop():
    """Sixth background loop: refreshes every enabled host's security-posture result.
    Each check is one read-only config download per firewall, so every six hours
    is plenty; the first run waits a little so a restart is not a thundering herd."""
    await asyncio.sleep(_COMPLIANCE_FIRST_RUN_SECONDS)
    while True:
        try:
            async with SessionLocal() as db:
                await compliance_service.check_all(db)
        except Exception:
            logger.exception("Sicherheitslage-Prüfung fehlgeschlagen")
        await asyncio.sleep(_COMPLIANCE_INTERVAL_SECONDS)


_AUDIT_EXPORT_POLL_SECONDS = 10


async def _audit_export_poll_loop():
    """Seventh background loop: forwards new audit events (syslog / webhook) when configured.
    Cheap when off (one settings read); keeps draining while batches come back full."""
    while True:
        shipped = 0
        try:
            async with SessionLocal() as db:
                shipped = await audit_export_service.run_once(db)
        except Exception:
            logger.exception("Audit-Export fehlgeschlagen")
        await asyncio.sleep(0 if shipped >= audit_export_service.BATCH else _AUDIT_EXPORT_POLL_SECONDS)


_BACKUP_POLL_SECONDS = 15 * 60


async def _backup_poll_loop():
    """Eighth background loop: takes due scheduled config backups, prunes, copies offsite."""
    await asyncio.sleep(90)
    while True:
        try:
            async with SessionLocal() as db:
                result = await backup_schedule_service.run_due(db)
            if result["taken"] or result["failed"] or result["uploaded"]:
                logger.info("Geplante Backups: %s", result)
        except Exception:
            logger.exception("Geplante Backups fehlgeschlagen")
        await asyncio.sleep(_BACKUP_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await migrate.run()
    async with SessionLocal() as db:
        await auth_service.bootstrap_admin(db)
        try:
            await overlay_service.sync(db)  # hub config + proxy rule follow the database after every start
        except Exception:
            logger.exception("Overlay-Konfiguration konnte nicht geschrieben werden")
    if any(o.startswith("https://") for o in settings.allowed_origins) and not settings.cookie_secure:
        logger.warning(
            "FRONTEND_ORIGIN is https:// but COOKIE_SECURE is false — the session cookie "
            "would also be sent over plain HTTP. Set COOKIE_SECURE=true."
        )
    if settings.allow_insecure_firewall_urls:
        logger.warning("ALLOW_INSECURE_FIREWALL_URLS is on: firewall API credentials may travel over plain HTTP.")

    sweep_task = asyncio.create_task(_deploy_revert_sweep_loop())
    notification_task = asyncio.create_task(_notification_poll_loop())
    log_ingest_task = asyncio.create_task(_log_ingest_poll_loop())
    resource_task = asyncio.create_task(_resource_poll_loop())
    scheduled_maintenance_task = asyncio.create_task(_scheduled_maintenance_poll_loop())
    compliance_task = asyncio.create_task(_compliance_poll_loop())
    audit_export_task = asyncio.create_task(_audit_export_poll_loop())
    backup_task = asyncio.create_task(_backup_poll_loop())
    try:
        yield
    finally:
        sweep_task.cancel()
        notification_task.cancel()
        log_ingest_task.cancel()
        resource_task.cancel()
        scheduled_maintenance_task.cancel()
        compliance_task.cancel()
        audit_export_task.cancel()
        backup_task.cancel()
        for task in (sweep_task, notification_task, log_ingest_task, resource_task, scheduled_maintenance_task, compliance_task, audit_export_task, backup_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


API_DESCRIPTION = (
    "REST API of OPNexus, the central management for OPNsense firewall fleets.\n\n"
    "**Authentication:** send a personal API token as `Authorization: Bearer opx_...` "
    "(create one under *System > API tokens*). Scopes: `read` (GET only), `deploy` "
    "(+ roll out rules, NAT, VPN, maintenance), `admin` (+ users, settings, hosts). "
    "A token never exceeds its owner's role. Token and account management is session-only.\n\n"
    "Errors are `{\"detail\": {\"code\": \"...\", \"params\": {...}}}` with a stable `code`."
)

# The interactive Swagger/ReDoc pages load scripts from a CDN, which the proxy's
# Content-Security-Policy forbids; the schema itself is served (behind auth) for
# any client that renders it (Postman, openapi-generator, an AI assistant).
app = FastAPI(
    title="OPNexus", version=APP_VERSION, lifespan=lifespan, description=API_DESCRIPTION,
    docs_url=None, redoc_url=None, openapi_url="/api/openapi.json",
)


def _custom_openapi():
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["bearerAuth"] = {
            "type": "http", "scheme": "bearer", "description": "Personal API token (opx_...)",
        }
        schema["security"] = [{"bearerAuth": []}]
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _custom_openapi

@app.exception_handler(RequestValidationError)
async def _safe_validation_error(request: Request, exc: RequestValidationError):
    """FastAPI's default 422 body echoes the offending `input` (and validator
    `ctx`) back — for password/key/PSK fields that puts the secret into
    responses that browsers, proxies and error telemetry may record. Only
    where the problem is and what it is are returned."""
    return JSONResponse(
        status_code=422,
        content={"detail": [{"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")} for err in exc.errors()]},
    )


@app.exception_handler(variables_service.MissingVariables)
async def _missing_variables(request: Request, exc: variables_service.MissingVariables):
    """A deploy that needs a value no target host has defined is refused before anything is written."""
    return JSONResponse(status_code=422, content={"detail": {"code": ErrorCode.VARIABLES_MISSING.value, "params": {"details": str(exc)}}})


@app.exception_handler(DBAPIError)
async def _database_text_errors(request: Request, exc: DBAPIError):
    """A NUL byte in any text field cannot be stored by Postgres and used to
    surface as a 500 plus a dropped connection (found by fuzzing). That is bad
    input, not a server fault. Anything else stays a normal 500."""
    if type(exc.orig).__name__.endswith("CharacterNotInRepertoireError") or "0x00" in str(exc.orig):
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": ErrorCode.INVALID_CHARACTERS.value, "params": {}}},
        )
    raise exc


app.add_middleware(AuthMiddleware)
# Inside CORS so preflights are answered first; outside Auth so a forged
# cross-site write is rejected before any session lookup happens.
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
# Added last so it wraps outermost (Starlette runs the last-added
# middleware first) — CORS must see and answer OPTIONS preflight requests
# before AuthMiddleware ever gets a chance to 401 them.
# An explicit FRONTEND_ORIGIN (required for any non-localhost deployment,
# see config.py) is matched exactly, never as a regex fragment — avoids
# turning operator-supplied input into an accidental regex hole.
_cors_origin_regex = (
    "^(" + "|".join(re.escape(o) for o in settings.allowed_origins) + ")$"
    if settings.allowed_origins else settings.cors_origin_regex
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Browsers hide all but a few standard response headers cross-origin
    # by default — the backup-download endpoint's filename (carried in
    # Content-Disposition) needs this to reach the frontend's fetch().
    expose_headers=["Content-Disposition"],
)

app.include_router(auth.router)
app.include_router(hosts.router, prefix="/api")
app.include_router(rules.router, prefix="/api")
app.include_router(search_router.router, prefix="/api")
app.include_router(users_router.router, prefix="/api")
app.include_router(tokens_router.router, prefix="/api")
app.include_router(metrics_router.router, prefix="/api")
app.include_router(mcp_router.router, prefix="/api")
app.include_router(compliance_router.router, prefix="/api")
app.include_router(audit_export_router.router, prefix="/api")
app.include_router(backups_router.router, prefix="/api")
app.include_router(oidc_router.router, prefix="/api")
app.include_router(governance_router.router, prefix="/api")
app.include_router(overlay_router.router, prefix="/api")
app.include_router(variables_router.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(groups.router, prefix="/api")
app.include_router(aliases.router, prefix="/api")
app.include_router(port_forward.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok"}

app.include_router(one_to_one.router, prefix="/api")

app.include_router(outbound_nat.router, prefix="/api")

app.include_router(audit.router, prefix="/api")

app.include_router(maintenance.router, prefix="/api")

app.include_router(settings_router.router, prefix="/api")
app.include_router(system_router.router, prefix="/api")

app.include_router(notifications_router.router, prefix="/api")

app.include_router(policy_hierarchy_groups.router, prefix="/api")
app.include_router(policy_hierarchy.router, prefix="/api")

# No background sweep loop for this router — see
# ipsec_deploy_service.py's module docstring: there is no pending/
# auto-revert state here to sweep.
app.include_router(ipsec_router.router, prefix="/api")

# Same as above — no sweep loop, see openvpn_deploy_service.py's module
# docstring (identical no-pending/auto-revert rationale as IPsec).
app.include_router(openvpn_router.router, prefix="/api")

# Same as above — no sweep loop, see wireguard_deploy_service.py's module
# docstring (identical no-pending/auto-revert rationale as IPsec/OpenVPN).
app.include_router(wireguard_router.router, prefix="/api")

# Trust-Store + ACME certificate management — single-host entities (no
# multi-host targeting, see the models' own docstrings), no sweep loop
# either: Trust-Store has no restart/apply step to auto-revert at all, and
# ACME's register/sign are one-shot external-network triggers, not
# config-write-then-reconfigure flows with a "config live, reload
# unconfirmed" middle state to sweep.
app.include_router(certificates_router.managed_router, prefix="/api")
app.include_router(certificates_router.acme_accounts_router, prefix="/api")
app.include_router(certificates_router.acme_certificates_router, prefix="/api")
app.include_router(restore_router.router, prefix="/api")
app.include_router(dashboard_router.router, prefix="/api")
app.include_router(cluster_router.router, prefix="/api")
app.include_router(services_router.router, prefix="/api")
app.include_router(gateways_router.router, prefix="/api")
app.include_router(network_router.router, prefix="/api")
app.include_router(snapshots_router.router, prefix="/api")
app.include_router(network_drift_router.router, prefix="/api")
app.include_router(diagnostics_router.router, prefix="/api")
app.include_router(resource_samples_router.router, prefix="/api")
app.include_router(scheduled_maintenance_router.router, prefix="/api")
app.include_router(maintenance_windows_router.router, prefix="/api")
app.include_router(rollouts_router.router, prefix="/api")
app.include_router(rule_analysis_router.router, prefix="/api")
