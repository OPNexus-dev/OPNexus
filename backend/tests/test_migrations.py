"""Tests für app/migrate.py und die Alembic-Migrationen.

Jeder Test bekommt eine frische Scratch-Datenbank (neben der _test-DB), damit
Zustände wie "frisch", "Altinstallation vor Alembic" und "schon aktuell"
echt nachgestellt werden können, ohne die Test-DB der anderen Tests anzufassen.
"""
import asyncio

import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app import migrate
from app.config import settings
from app.database import Base

migrate.load_models()


@pytest_asyncio.fixture
async def scratch(monkeypatch, tmp_path):
    base_url = make_url(settings.database_url)
    name = f"{base_url.database}_migrations"
    admin = create_async_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base_url.set(database=name)
    engine = create_async_engine(url)
    monkeypatch.setattr(migrate, "engine", engine)
    monkeypatch.setattr(settings, "database_url", url.render_as_string(hide_password=False))
    monkeypatch.setattr(settings, "migration_backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "skip_migration_backup", True)
    yield engine
    await engine.dispose()
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


def _head() -> str:
    return ScriptDirectory.from_config(migrate._config()).get_current_head()


async def _version(engine) -> str | None:
    async with engine.connect() as conn:
        exists = (await conn.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL"))).scalar_one()
        if not exists:
            return None
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()


async def _schema_diff(engine) -> list:
    def diff(sync_conn):
        ctx = MigrationContext.configure(
            sync_conn, opts={"compare_type": True, "compare_server_default": True}
        )
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        return await conn.run_sync(diff)


async def test_fresh_database_gets_full_schema_matching_models(scratch):
    await migrate.run()
    assert await _version(scratch) == _head()
    # Wer ein Modell ändert, ohne eine Migration zu schreiben, scheitert hier.
    assert await _schema_diff(scratch) == []


async def test_audit_trigger_is_created_by_migrations(scratch):
    await migrate.run()
    async with scratch.begin() as conn:
        await conn.execute(text(
            "INSERT INTO audit_events (id, created_at, correlation_id, actor, kind, action, status, details) "
            "VALUES (gen_random_uuid(), now(), 'c', 'a', 'k', 'x', 's', '{}')"
        ))
    with pytest.raises(DBAPIError, match="append-only"):
        async with scratch.begin() as conn:
            await conn.execute(text("DELETE FROM audit_events"))


async def test_second_run_is_a_noop(scratch):
    await migrate.run()
    await migrate.run()
    assert await _version(scratch) == _head()


async def test_legacy_install_is_bridged_and_keeps_data(scratch):
    # Altinstallation: Tabellen von create_all(), aber (wie auf einem echten
    # 1.8.1-System) ohne Server-Defaults/Index aus 0002 und ohne Alembic.
    async with scratch.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE log_settings ALTER COLUMN max_size_gb DROP DEFAULT"))
        await conn.execute(text("DROP INDEX ix_opnsense_hosts_cluster_name"))
        # Spalte, die nur die eingefrorenen Legacy-Patches nachziehen:
        await conn.execute(text("ALTER TABLE admin_users DROP COLUMN totp_recovery_hashes"))
        await conn.execute(text(
            "INSERT INTO admin_users (id, username, password_hash) "
            "VALUES (gen_random_uuid(), 'legacy-admin', 'x')"
        ))
    assert await _version(scratch) is None
    assert await _schema_diff(scratch) != []  # der Diff-Check erkennt Abweichungen wirklich

    await migrate.run()

    assert await _version(scratch) == _head()
    assert await _schema_diff(scratch) == []
    async with scratch.connect() as conn:
        users = (await conn.execute(text("SELECT username, totp_recovery_hashes FROM admin_users"))).all()
    assert [tuple(u) for u in users] == [("legacy-admin", None)]


async def test_database_from_newer_version_is_refused(scratch):
    await migrate.run()
    async with scratch.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    with pytest.raises(RuntimeError, match="neueren Version"):
        await migrate.run()


async def test_concurrent_starts_migrate_exactly_once(scratch):
    await asyncio.gather(migrate.run(), migrate.run(), migrate.run())
    assert await _version(scratch) == _head()


async def test_legacy_migration_writes_pre_upgrade_backup(scratch, monkeypatch):
    monkeypatch.setattr(settings, "skip_migration_backup", False)
    async with scratch.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate.run()
    from pathlib import Path
    dumps = list(Path(settings.migration_backup_dir).glob("pre-upgrade-legacy-to-*.dump"))
    assert len(dumps) == 1 and dumps[0].stat().st_size > 0


async def test_pre_upgrade_backup_can_be_restored(scratch, monkeypatch):
    """Ein Backup, das sich nicht zurückspielen lässt, ist keins — genau das
    ging mit pg_dump 17 gegen Server 16 schief."""
    import subprocess
    from pathlib import Path

    monkeypatch.setattr(settings, "skip_migration_backup", False)
    async with scratch.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "INSERT INTO admin_users (id, username, password_hash) "
            "VALUES (gen_random_uuid(), 'must-survive', 'x')"
        ))
    await migrate.run()
    dump = next(Path(settings.migration_backup_dir).glob("pre-upgrade-*.dump"))

    url = make_url(settings.database_url)
    restore_name = f"{url.database}_restore"
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{restore_name}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{restore_name}"'))
    try:
        subprocess.run(
            ["pg_restore", "-h", url.host, "-p", str(url.port or 5432), "-U", url.username,
             "-d", restore_name, "--no-owner", "--exit-on-error", str(dump)],
            check=True, capture_output=True, text=True, env={**__import__("os").environ, "PGPASSWORD": url.password or ""},
        )
        restored = create_async_engine(url.set(database=restore_name))
        async with restored.connect() as conn:
            names = (await conn.execute(text("SELECT username FROM admin_users"))).scalars().all()
        await restored.dispose()
        assert names == ["must-survive"]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{restore_name}" WITH (FORCE)'))
        await admin.dispose()


async def test_pg_client_major_mismatch_aborts_before_migration(scratch, monkeypatch):
    monkeypatch.setattr(settings, "skip_migration_backup", False)
    monkeypatch.setattr(migrate, "_pg_dump_major", lambda: 99)
    async with scratch.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    with pytest.raises(RuntimeError, match="nicht zurückspielen"):
        await migrate.run()
    assert await _version(scratch) is None


async def test_failed_backup_aborts_migration(scratch, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "skip_migration_backup", False)
    monkeypatch.setattr(settings, "migration_backup_dir", str(blocker / "sub"))
    async with scratch.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    with pytest.raises(RuntimeError, match="NICHT migriert"):
        await migrate.run()
    assert await _version(scratch) is None


async def test_fresh_database_needs_no_backup(scratch, monkeypatch):
    monkeypatch.setattr(settings, "skip_migration_backup", False)
    await migrate.run()  # würde ohne Backup-Ziel scheitern, wenn eins verlangt würde
    from pathlib import Path
    assert not Path(settings.migration_backup_dir).exists()


async def _alembic(engine, action: str, revision: str) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: migrate._run_alembic(sync_conn, action, revision))


async def test_0020_upgrade_keeps_existing_settings_and_downgrade_is_clean(scratch):
    """1.23 -> 1.24 with a real settings row: the new alert columns get their defaults, the stored webhook
    and flags stay, the channel table appears; a downgrade removes exactly that again and re-upgrading works."""
    await _alembic(scratch, "upgrade", "0019")
    async with scratch.begin() as conn:
        # Every NOT NULL column of the 0019 table gets a value, so the row is what a real 1.23 install has.
        required = (await conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'notification_settings' "
            "AND is_nullable = 'NO' AND column_default IS NULL"
        ))).all()
        by_type = {"boolean": "true", "integer": "300", "json": "'[]'", "timestamp with time zone": "now()"}
        values = {name: by_type.get(kind, "'x'") for name, kind in required}
        values.update({"id": "1", "webhook_enabled": "true", "webhook_url": "'https://hooks.example/x'"})
        await conn.execute(text(
            f"INSERT INTO notification_settings ({', '.join(values)}) VALUES ({', '.join(values.values())})"
        ))
    await _alembic(scratch, "upgrade", "head")
    async with scratch.connect() as conn:
        row = (await conn.execute(text(
            "SELECT webhook_enabled, webhook_url, gateway_down_enabled, resource_threshold_enabled, service_down_enabled, "
            "disk_threshold_pct, load_per_cpu_threshold, alert_confirm_samples, resource_confirm_samples, service_watchlist "
            "FROM notification_settings"
        ))).one()
        assert tuple(row) == (True, "https://hooks.example/x", True, False, False, 90, 2.0, 3, 5, None)
        await conn.execute(text(
            "INSERT INTO notification_channels (id, name, channel_type, config, created_at, updated_at) "
            "VALUES (gen_random_uuid(), 'n', 'ntfy', '{\"topic\": \"t\"}', now(), now())"
        ))
        await conn.commit()
    assert await _schema_diff(scratch) == []

    await _alembic(scratch, "downgrade", "0019")
    async with scratch.connect() as conn:
        assert (await conn.execute(text("SELECT to_regclass('notification_channels')"))).scalar() is None
        columns = set((await conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'notification_settings'"
        ))).scalars().all())
        assert "gateway_down_enabled" not in columns and "service_watchlist" not in columns
        assert (await conn.execute(text("SELECT webhook_url FROM notification_settings"))).scalar() == "https://hooks.example/x"

    await _alembic(scratch, "upgrade", "head")
    assert await _version(scratch) == _head()
    assert await _schema_diff(scratch) == []


async def test_0025_adds_api_rights_flag_and_gateway_mute_list_and_is_idempotent(scratch):
    """1.28 -> 1.29: an existing settings row gets api_rights_enabled = true and an empty mute list; running the
    statements again (pre-Alembic bridge) changes nothing; a downgrade removes exactly the two columns."""
    await _alembic(scratch, "upgrade", "0024")
    async with scratch.begin() as conn:
        required = (await conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'notification_settings' "
            "AND is_nullable = 'NO' AND column_default IS NULL"
        ))).all()
        by_type = {"boolean": "true", "integer": "300", "json": "'[]'", "timestamp with time zone": "now()"}
        values = {name: by_type.get(kind, "'x'") for name, kind in required}
        values.update({"id": "1", "webhook_url": "'https://hooks.example/x'"})
        await conn.execute(text(
            f"INSERT INTO notification_settings ({', '.join(values)}) VALUES ({', '.join(values.values())})"
        ))
    await _alembic(scratch, "upgrade", "head")
    async with scratch.connect() as conn:
        row = (await conn.execute(text(
            "SELECT webhook_url, api_rights_enabled, gateway_alert_muted::text FROM notification_settings"
        ))).one()
        assert tuple(row) == ("https://hooks.example/x", True, "[]")
    assert await _schema_diff(scratch) == []

    await _alembic(scratch, "downgrade", "0024")
    async with scratch.connect() as conn:
        columns = set((await conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'notification_settings'"
        ))).scalars().all())
        assert "api_rights_enabled" not in columns and "gateway_alert_muted" not in columns
        assert (await conn.execute(text("SELECT webhook_url FROM notification_settings"))).scalar() == "https://hooks.example/x"

    await _alembic(scratch, "upgrade", "head")
    await _alembic(scratch, "downgrade", "0024")
    await _alembic(scratch, "upgrade", "head")  # IF NOT EXISTS / IF EXISTS: repeatable
    assert await _version(scratch) == _head()
    assert await _schema_diff(scratch) == []


async def test_0026_adds_account_ids_matches_existing_names_and_is_idempotent(scratch):
    """1.28 -> 1.29: existing plans/runs get the id of the account with that name (if it still exists), the others stay
    empty; the statements can run twice (pre-Alembic bridge); a downgrade drops exactly the two columns."""
    await _alembic(scratch, "upgrade", "0025")
    uid = "11111111-1111-1111-1111-111111111111"
    async with scratch.begin() as conn:
        await conn.execute(text("INSERT INTO admin_users (id, username, password_hash) VALUES (:id, 'still-here', 'x')"), {"id": uid})
        await conn.execute(text(
            "INSERT INTO rollout_plans (id, name, description, action, auto_start, gate_checks, compliance_mode, activated_by, "
            "created_by, created_at, updated_at) VALUES "
            "(gen_random_uuid(), 'p1', '', 'update', true, '{}', 'warn', 'still-here', 'x', now(), now()), "
            "(gen_random_uuid(), 'p2', '', 'update', true, '{}', 'warn', 'gone', 'x', now(), now()), "
            "(gen_random_uuid(), 'p3', '', 'update', false, '{}', 'warn', NULL, 'x', now(), now())"))
        await conn.execute(text(
            "INSERT INTO rollout_runs (id, plan_name, plan_snapshot, action, status, current_wave, started_by, trigger, "
            "progress, revision, created_at) VALUES "
            "(gen_random_uuid(), 'p1', '{}', 'update', 'succeeded', 0, 'still-here', 'manual', '{}', 0, now()), "
            "(gen_random_uuid(), 'p2', '{}', 'update', 'succeeded', 0, 'gone', 'manual', '{}', 0, now())"))
    await _alembic(scratch, "upgrade", "head")
    async with scratch.connect() as conn:
        plans = dict((await conn.execute(text("SELECT name, activated_by_id::text FROM rollout_plans"))).all())
        runs = dict((await conn.execute(text("SELECT started_by, started_by_id::text FROM rollout_runs"))).all())
    assert plans == {"p1": uid, "p2": None, "p3": None} and runs == {"still-here": uid, "gone": None}
    assert await _schema_diff(scratch) == []

    await _alembic(scratch, "downgrade", "0025")
    async with scratch.connect() as conn:
        for table, column in (("rollout_plans", "activated_by_id"), ("rollout_runs", "started_by_id")):
            names = set((await conn.execute(text(
                f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'"))).scalars().all())
            assert column not in names
        assert (await conn.execute(text("SELECT count(*) FROM rollout_plans"))).scalar() == 3
    await _alembic(scratch, "upgrade", "head")
    await _alembic(scratch, "downgrade", "0025")
    await _alembic(scratch, "upgrade", "head")
    assert await _version(scratch) == _head()
    assert await _schema_diff(scratch) == []


async def test_0027_creates_the_rule_history_tables_and_is_idempotent(scratch):
    """1.29 -> 1.30: two new tables, the downgrade drops them; running the upgrade over tables that a pre-Alembic
    bridge (create_all) already made changes nothing."""
    await _alembic(scratch, "upgrade", "0026")
    async with scratch.connect() as conn:
        tables = set((await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all())
    assert "live_rule_states" not in tables and "rule_change_events" not in tables

    await _alembic(scratch, "upgrade", "head")
    assert await _version(scratch) == _head()
    assert await _schema_diff(scratch) == []
    host = "11111111-1111-1111-1111-111111111111"
    async with scratch.begin() as conn:
        await conn.execute(text(
            "INSERT INTO opnsense_hosts (id, name, base_url, api_key, verify_tls, enabled, created_at) "
            "VALUES (:h, 'fw', 'https://fw.test', 'k', true, true, now())"), {"h": host})
        await conn.execute(text(
            "INSERT INTO rule_change_events (id, host_id, rule_uuid, seq, observed_at, kind, origin) "
            "VALUES (gen_random_uuid(), :h, 'r', 1, now(), 'added', 'external')"), {"h": host})
    with pytest.raises(DBAPIError):  # the same change can only be recorded once
        async with scratch.begin() as conn:
            await conn.execute(text(
                "INSERT INTO rule_change_events (id, host_id, rule_uuid, seq, observed_at, kind, origin) "
                "VALUES (gen_random_uuid(), :h, 'r', 1, now(), 'added', 'external')"), {"h": host})
    async with scratch.begin() as conn:  # deleting the host takes its history with it
        await conn.execute(text("DELETE FROM opnsense_hosts"))
        assert (await conn.execute(text("SELECT count(*) FROM rule_change_events"))).scalar() == 0

    await _alembic(scratch, "downgrade", "0026")
    async with scratch.connect() as conn:
        tables = set((await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all())
    assert "live_rule_states" not in tables and "rule_change_events" not in tables
    await _alembic(scratch, "upgrade", "head")
    await _alembic(scratch, "upgrade", "head")  # second run: nothing to do
    assert await _version(scratch) == _head() and await _schema_diff(scratch) == []


async def test_0028_adds_the_event_window_and_is_idempotent(scratch):
    await _alembic(scratch, "upgrade", "0027")
    async with scratch.connect() as conn:
        cols = set((await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'rule_change_events'"))).scalars().all())
    assert "window_start" not in cols
    await _alembic(scratch, "upgrade", "head")
    assert await _version(scratch) == _head() == "0028" and await _schema_diff(scratch) == []
    await _alembic(scratch, "downgrade", "0027")
    async with scratch.connect() as conn:
        cols = set((await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'rule_change_events'"))).scalars().all())
    assert "window_start" not in cols
    await _alembic(scratch, "upgrade", "head")
    await _alembic(scratch, "upgrade", "head")
    assert await _version(scratch) == _head() and await _schema_diff(scratch) == []
