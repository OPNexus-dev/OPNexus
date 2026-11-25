"""Language of the notification texts (e-mail, webhook "subject"/"message", ntfy, Gotify, Telegram, Teams, Matrix).

German is the source language of the texts the checks produce. English is produced here from lookup tables, so the
checks stay as they are and the machine-readable parts stay stable:

* the webhook's ``context`` object keeps its (German) keys and values — receivers parse it;
* only the human text changes: subject, message body, the key names printed in the body and the sentences in it.

Anything the tables do not know (prose that comes from other services, e.g. the reason a rollout paused) is passed
through unchanged, so a message is never lost because of a missing translation. ``tests/test_notification_i18n.py``
fails when a hint literal in the checks has no English text.
"""
import re
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

LANGUAGES = ("de", "en")
DEFAULT_LANGUAGE = "de"  # what render_message uses when nobody set a language (and what every message used to be)

_current: ContextVar[str] = ContextVar("notification_language", default=DEFAULT_LANGUAGE)


def current() -> str:
    return _current.get()


def set_current(language: str):
    """Returns the token for ``reset_current``. Asyncio tasks copy the context when they are created, so a language set
    before the delivery tasks are started is seen by all of them and by nothing else."""
    return _current.set(language if language in LANGUAGES else DEFAULT_LANGUAGE)


def reset_current(token) -> None:
    _current.reset(token)


async def effective_language(db: AsyncSession, configured: str | None) -> str:
    """The setting wins ("de"/"en"). Automatic (NULL): the language the oldest administrator account saved —
    that is what an existing installation has been reading all along — and English if nobody ever chose."""
    if configured in LANGUAGES:
        return configured
    from app.models.preferences import UserPreferences
    from app.models.user import AdminUser
    row = await db.scalar(
        select(UserPreferences.language)
        .join(AdminUser, AdminUser.id == UserPreferences.user_id)
        .where(AdminUser.role == "admin")
        .order_by(AdminUser.created_at)
        .limit(1)
    )
    return row if row in LANGUAGES else "en"


LABELS_EN = {
    "host_offline": "Host not reachable",
    "cert_expiry": "Certificate expiring",
    "rule_drift": "Rule drift in the cluster",
    "revert_failed": "Rollback failed",
    "maintenance_unknown": "Maintenance result unclear",
    "carp_split_brain": "CARP state critical",
    "nat_drift": "NAT drift in the cluster",
    "network_drift": "Network deviation in the cluster",
    "carp_auto_revert_failed": "CARP auto-revert failed",
    "scheduled_maintenance_failed": "Scheduled maintenance not executed",
    "backup_stale": "No current backup",
    "backup_offsite_failed": "Offsite backup failed",
    "compliance_finding": "Security finding",
    "gateway_down": "Gateway disturbed",
    "resource_threshold": "Resource threshold exceeded",
    "service_down": "Service down",
    "api_rights_missing": "API privileges missing",
    "server_disk": "OPNexus server disk almost full",
    "rollout_paused": "Rollout stopped",
    "rollout_start_failed": "Rollout not started automatically",
    "ha_standby_down": "High availability: standby is not replicating",
    "ha_replication_lag": "High availability: replication lag",
    "ha_slot_retention": "High availability: replication slot",
    "ha_sync_stale": "High availability: secrets sync outdated",
    "ha_split_brain": "High availability: two primaries (split brain)",
    "ha_agent_down": "High availability: agent not responding",
    "test": "Test notification",
}

RESOLVED_EN = "resolved"

# Names of the lines in the message body (the context keys). Keys that are already English stay as they are.
KEYS_EN = {
    "hinweis": "hint", "knoten": "node", "zustand": "state", "grund": "reason", "seit": "since",
    "schwelle_prozent": "threshold_percent", "schwelle": "threshold", "ressource": "resource", "dauer": "duration",
    "zurueckgehalten_mb": "retained_mb", "obergrenze_mb": "limit_mb", "zielversion": "target_version",
    "wert_prozent": "value_percent", "wert": "value", "welle": "wave", "verzug_s": "lag_s", "verzug_mb": "lag_mb",
    "termin": "scheduled_for", "schweregrad": "severity", "rolle_partner": "partner_role", "regeln": "rules",
    "paketverlust_prozent": "packet_loss_percent", "latenz_ms": "latency_ms", "letzter_abgleich": "last_sync",
    "lesezugriff": "read_access", "endpunkt": "endpoint", "fehlendes_recht": "missing_privilege",
    "gesamt_gb": "total_gb",
}

# Whole values (hints, states, labels, severities).
VALUES_EN = {
    "Dies ist eine Testbenachrichtigung von OPNexus.": "This is a test notification from OPNexus.",
    # rule order / cleanup
    "Reihenfolge: Das automatische Zurücksetzen einer verschobenen Regel ist fehlgeschlagen. Die Reihenfolge auf der Firewall prüfen und in OPNexus erneut zurücknehmen.":
        "Order: automatically resetting a moved rule failed. Check the order on the firewall and revert it again in OPNexus.",
    "Bereinigung: Die Regel konnte nicht automatisch zurückgesetzt werden und ist möglicherweise noch geändert. Auf der Firewall prüfen und in OPNexus „Erneut zurücknehmen“ wählen.":
        "Cleanup: the rule could not be reset automatically and may still be changed. Check the firewall and choose \"Retry reset\" in OPNexus.",
    "Bereinigung: Das automatische Zurücknehmen ist fehlgeschlagen. Zustand der Firewall prüfen.":
        "Cleanup: the automatic reset failed. Check the state of the firewall.",
    "Bereinigung nur teilweise ausgeführt (oder das Anwenden ist fehlgeschlagen). Bestätigen ist gesperrt; jetzt zurücknehmen oder die automatische Rücknahme am Fristende abwarten.":
        "Cleanup only partly executed (or applying failed). Confirming is blocked; reset now or wait for the automatic reset at the end of the window.",
    "Bereinigung: Der Ausgang beim Löschen/Wiederherstellen ist unklar. Mit „Erneut prüfen“ nachlesen.":
        "Cleanup: the outcome of deleting/restoring is unclear. Use \"Check again\" to find out.",
    # server disk
    "Platz schaffen (z. B. alte Images mit 'docker image prune', Log-Obergrenze unter Einstellungen › Logs senken) oder die Platte vergrößern. Läuft sie voll, bleibt die Datenbank stehen.":
        "Free up space (e.g. old images with 'docker image prune', lower the log limit under Settings › Logs) or enlarge the disk. If it fills up, the database stops.",
    # services
    "Dienst läuft nicht mehr; absichtlich gestoppt? Dann aus der Überwachungsliste nehmen.":
        "The service is no longer running; stopped on purpose? Then remove it from the watch list.",
    # high availability
    "Der Standby streamt nicht mehr: 'opnexus ha status' zeigt, warum. Nach einem Wechsel den alten Primär mit 'opnexus ha resync' zurückholen.":
        "The standby is no longer streaming: 'opnexus ha status' shows why. After a switch, bring the old primary back with 'opnexus ha resync'.",
    "Bei einem Wechsel in dieser Lücke gehen Änderungen verloren. Netz und Platte des Standbys prüfen.":
        "A switch during this gap loses changes. Check the standby's network and disk.",
    "Firewall-Zugänge und 2FA-Geheimnisse neuer Objekte fehlen am Standby. Agent (Updater-Container, Log ha-timer.log) bzw. Cron und SSH prüfen ('opnexus ha sync' von Hand).":
        "Firewall credentials and 2FA secrets of new objects are missing on the standby. Check the agent (updater container, log ha-timer.log) or cron and SSH ('opnexus ha sync' by hand).",
    "Ohne den Agenten (Updater-Container) gehen Übernahme im Browser, Geheimnis-Abgleich und VIP-Nachführung nicht. 'docker compose ps updater ha-gate' prüfen, Port 8445 zwischen den Knoten.":
        "Without the agent (updater container) takeover in the browser, secrets sync and VIP follow-up do not work. Check 'docker compose ps updater ha-gate', port 8445 between the nodes.",
    "Der Partner läuft ohne Fencing als zweiter Primär; sein Wächter hat die Hintergrunddienste gestoppt. Partner anhalten und mit 'opnexus ha resync' neu aufbauen, bevor Änderungen verloren gehen.":
        "The partner runs as a second primary without fencing; its watchdog has stopped the background services. Stop the partner and rebuild it with 'opnexus ha resync' before changes are lost.",
    "Standby neu aufbauen: am Standby 'opnexus ha resync'.": "Rebuild the standby: run 'opnexus ha resync' on the standby.",
    "Der Standby holt WAL nicht ab. Bei Überschreiten der Obergrenze geht der Slot verloren und der Standby muss neu aufgebaut werden.":
        "The standby is not fetching WAL. If the limit is exceeded the slot is lost and the standby has to be rebuilt.",
    "kein Walsender": "no WAL sender",
    "fehlt": "missing",
    "verloren (Obergrenze überschritten)": "lost (limit exceeded)",
    # states and labels
    "fehlt auf einem Knoten": "missing on a node",
    "Einstellungen weichen ab": "settings differ",
    "keine aktuellen Messwerte": "no current measurements",
    "Gateway offline": "Gateway offline",
    "Arbeitsspeicher": "Memory",
    "CPU-Last (pro Thread)": "CPU load (per thread)",
    "kritisch": "critical", "hoch": "high", "mittel": "medium", "niedrig": "low", "Hinweis": "note",
}

# Parts inside longer values (applied in order).
_SUBSTITUTIONS_EN = [
    (re.compile(r"^Festplatte "), "Disk "),
    (re.compile(r"\bPaketverlust\b"), "Packet loss"),
    (re.compile(r"\bLatenz\b"), "Latency"),
    (re.compile(r'^Dem API-Benutzer fehlt das Recht, (.+) zu lesen \(OPNsense-Berechtigung "(.+)"\)\. Alarme, die darauf beruhen, können nicht auslösen\.$'),
     r'The API user lacks the privilege to read \1 (OPNsense privilege "\2"). Alarms based on it cannot fire.'),
]


def label(problem_type: str, language: str, german_labels: dict) -> str:
    if language == "en":
        return LABELS_EN.get(problem_type) or german_labels.get(problem_type, problem_type)
    return german_labels.get(problem_type, problem_type)


def key_name(key: str, language: str) -> str:
    return KEYS_EN.get(key, key) if language == "en" else key


def value_text(value, language: str):
    """The text of one line's value; non-text values (numbers, lists …) are not touched."""
    if language != "en" or not isinstance(value, str):
        return value
    if value in VALUES_EN:
        return VALUES_EN[value]
    result = value
    for pattern, replacement in _SUBSTITUTIONS_EN:
        result = pattern.sub(replacement, result)
    return result
