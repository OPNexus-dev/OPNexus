"""Orchestrates the notification feature: runs each enabled check, dedups
against previously open problems, and delivers on transitions. The single
testable entry point — called directly by main.py's background loop AND
directly by tests, same pattern as rule_deploy_service.sweep_expired_deployments.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationChannel, NotificationProblem, NotificationSettings
from app.services import monitor_checks, notification_channels, notification_checks, notification_dedup, notification_delivery
from app.services import notification_settings_service

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# (problem_type, settings-enabled-flag, check function)
_CHECKS = [
    ("host_offline", "host_offline_enabled", notification_checks.check_host_offline),
    ("cert_expiry", "cert_expiring_enabled", notification_checks.check_certificates),
    ("rule_drift", "rule_drift_enabled", notification_checks.check_rule_drift),
    ("revert_failed", "revert_failed_enabled", notification_checks.check_revert_failed),
    ("maintenance_unknown", "maintenance_unknown_enabled", notification_checks.check_maintenance_unknown),
    ("carp_split_brain", "carp_health_enabled", notification_checks.check_carp_health),
    ("nat_drift", "nat_drift_enabled", notification_checks.check_nat_drift),
    ("network_drift", "network_drift_enabled", notification_checks.check_network_drift),
    ("carp_auto_revert_failed", "carp_auto_revert_failed_enabled", notification_checks.check_carp_auto_revert_failed),
    ("scheduled_maintenance_failed", "scheduled_maintenance_failed_enabled", notification_checks.check_scheduled_maintenance_failed),
    ("backup_stale", "backup_failed_enabled", notification_checks.check_backup_stale),
    ("backup_offsite_failed", "backup_failed_enabled", notification_checks.check_backup_offsite),
    ("compliance_finding", "compliance_alerts_enabled", notification_checks.check_compliance),
    ("gateway_down", "gateway_down_enabled", monitor_checks.check_gateway_down),
    ("resource_threshold", "resource_threshold_enabled", monitor_checks.check_resource_threshold),
    ("service_down", "service_down_enabled", monitor_checks.check_service_down),
    ("api_rights_missing", "api_rights_enabled", monitor_checks.check_api_rights_missing),
    ("rollout_paused", "rollout_problems_enabled", notification_checks.check_rollout_paused),
    ("rollout_start_failed", "rollout_problems_enabled", notification_checks.check_rollout_start_failed),
]

# Event-like problems: when they drop out of the window nothing was "fixed", so no
# "behoben" message goes out for them.
_NO_RESOLVED_NOTICE = {"scheduled_maintenance_failed", "rollout_start_failed"}


# Delivery limits (1.29): a hanging channel must neither delay the others nor hold up the check loop.
DELIVERY_TIMEOUT_SECONDS = 20.0    # one target (webhook, e-mail, one channel) for one message
DELIVERY_CYCLE_BUDGET_SECONDS = 90.0  # everything one check cycle sends, in total
DELIVERY_PARALLEL = 10             # targets served at the same time within a cycle


async def _attempt(label: str, make_call, gate: asyncio.Semaphore | None = None, started: list | None = None) -> None:
    """One delivery target: its own timeout, its own failure — never an exception for the caller. Texts logged
    here come from NotificationDeliveryError (names the channel and the HTTP status, never the URL or token)."""
    try:
        if gate is not None:
            await gate.acquire()
        if started is not None:
            started.append(label)  # the target was really tried (a cancelled wait for the gate does not count)
        try:
            await asyncio.wait_for(make_call(), DELIVERY_TIMEOUT_SECONDS)
        finally:
            if gate is not None:
                gate.release()
    except notification_delivery.NotificationDeliveryError:
        logger.exception("%s delivery failed", label)
    except asyncio.TimeoutError:
        logger.warning("%s delivery timed out after %.0f s", label, DELIVERY_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a broken target must not take the others down
        logger.warning("%s delivery failed unexpectedly (%s)", label, type(exc).__name__)


async def _deliver(
    settings_row: NotificationSettings, problem: NotificationProblem, transition: str, channels: list[NotificationChannel] = (),
    gate: asyncio.Semaphore | None = None,
) -> None:
    """Best-effort, not an audit trail: attempted once per transition and
    never retried, even on failure — avoids retry-storming a broken webhook
    URL or SMTP relay every poll cycle. Webhook, e-mail and every channel are
    served in parallel, each with its own timeout (the order has no meaning)."""
    if transition == "resolved" and problem.problem_type in _NO_RESOLVED_NOTICE:
        problem.resolved_notified_at = _now()
        return
    where = f"{problem.problem_type}/{problem.dedup_key}"
    calls = []
    started: list = []
    if settings_row.webhook_enabled:
        calls.append(_attempt(f"Webhook for {where}", lambda: notification_delivery.send_webhook(settings_row, problem, transition), gate, started))
    if settings_row.email_enabled:
        async def email():
            subject, body = notification_delivery.render_message(problem, transition)
            await notification_delivery.send_email(settings_row, subject, body)
        calls.append(_attempt(f"Email for {where}", email, gate, started))
    for channel in channels:
        calls.append(_attempt(f"Channel {channel.name} ({channel.channel_type}) for {where}",
                              lambda channel=channel: notification_channels.send(channel, problem, transition), gate, started))
    try:
        await asyncio.gather(*calls, return_exceptions=True)
    finally:
        # "Attempted" is what is recorded (also when a target failed or timed out). A message the cycle budget
        # cancelled before ANY target was tried stays unrecorded: an opened problem is sent again next cycle.
        if started or not calls:
            if transition == "resolved":
                problem.resolved_notified_at = _now()
            else:
                problem.last_notified_at = _now()


async def _deliver_all(settings_row: NotificationSettings, pending: list[tuple[NotificationProblem, str]],
                       channels: list[NotificationChannel]) -> None:
    """Everything this cycle found, in parallel, within DELIVERY_CYCLE_BUDGET_SECONDS in total."""
    if not pending:
        return
    gate = asyncio.Semaphore(DELIVERY_PARALLEL)
    tasks = [asyncio.ensure_future(_deliver(settings_row, problem, transition, channels, gate)) for problem, transition in pending]
    done, late = await asyncio.wait(tasks, timeout=DELIVERY_CYCLE_BUDGET_SECONDS)
    for task in late:
        task.cancel()
    if late:
        logger.warning("Notification delivery stopped after %.0f s: %d of %d messages were not completed",
                       DELIVERY_CYCLE_BUDGET_SECONDS, len(late), len(tasks))
        await asyncio.gather(*late, return_exceptions=True)  # let the cancelled ones record "attempted"
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Notification delivery failed unexpectedly (%s)", type(task.exception()).__name__)


async def run_notification_checks(db: AsyncSession) -> dict:
    settings_row = await notification_settings_service.get_settings(db)
    result = {"opened": 0, "resolved": 0}
    channels = await notification_channels.enabled_channels(db)
    pending: list[tuple[NotificationProblem, str]] = []

    for problem_type, enabled_flag, check_fn in _CHECKS:
        if not getattr(settings_row, enabled_flag):
            # Deliberately leaves any already-open problems of this type
            # untouched rather than auto-resolving them — disabling a
            # check just stops new notifications, it isn't a claim that
            # the underlying problem went away.
            continue
        try:
            current = await check_fn(db)
        except Exception:
            logger.exception("Notification check %s failed", problem_type)
            continue

        opened, resolved = await notification_dedup.diff_and_update(db, problem_type, current)
        pending += [(problem, "opened") for problem in opened] + [(problem, "resolved") for problem in resolved]
        result["opened"] += len(opened)
        result["resolved"] += len(resolved)

    # Open problems that no target was ever tried for (an earlier cycle ran out of time) are sent now.
    enabled_types = [t for t, flag, _fn in _CHECKS if getattr(settings_row, flag)]
    already = {id(problem) for problem, _transition in pending}
    undelivered = (await db.scalars(
        select(NotificationProblem).where(
            NotificationProblem.status == "open", NotificationProblem.last_notified_at.is_(None),
            NotificationProblem.problem_type.in_(enabled_types),
        )
    )).all() if enabled_types else []
    pending += [(problem, "opened") for problem in undelivered if id(problem) not in already]
    # Delivered once all checks ran (and in parallel): a slow target no longer holds up the checks after it.
    await _deliver_all(settings_row, pending, channels)
    await db.commit()
    return result
