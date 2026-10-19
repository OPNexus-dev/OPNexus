"""Orchestrates the notification feature: runs each enabled check, dedups
against previously open problems, and delivers on transitions. The single
testable entry point — called directly by main.py's background loop AND
directly by tests, same pattern as rule_deploy_service.sweep_expired_deployments.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationProblem, NotificationSettings
from app.services import notification_checks, notification_dedup, notification_delivery
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
]

# Event-like problems: when they drop out of the window nothing was "fixed", so no
# "behoben" message goes out for them.
_NO_RESOLVED_NOTICE = {"scheduled_maintenance_failed"}


async def _deliver(settings_row: NotificationSettings, problem: NotificationProblem, transition: str) -> None:
    """Best-effort, not an audit trail: attempted once per transition and
    never retried, even on failure — avoids retry-storming a broken webhook
    URL or SMTP relay every poll cycle."""
    if transition == "resolved" and problem.problem_type in _NO_RESOLVED_NOTICE:
        problem.resolved_notified_at = _now()
        return
    if settings_row.webhook_enabled:
        try:
            await notification_delivery.send_webhook(settings_row, problem, transition)
        except notification_delivery.NotificationDeliveryError:
            logger.exception("Webhook delivery failed for %s/%s", problem.problem_type, problem.dedup_key)
    if settings_row.email_enabled:
        try:
            subject, body = notification_delivery.render_message(problem, transition)
            await notification_delivery.send_email(settings_row, subject, body)
        except notification_delivery.NotificationDeliveryError:
            logger.exception("Email delivery failed for %s/%s", problem.problem_type, problem.dedup_key)

    if transition == "resolved":
        problem.resolved_notified_at = _now()
    else:
        problem.last_notified_at = _now()


async def run_notification_checks(db: AsyncSession) -> dict:
    settings_row = await notification_settings_service.get_settings(db)
    result = {"opened": 0, "resolved": 0}

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
        for problem in opened:
            await _deliver(settings_row, problem, "opened")
        for problem in resolved:
            await _deliver(settings_row, problem, "resolved")
        result["opened"] += len(opened)
        result["resolved"] += len(resolved)

    await db.commit()
    return result
