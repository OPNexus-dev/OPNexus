"""Extra notification channels: ntfy, Gotify, Telegram, Microsoft Teams, Matrix.

They sit beside SMTP and the generic webhook (notification_delivery.py) and reuse its message
text. Each channel is a row in ``notification_channels`` (see models/notification.py); the
credential never touches the database: it is stored through notification_secret_store as
``channel_<id>`` (mode 0600 file) and read only at send time. Where the credential is part of the
URL (a Telegram bot token, the whole Teams webhook URL) the URL is built per request, and the
HTTP request log is muted while it is in flight (``url_guard.muted``), so neither the logs nor an
error message ever contain it.

API formats, checked against the vendors' documentation:

* ntfy      — JSON publish: POST <server>/ with {"topic", "title", "message", "priority", "tags"}
              (docs.ntfy.sh/publish, "Publish as JSON"); optional ``Authorization: Bearer <token>``.
* Gotify    — POST <server>/message, header ``X-Gotify-Key: <app token>`` (keeps the token out of
              the URL), JSON {"title", "message", "priority"} (gotify.net/docs/pushmsg).
* Telegram  — POST <api>/bot<token>/sendMessage, JSON {"chat_id", "text"}; text limit 4096
              characters; answers {"ok": true, ...} (core.telegram.org/bots/api#sendmessage).
* Teams     — POST to the (Workflows / incoming) webhook URL with a message envelope carrying an
              Adaptive Card (learn.microsoft.com ... add-incoming-webhook); the URL is the secret.
* Matrix    — PUT <homeserver>/_matrix/client/v3/rooms/<roomId>/send/m.room.message/<txnId> with
              ``Authorization: Bearer <access token>``, JSON {"msgtype": "m.text", "body"}; a fresh
              transaction id per message (Matrix client-server API, "Sending events to a room").
"""
import re
import uuid
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.notification import CHANNEL_TYPES, NotificationChannel, NotificationProblem
from app.services import notification_delivery, notification_secret_store, url_guard
from app.services.notification_delivery import NotificationDeliveryError

TELEGRAM_LIMIT = 4096
TEAMS_LIMIT = 20000
_NTFY_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MATRIX_ROOM = re.compile(r"^![^\s:]+:[^\s/]+$")

# type -> (required config keys, secret required?)
_SPEC = {
    "ntfy": ({"topic"}, False),
    "gotify": ({"server_url"}, True),
    "telegram": ({"chat_id"}, True),
    "teams": (set(), True),
    "matrix": ({"homeserver_url", "room_id"}, True),
}
assert set(_SPEC) == set(CHANNEL_TYPES)


def secret_name(channel_id) -> str:
    return f"channel_{channel_id}"


def clean_config(channel_type: str, config: dict) -> dict:
    """Validates and normalises the non-secret settings of a channel. Raises ValueError (German reason)."""
    if channel_type not in _SPEC:
        raise ValueError("Unbekannter Kanaltyp")
    config = dict(config or {})
    out: dict = {}

    def url(key: str, default: str | None = None) -> str | None:
        raw = str(config.get(key) or "").strip() or default
        if raw is None:
            return None
        return url_guard.validate_url(raw).rstrip("/")

    def integer(key: str, low: int, high: int, default: int) -> int:
        value = config.get(key)
        if value in (None, ""):
            return default
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: ganze Zahl erwartet") from None
        if not low <= number <= high:
            raise ValueError(f"{key}: Wert zwischen {low} und {high} erwartet")
        return number

    if channel_type == "ntfy":
        out["server_url"] = url("server_url", "https://ntfy.sh")
        topic = str(config.get("topic") or "").strip()
        if not _NTFY_TOPIC.match(topic):
            raise ValueError("Thema: 1–64 Zeichen aus Buchstaben, Ziffern, _ und -")
        out["topic"] = topic
        out["priority"] = integer("priority", 1, 5, 4)
    elif channel_type == "gotify":
        out["server_url"] = url("server_url")
        if not out["server_url"]:
            raise ValueError("Serveradresse fehlt")
        out["priority"] = integer("priority", 0, 10, 5)
    elif channel_type == "telegram":
        chat_id = str(config.get("chat_id") or "").strip()
        if not re.match(r"^(-?\d{1,20}|@[A-Za-z0-9_]{4,64})$", chat_id):
            raise ValueError("Chat-ID: Zahl (z. B. -1001234567890) oder @Kanalname")
        out["chat_id"] = chat_id
        out["api_base"] = url("api_base", "https://api.telegram.org")
    elif channel_type == "matrix":
        out["homeserver_url"] = url("homeserver_url")
        if not out["homeserver_url"]:
            raise ValueError("Homeserver-Adresse fehlt")
        room = str(config.get("room_id") or "").strip()
        if not _MATRIX_ROOM.match(room):
            raise ValueError("Raum-ID: Form !abc123:example.org (nicht der Alias #raum:…)")
        out["room_id"] = room
    return out


def clean_secret(channel_type: str, secret: str) -> str:
    value = (secret or "").strip()
    if not value:
        raise ValueError("Zugangsdaten fehlen")
    if channel_type == "teams":
        value = url_guard.validate_url(value, max_length=2000)
        if not value.startswith("https://"):
            raise ValueError("Die Teams-Webhook-Adresse muss mit https:// beginnen")
    elif any(c.isspace() for c in value) or len(value) > 500:
        raise ValueError("Token: keine Leerzeichen, höchstens 500 Zeichen")
    return value


def missing_requirements(channel: NotificationChannel) -> str | None:
    required, needs_secret = _SPEC[channel.channel_type]
    if any(not channel.config.get(k) for k in required):
        return "Konfiguration unvollständig"
    if needs_secret and not channel.secret_set:
        return "Zugangsdaten fehlen"
    return None


# ── Message formatting (one function per channel, pure — unit-testable) ───────────

def _parts(problem: NotificationProblem, transition: str) -> tuple[str, str]:
    """(title, details) — the title repeated at the top of render_message's body is dropped."""
    subject, body = notification_delivery.render_message(problem, transition)
    details = body.split("\n", 2)[2] if body.count("\n") >= 2 else ""
    return subject, details.strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_ntfy(config: dict, problem, transition: str) -> dict:
    title, details = _parts(problem, transition)
    resolved = transition == "resolved"
    return {
        "topic": config["topic"], "title": title, "message": details or title,
        "priority": 3 if resolved else int(config.get("priority", 4)),
        "tags": ["white_check_mark"] if resolved else ["warning"],
    }


def format_gotify(config: dict, problem, transition: str) -> dict:
    title, details = _parts(problem, transition)
    return {"title": title, "message": details or title, "priority": 1 if transition == "resolved" else int(config.get("priority", 5))}


def format_telegram(config: dict, problem, transition: str) -> dict:
    title, details = _parts(problem, transition)
    return {
        "chat_id": config["chat_id"], "text": _clip(f"{title}\n\n{details}".strip(), TELEGRAM_LIMIT),
        "disable_web_page_preview": True,
    }


def format_teams(config: dict, problem, transition: str) -> dict:
    title, details = _parts(problem, transition)
    body: list[dict] = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    if details:
        body.append({"type": "TextBlock", "text": _clip(details, TEAMS_LIMIT), "wrap": True})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard", "version": "1.4", "body": body,
            },
        }],
    }


def format_matrix(config: dict, problem, transition: str) -> dict:
    title, details = _parts(problem, transition)
    return {"msgtype": "m.text", "body": f"{title}\n\n{details}".strip()}


# ── Sending ───────────────────────────────────────────────────────────────────────

def _request_for(channel: NotificationChannel, secret: str, problem, transition: str) -> tuple[str, str, dict, dict, str | None]:
    """(method, url, headers, json body, expected "ok" check key)."""
    cfg, kind = channel.config, channel.channel_type
    if kind == "ntfy":
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        return "POST", cfg["server_url"].rstrip("/") + "/", headers, format_ntfy(cfg, problem, transition), None
    if kind == "gotify":
        return "POST", cfg["server_url"].rstrip("/") + "/message", {"X-Gotify-Key": secret}, format_gotify(cfg, problem, transition), None
    if kind == "telegram":
        return "POST", f"{cfg['api_base'].rstrip('/')}/bot{secret}/sendMessage", {}, format_telegram(cfg, problem, transition), "ok"
    if kind == "teams":
        return "POST", secret, {}, format_teams(cfg, problem, transition), None
    room = quote(cfg["room_id"], safe="")
    url = f"{cfg['homeserver_url'].rstrip('/')}/_matrix/client/v3/rooms/{room}/send/m.room.message/{uuid.uuid4().hex}"
    return "PUT", url, {"Authorization": f"Bearer {secret}"}, format_matrix(cfg, problem, transition), None


async def send(channel: NotificationChannel, problem: NotificationProblem, transition: str) -> None:
    problem_missing = missing_requirements(channel)
    if problem_missing:
        raise NotificationDeliveryError(f"Kanal „{channel.name}“: {problem_missing}")
    secret = notification_secret_store.read_secret(secret_name(channel.id)) or ""
    if channel.secret_set and not secret:
        raise NotificationDeliveryError(f"Kanal „{channel.name}“: Zugangsdaten nicht lesbar – bitte neu setzen")
    method, url, headers, payload, ok_key = _request_for(channel, secret, problem, transition)
    label = f"Kanal „{channel.name}“ ({channel.channel_type})"
    with url_guard.muted():
        try:
            url_guard.validate_url(url, max_length=2100)
            await url_guard.ensure_resolved_safe(url)
        except ValueError as exc:
            raise NotificationDeliveryError(f"{label}: {exc}") from None
        try:
            async with url_guard.guarded_client(timeout=settings.opnsense_timeout, follow_redirects=False) as client:
                resp = await client.request(method, url, json=payload, headers=headers)
                resp.raise_for_status()
                if ok_key and not (resp.json() or {}).get(ok_key):
                    raise NotificationDeliveryError(f"{label}: Zustellung abgelehnt")
        except httpx.HTTPError as exc:
            # str(exc) names the full URL, which for Telegram/Teams is the credential itself.
            reason = url_guard.failure_reason(exc)
            raise NotificationDeliveryError(f"{label}: Zustellung fehlgeschlagen ({reason})") from None
        except ValueError:
            raise NotificationDeliveryError(f"{label}: Antwort nicht lesbar") from None


async def send_test(channel: NotificationChannel) -> None:
    fake = NotificationProblem(
        problem_type="test", dedup_key="test",
        context={"message": "Dies ist eine Testbenachrichtigung von OPNexus."},
    )
    await send(channel, fake, "opened")


async def enabled_channels(db: AsyncSession) -> list[NotificationChannel]:
    rows = await db.scalars(select(NotificationChannel).where(NotificationChannel.enabled.is_(True)).order_by(NotificationChannel.name))
    return list(rows.all())
