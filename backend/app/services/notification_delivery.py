"""Webhook + email delivery for notification_service.py. Webhook uses httpx
(already the project's only HTTP client); email uses stdlib smtplib/email,
run via asyncio.to_thread since smtplib is blocking — no new dependency
needed (requirements.txt has no SMTP library, and this project deliberately
avoids adding dependencies where the standard library already covers it).
"""
import asyncio
import hashlib
import hmac
import json
import smtplib
from email.message import EmailMessage

import httpx

from app.config import settings
from app.models.notification import NotificationProblem, NotificationSettings
from app.services import notification_secret_store


class NotificationDeliveryError(Exception):
    pass


_LABELS = {
    "host_offline": "Host nicht erreichbar",
    "cert_expiry": "Zertifikat läuft ab",
    "rule_drift": "Regel-Drift im Cluster",
    "revert_failed": "Rollback fehlgeschlagen",
    "maintenance_unknown": "Wartungsergebnis unklar",
    "carp_split_brain": "CARP-Zustand kritisch",
    "nat_drift": "NAT-Drift im Cluster",
    "carp_auto_revert_failed": "CARP-Auto-Revert fehlgeschlagen",
}


def render_message(problem: NotificationProblem, transition: str) -> tuple[str, str]:
    """transition: "opened" | "resolved". Single source of copy for both
    delivery channels, so webhook and email never drift in wording."""
    label = _LABELS.get(problem.problem_type, problem.problem_type)
    if transition == "resolved":
        subject = f"OPNexus: {label} behoben"
    else:
        subject = f"OPNexus: {label}"

    lines = [subject, ""]
    for key, value in problem.context.items():
        if value in (None, "", []):
            continue
        lines.append(f"{key}: {value}")
    body = "\n".join(lines)
    return subject, body


async def send_webhook(settings_row: NotificationSettings, problem: NotificationProblem, transition: str) -> None:
    if not settings_row.webhook_url:
        raise NotificationDeliveryError("Keine Webhook-URL konfiguriert")
    subject, body = render_message(problem, transition)
    payload = {
        "problem_type": problem.problem_type,
        "transition": transition,
        "subject": subject,
        "message": body,
        "context": problem.context,
    }
    raw = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings_row.webhook_signing_secret_set:
        secret = notification_secret_store.read_secret("webhook_signing_secret")
        if secret:
            signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            headers["X-OPNexus-Signature"] = f"sha256={signature}"

    try:
        async with httpx.AsyncClient(timeout=settings.opnsense_timeout) as client:
            resp = await client.post(settings_row.webhook_url, content=raw, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise NotificationDeliveryError(f"Webhook-Zustellung fehlgeschlagen: {exc}") from exc


def _send_email_sync(settings_row: NotificationSettings, subject: str, body: str) -> None:
    password = notification_secret_store.read_secret("smtp_password")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings_row.smtp_from_address
    message["To"] = ", ".join(settings_row.smtp_to_addresses)
    message.set_content(body)

    timeout = settings.notification_smtp_timeout_seconds
    if settings_row.smtp_security == "ssl":
        client = smtplib.SMTP_SSL(settings_row.smtp_host, settings_row.smtp_port, timeout=timeout)
    else:
        client = smtplib.SMTP(settings_row.smtp_host, settings_row.smtp_port, timeout=timeout)
    try:
        if settings_row.smtp_security == "starttls":
            client.starttls()
        if settings_row.smtp_username and password:
            client.login(settings_row.smtp_username, password)
        client.send_message(message)
    finally:
        client.quit()


async def send_email(settings_row: NotificationSettings, subject: str, body: str) -> None:
    if not settings_row.smtp_host or not settings_row.smtp_from_address or not settings_row.smtp_to_addresses:
        raise NotificationDeliveryError("SMTP-Konfiguration unvollständig")
    try:
        await asyncio.to_thread(_send_email_sync, settings_row, subject, body)
    except (OSError, smtplib.SMTPException) as exc:
        raise NotificationDeliveryError(f"E-Mail-Zustellung fehlgeschlagen: {exc}") from exc
