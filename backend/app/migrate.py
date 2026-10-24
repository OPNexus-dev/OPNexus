"""Schema-Migrationen beim Start (Alembic, in-process).

Ablauf von `run()` — bei jedem Backend-Start:

1. Session-weiten Postgres-Advisory-Lock nehmen. Starten zwei Instanzen
   gleichzeitig, migriert nur eine, die andere wartet und sieht dann "schon
   aktuell".
2. Zustand der Datenbank bestimmen:
   - **frisch** (keine Tabellen): `alembic upgrade head` baut alles auf.
   - **Altinstallation** (Tabellen, aber keine `alembic_version`; alles bis
     1.8.x): Sicherheits-Backup, dann `create_all()` + die eingefrorenen
     Legacy-Patches, auf Revision 0001 stempeln, `upgrade head`.
   - **Alembic-Installation**: nur wenn die Revision hinter `head` liegt,
     Sicherheits-Backup und `upgrade head`.
3. Lock freigeben.

Das Backup vor einer Migration ist Pflicht: schlägt `pg_dump` fehl, wird
nicht migriert und der Start bricht mit klarer Meldung ab. Wer das bewusst
nicht will, setzt SKIP_MIGRATION_BACKUP=true.

Eine Datenbank, die schon von einer NEUEREN Version migriert wurde (unbekannte
Revision), wird nicht angefasst — der Start bricht ab, statt mit einem Schema
zu laufen, das der Code nicht kennt. Rollback = das Pre-Upgrade-Backup
einspielen oder die neuere Version wieder starten.
"""
import asyncio
import importlib
import logging
import os
import pkgutil
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection

import app.models
from app import runtime_settings
from app import legacy_schema_patches
from app.config import settings
from app.database import Base, engine

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASELINE_REVISION = "0001"
# "OPNX" — beliebige, aber feste Zahl für den Advisory-Lock.
_LOCK_KEY = 0x4F504E58
# Hochvolumige, verzichtbare Betriebsdaten: nur das Schema sichern, sonst
# wird das Backup selbst zum Platzproblem (siehe docker-compose.yml,
# postgres-backup).
_BACKUP_SKIP_DATA = ("firewall_log_entries", "host_resource_samples")
_BACKUP_PREFIX = "pre-upgrade-"
_BACKUP_TIMEOUT_SECONDS = 1800


def load_models() -> None:
    """Jedes Modell-Modul importieren, sonst fehlen Tabellen in Base.metadata
    (app/models/__init__.py importiert nicht alle)."""
    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{module.name}")


def _config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


def _run_alembic(connection, action: str, revision: str) -> None:
    """Alembic-Kommando auf einer bereits offenen (sync-)Verbindung — env.py
    nimmt sie aus config.attributes, damit alles in EINER Transaktion läuft."""
    cfg = _config()
    cfg.attributes["connection"] = connection
    getattr(command, action)(cfg, revision)


async def _database_state(conn: AsyncConnection) -> tuple[bool, str | None]:
    """(hat_app_tabellen, aktuelle_alembic_revision oder None)."""
    has_tables = (
        await conn.execute(text("SELECT to_regclass('admin_users') IS NOT NULL"))
    ).scalar_one()
    has_version = (
        await conn.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
    ).scalar_one()
    current = None
    if has_version:
        current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    return bool(has_tables), current


def _dump_to(target: Path) -> None:
    url = make_url(settings.database_url)
    partial = target.with_suffix(".partial")
    cmd = [
        "pg_dump",
        "-h", url.host or "localhost",
        "-p", str(url.port or 5432),
        "-U", url.username or "",
        "-d", url.database or "",
        "-F", "c",
        *(f"--exclude-table-data={table}" for table in _BACKUP_SKIP_DATA),
        "-f", str(partial),
    ]
    try:
        subprocess.run(
            cmd,
            env={**os.environ, "PGPASSWORD": url.password or ""},
            check=True,
            capture_output=True,
            text=True,
            timeout=_BACKUP_TIMEOUT_SECONDS,
        )
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def _pg_dump_major() -> int:
    out = subprocess.run(["pg_dump", "--version"], check=True, capture_output=True, text=True).stdout
    match = re.search(r"PostgreSQL\) (\d+)", out)
    if not match:
        raise RuntimeError(f"pg_dump-Version nicht erkennbar: {out.strip()!r}")
    return int(match.group(1))


async def _ensure_pg_client_matches_server() -> None:
    """Ein pg_dump anderer Hauptversion als der Server schreibt Backups, die sich
    nicht zurückspielen lassen (z. B. Client 17 → Server 16: `SET
    transaction_timeout`). Lieber vor der Migration abbrechen als ein
    Backup anlegen, auf das man sich nicht verlassen kann."""
    async with engine.connect() as conn:
        server_major = int((await conn.execute(text("SHOW server_version_num"))).scalar_one()) // 10000
    client_major = await asyncio.to_thread(_pg_dump_major)
    if client_major != server_major:
        raise RuntimeError(
            f"pg_dump im Backend-Image ist Version {client_major}, der Postgres-Server Version {server_major}. "
            "Ein solches Backup ließe sich nicht zurückspielen. backend/scripts/install_pg_client.sh und "
            "das postgres-Image in docker-compose.yml müssen dieselbe Hauptversion haben. Die Datenbank "
            "wurde NICHT migriert (SKIP_MIGRATION_BACKUP=true überspringt Backup und diese Prüfung bewusst)."
        )


def _prune_backups(directory: Path) -> None:
    dumps = sorted(directory.glob(f"{_BACKUP_PREFIX}*.dump"), key=lambda p: p.stat().st_mtime)
    for old in dumps[: max(0, len(dumps) - runtime_settings.value("migration_backup_keep"))]:
        old.unlink(missing_ok=True)


async def _backup_before_migration(from_revision: str, to_revision: str) -> None:
    if settings.skip_migration_backup:
        logger.warning("Pre-Upgrade-Backup übersprungen (SKIP_MIGRATION_BACKUP=true)")
        return
    directory = Path(settings.migration_backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = directory / f"{_BACKUP_PREFIX}{from_revision}-to-{to_revision}-{stamp}.dump"
    logger.info("Schema-Migration %s → %s: sichere Datenbank nach %s", from_revision, to_revision, target)
    await _ensure_pg_client_matches_server()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_dump_to, target)
        _prune_backups(directory)
    except Exception as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else repr(exc)
        raise RuntimeError(
            f"Pre-Upgrade-Backup nach {target} fehlgeschlagen ({detail}). Die Datenbank wurde "
            "NICHT migriert. Backup-Ziel prüfen oder mit SKIP_MIGRATION_BACKUP=true bewusst überspringen."
        ) from exc


async def _migrate() -> None:
    script = ScriptDirectory.from_config(_config())
    head = script.get_current_head()

    async with engine.connect() as conn:
        has_tables, current = await _database_state(conn)

    if current is not None:
        try:
            script.get_revision(current)
        except Exception as exc:
            raise RuntimeError(
                f"Die Datenbank hat Schema-Revision {current}, die diese OPNexus-Version nicht kennt — "
                "sie wurde von einer neueren Version migriert. Die neuere Version wieder starten oder "
                "das Pre-Upgrade-Backup einspielen."
            ) from exc

    if current is None and not has_tables:
        logger.info("Leere Datenbank: lege Schema an (Revision %s)", head)
        async with engine.begin() as conn:
            await conn.run_sync(_run_alembic, "upgrade", "head")
        return

    if current is None:
        await _backup_before_migration("legacy", head)
        logger.info("Datenbank aus der Zeit vor Alembic erkannt: Legacy-Brücke, dann Migration auf %s", head)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await legacy_schema_patches.apply(conn)
            await conn.run_sync(_run_alembic, "stamp", BASELINE_REVISION)
            await conn.run_sync(_run_alembic, "upgrade", "head")
        return

    if current == head:
        logger.info("Datenbankschema ist aktuell (Revision %s)", head)
        return

    try:  # the kept-backups count may be set in the web interface; an old schema without the table keeps the default
        await runtime_settings.refresh()
    except Exception:
        pass
    await _backup_before_migration(current, head)
    logger.info("Migriere Datenbankschema %s → %s", current, head)
    async with engine.begin() as conn:
        await conn.run_sync(_run_alembic, "upgrade", "head")


async def run() -> None:
    load_models()
    async with engine.connect() as lock_conn:
        await lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _LOCK_KEY})
        try:
            await _migrate()
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
            await lock_conn.commit()
