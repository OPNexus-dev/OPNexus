"""Settings an admin changes in the web interface instead of the .env (Settings › Betrieb).

One registry for every tunable that is safe to change while OPNexus runs. The value in effect is, in this order:
1. the environment variable, if the spec names one and it is set to a non-empty value ("vom Server festgelegt": a
   scripted installation stays in control, the UI shows the value read-only);
2. the value saved in the web interface (table ``runtime_settings``; secrets in the notification secret store);
3. the built-in default (the attribute of ``app.config.settings``).

Reads are synchronous (``value(key)``) from a cache that every backend process refreshes every few seconds
(``refresh_loop``), so a leader and a standby agree within that time and hot paths (login, every firewall call) do
not touch the database. A save refreshes the cache of the saving process at once.

What deliberately stays in the .env: addresses and TLS of OPNexus itself (a wrong value there locks everyone out of
the very interface that would fix it), the database credentials, the first admin account, the container switches
(Compose profiles) and the escape hatches (ALLOW_INSECURE_*, OPNEXUS_RUN_AS_ROOT).
"""
import asyncio
import logging
import os
import ssl
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import SessionLocal
from app.models.runtime_setting import RuntimeSetting
from app.services import notification_secret_store, url_guard

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 15
_MAX_PEM = 65536


@dataclass(frozen=True)
class Spec:
    key: str
    group: str            # security | operation | monitoring | integrations (UI sections)
    kind: str             # int | float | secret_url | pem
    attr: str             # attribute of app.config.settings holding the default (and the env value, via pydantic)
    unit: str = ""        # shown next to the field: seconds | minutes | hours | days | count
    minimum: float | None = None
    maximum: float | None = None
    scale: int = 1        # stored/shown in `unit`; the code reads value * scale (days -> seconds)
    env: str | None = None


SPECS: list[Spec] = [
    # Security
    Spec("session_lifetime_days", "security", "int", "session_lifetime", "days", 1, 365, scale=86400),
    Spec("login_max_attempts", "security", "int", "login_max_attempts", "count", 3, 50),
    Spec("login_ip_max_attempts", "security", "int", "login_ip_max_attempts", "count", 5, 500),
    Spec("login_window_seconds", "security", "int", "login_window_seconds", "seconds", 60, 3600),
    Spec("login_lockout_seconds", "security", "int", "login_lockout_seconds", "seconds", 60, 86400),
    # Operation
    Spec("deploy_confirm_window_seconds", "operation", "int", "deploy_confirm_window_seconds", "seconds", 60, 1800),
    Spec("opnsense_timeout", "operation", "float", "opnsense_timeout", "seconds", 5, 120),
    Spec("migration_backup_keep", "operation", "int", "migration_backup_keep", "count", 1, 50),
    # Monitoring
    Spec("resource_poll_interval_seconds", "monitoring", "int", "resource_poll_interval_seconds", "seconds", 30, 3600),
    Spec("resource_sample_retention_days", "monitoring", "int", "resource_sample_retention_days", "days", 1, 365),
    Spec("rule_observe_interval_seconds", "monitoring", "int", "rule_observe_interval_seconds", "seconds", 60, 86400),
    # No env lock: Compose passes UPDATE_CHECK_INTERVAL_HOURS=12 when unset (it must: the backend before 1.34 cannot
    # parse an empty value, and a rollback runs that backend with the new compose file), so the variable cannot tell
    # "set by the admin" from "default". It stays the default; the web interface may override it.
    Spec("update_check_interval_hours", "monitoring", "int", "update_check_interval_hours", "hours", 1, 168),
    # Integrations
    Spec("heartbeat_url", "integrations", "secret_url", "heartbeat_url", env="HEARTBEAT_URL"),
    Spec("smtp_ca_pem", "integrations", "pem", "smtp_ca_file", env="SMTP_CA_FILE"),
    Spec("notification_smtp_timeout_seconds", "integrations", "float", "notification_smtp_timeout_seconds", "seconds",
         5, 120),
]
BY_KEY = {s.key: s for s in SPECS}

# Saved values (numbers as stored, in the spec's unit; secrets and PEM as text). Missing key = not saved.
_cache: dict[str, Any] = {}


class SettingError(ValueError):
    def __init__(self, key: str, reason: str):
        super().__init__(reason)
        self.key = key
        self.reason = reason


def _secret_name(key: str) -> str:
    return f"runtime-{key}"


def from_server(spec: Spec) -> bool:
    if spec.env and os.environ.get(spec.env, "").strip():
        return True
    # Text settings default to "": a non-empty value can only come from the server (env, parsed by pydantic).
    return spec.kind in ("secret_url", "pem") and bool(str(getattr(settings, spec.attr) or "").strip())


def _default(spec: Spec) -> Any:
    raw = getattr(settings, spec.attr)
    if spec.kind in ("int", "float") and spec.scale != 1:
        return type(raw)(raw / spec.scale) if spec.kind == "float" else int(raw // spec.scale)
    return raw


def shown(spec: Spec) -> tuple[Any, str]:
    """(value in the spec's unit, source): source is server | ui | default."""
    if from_server(spec):
        return _default(spec), "server"
    if spec.key in _cache:
        return _cache[spec.key], "ui"
    return _default(spec), "default"


def value(key: str) -> Any:
    """The value the code uses (numbers multiplied by the spec's scale)."""
    spec = BY_KEY[key]
    if from_server(spec) or spec.key not in _cache:
        return getattr(settings, spec.attr)  # tests monkeypatch settings; the env is already parsed into it
    raw = _cache[spec.key]
    if spec.kind == "int":
        return int(raw) * spec.scale
    if spec.kind == "float":
        return float(raw) * spec.scale
    return raw


# ── validation ───────────────────────────────────────────────────────────

def validate(spec: Spec, raw: Any) -> Any:
    if spec.kind in ("int", "float"):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise SettingError(spec.key, "Bitte eine Zahl angeben")
        if spec.kind == "int" and int(raw) != raw:
            raise SettingError(spec.key, "Bitte eine ganze Zahl angeben")
        number = int(raw) if spec.kind == "int" else float(raw)
        if (spec.minimum is not None and number < spec.minimum) or (spec.maximum is not None and number > spec.maximum):
            raise SettingError(spec.key, f"Erlaubt ist {spec.minimum:g} bis {spec.maximum:g}")
        return number
    if not isinstance(raw, str):
        raise SettingError(spec.key, "Bitte Text angeben")
    text = raw.strip()
    if spec.kind == "secret_url":
        try:
            url = url_guard.validate_url(text, max_length=2000)
        except ValueError as exc:
            raise SettingError(spec.key, str(exc)) from None
        if not url.startswith("https://"):
            raise SettingError(spec.key, "Die Adresse muss mit https:// beginnen")
        return url
    if spec.kind == "pem":
        if len(text) > _MAX_PEM or "-----BEGIN CERTIFICATE-----" not in text:
            raise SettingError(spec.key, "Bitte ein CA-Zertifikat im PEM-Format einfügen")
        try:
            ssl.create_default_context(cadata=text)
        except (ssl.SSLError, ValueError):
            raise SettingError(spec.key, "Das Zertifikat ist nicht lesbar") from None
        return text
    raise SettingError(spec.key, "Unbekannte Einstellung")


# ── storage ──────────────────────────────────────────────────────────────

async def load(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.scalars(select(RuntimeSetting))).all()
    fresh: dict[str, Any] = {}
    for row in rows:
        spec = BY_KEY.get(row.key)
        if spec is None:
            continue  # a setting a newer version knew about; keep the row, ignore it
        if spec.kind == "secret_url":
            secret = notification_secret_store.read_secret(_secret_name(spec.key))
            if secret:
                fresh[spec.key] = secret
        elif row.value is not None:
            fresh[spec.key] = row.value
    return fresh


async def refresh(db: AsyncSession | None = None) -> None:
    global _cache
    if db is not None:
        _cache = await load(db)
        return
    async with SessionLocal() as session:
        _cache = await load(session)


async def refresh_loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        try:
            await refresh()
        except Exception as exc:
            logger.warning("Einstellungen konnten nicht neu gelesen werden (%s)", type(exc).__name__)


async def save(db: AsyncSession, values: dict[str, Any], actor: str) -> list[str]:
    """Validates everything first, then writes; None resets a setting to its default. Returns the changed keys."""
    clean: dict[str, Any] = {}
    for key, raw in values.items():
        spec = BY_KEY.get(key)
        if spec is None:
            raise SettingError(key, "Unbekannte Einstellung")
        if from_server(spec):
            raise SettingError(key, f"Wird vom Server festgelegt ({spec.env} in der .env)")
        clean[key] = None if raw is None or (isinstance(raw, str) and not raw.strip()) else validate(spec, raw)
    for key, val in clean.items():
        spec = BY_KEY[key]
        if val is None:
            await db.execute(delete(RuntimeSetting).where(RuntimeSetting.key == key))
            if spec.kind == "secret_url":
                notification_secret_store.delete_secret(_secret_name(key))
            continue
        stored = val
        if spec.kind == "secret_url":
            notification_secret_store.save_secret(_secret_name(key), val)
            stored = None  # the database only knows that it is set
        await db.execute(
            insert(RuntimeSetting).values(key=key, value=stored, updated_by=actor)
            .on_conflict_do_update(index_elements=["key"], set_={"value": stored, "updated_by": actor})
        )
    await db.commit()
    await refresh(db)
    return sorted(clean)


def describe() -> list[dict]:
    out = []
    for spec in SPECS:
        val, source = shown(spec)
        item = {"key": spec.key, "group": spec.group, "kind": spec.kind, "unit": spec.unit,
                "min": spec.minimum, "max": spec.maximum, "default": None, "source": source,
                "env": spec.env if source == "server" else None}
        if spec.kind == "secret_url":
            item["value"] = None
            item["is_set"] = bool(val)
        elif spec.kind == "pem":
            item["value"] = val if source != "server" else None  # an env value is a path, not the certificate
            item["is_set"] = bool(val)
        else:
            item["value"] = val
            item["default"] = _default(spec)
        out.append(item)
    return out


# ── consumers ────────────────────────────────────────────────────────────

def smtp_ssl_context() -> ssl.SSLContext:
    """Trust for the mail server (and the TLS syslog of the audit export): the system store plus the private CA from
    SMTP_CA_FILE or the certificate pasted in the web interface."""
    spec = BY_KEY["smtp_ca_pem"]
    if from_server(spec):
        return ssl.create_default_context(cafile=settings.smtp_ca_file)
    pem = _cache.get(spec.key)
    return ssl.create_default_context(cadata=pem) if pem else ssl.create_default_context()
