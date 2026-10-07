import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.host import OpnsenseHost
from app.models.log_cursor import HostLogCursor
from app.models.log_entry import FirewallLogEntry
from app.services import log_ingest_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(digest: str, ts: datetime, action: str = "pass", src: str = "10.0.0.1", dst: str = "10.0.0.2", dstport: str = "443") -> dict:
    return {
        "__digest__": digest,
        "__timestamp__": ts.isoformat(),
        "action": action,
        "interface": "wan",
        "dir": "in",
        "protoname": "tcp",
        "src": src,
        "srcport": "5000",
        "dst": dst,
        "dstport": dstport,
        "label": "test-rule",
    }


async def _create_host(client, name: str, base_url: str) -> dict:
    resp = await client.post(
        "/api/hosts",
        json={"name": name, "base_url": base_url, "api_key": "key", "api_secret": "secret", "verify_tls": False},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── poll_host: first fetch, incremental cursor, dedupe ──────────────────

@pytest.mark.asyncio
async def test_poll_host_first_fetch_has_no_digest_param(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    host_id = uuid.UUID(host["id"])
    ts = _now()
    route = respx_mock.get(url__regex=r".*/diagnostics/firewall/log").mock(
        return_value=httpx.Response(200, json={"rows": [_row("dig-2", ts), _row("dig-1", ts - timedelta(seconds=5))]})
    )

    async with SessionLocal() as db:
        h = await db.get(OpnsenseHost, host_id)
        inserted = await log_ingest_service.poll_host(db, h)
        await db.commit()

    assert inserted == 2
    assert route.calls.last.request.url.params.get("digest") is None

    async with SessionLocal() as db:
        cursor = await db.get(HostLogCursor, host_id)
        assert cursor.last_digest == "dig-2"
        assert cursor.last_polled_at is not None
        rows = (await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == host_id))).all()
        assert len(rows) == 2
        assert {r.digest for r in rows} == {"dig-1", "dig-2"}


@pytest.mark.asyncio
async def test_poll_host_uses_cursor_digest_and_advances(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw2", "https://fw2.test")
    host_id = uuid.UUID(host["id"])
    ts = _now()
    calls = {"n": 0}

    def _responder(request):
        calls["n"] += 1
        if calls["n"] == 1:
            assert "digest" not in request.url.params
            return httpx.Response(200, json={"rows": [_row("dig-1", ts)]})
        assert request.url.params.get("digest") == "dig-1"
        return httpx.Response(200, json={"rows": [_row("dig-2", ts + timedelta(seconds=5))]})

    respx_mock.get(url__regex=r".*/diagnostics/firewall/log").mock(side_effect=_responder)

    async with SessionLocal() as db:
        h = await db.get(OpnsenseHost, host_id)
        first = await log_ingest_service.poll_host(db, h)
        await db.commit()
    assert first == 1

    async with SessionLocal() as db:
        h = await db.get(OpnsenseHost, host_id)
        second = await log_ingest_service.poll_host(db, h)
        await db.commit()
    assert second == 1
    assert calls["n"] == 2

    async with SessionLocal() as db:
        cursor = await db.get(HostLogCursor, host_id)
        assert cursor.last_digest == "dig-2"
        rows = (await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == host_id))).all()
        assert {r.digest for r in rows} == {"dig-1", "dig-2"}


@pytest.mark.asyncio
async def test_poll_host_dedupes_overlapping_digest(client, respx_mock):
    """An overlapping/replayed fetch of an already-ingested digest (e.g.
    cursor got reset, or the boundary line is re-sent) must not create a
    duplicate row — this is what the (host_id, digest) unique constraint
    plus ON CONFLICT DO NOTHING is for."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw3", "https://fw3.test")
    host_id = uuid.UUID(host["id"])
    ts = _now()
    respx_mock.get(url__regex=r".*/diagnostics/firewall/log").mock(
        return_value=httpx.Response(200, json={"rows": [_row("dig-a", ts)]})
    )

    async with SessionLocal() as db:
        h = await db.get(OpnsenseHost, host_id)
        first = await log_ingest_service.poll_host(db, h)
        await db.commit()
    assert first == 1

    # Simulate the cursor being unset (or OPNsense re-sending the same
    # line) so the exact same digest is fetched and inserted again.
    async with SessionLocal() as db:
        cursor = await db.get(HostLogCursor, host_id)
        cursor.last_digest = None
        await db.commit()

    async with SessionLocal() as db:
        h = await db.get(OpnsenseHost, host_id)
        second = await log_ingest_service.poll_host(db, h)
        await db.commit()
    assert second == 0

    async with SessionLocal() as db:
        rows = (await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == host_id))).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_poll_host_no_new_rows_still_updates_polled_at(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw4", "https://fw4.test")
    host_id = uuid.UUID(host["id"])
    respx_mock.get(url__regex=r".*/diagnostics/firewall/log").mock(return_value=httpx.Response(200, json={"rows": []}))

    async with SessionLocal() as db:
        h = await db.get(OpnsenseHost, host_id)
        inserted = await log_ingest_service.poll_host(db, h)
        await db.commit()

    assert inserted == 0
    async with SessionLocal() as db:
        cursor = await db.get(HostLogCursor, host_id)
        assert cursor is not None
        assert cursor.last_polled_at is not None
        assert cursor.last_digest is None


@pytest.mark.asyncio
async def test_poll_host_skips_row_without_digest(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw5", "https://fw5.test")
    host_id = uuid.UUID(host["id"])
    ts = _now()
    bad_row = _row("dig-ok", ts)
    broken_row = _row("", ts - timedelta(seconds=1))
    broken_row["__digest__"] = None
    respx_mock.get(url__regex=r".*/diagnostics/firewall/log").mock(
        return_value=httpx.Response(200, json={"rows": [bad_row, broken_row]})
    )

    async with SessionLocal() as db:
        h = await db.get(OpnsenseHost, host_id)
        inserted = await log_ingest_service.poll_host(db, h)
        await db.commit()

    assert inserted == 1
    async with SessionLocal() as db:
        rows = (await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == host_id))).all()
        assert len(rows) == 1
        assert rows[0].digest == "dig-ok"


# ── poll_all_hosts: one failing host doesn't block the others ───────────

@pytest.mark.asyncio
async def test_poll_all_hosts_continues_past_unreachable_host(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    up = await _create_host(client, "fw-up", "https://fw-up.test")
    down = await _create_host(client, "fw-down", "https://fw-down.test")

    respx_mock.get(url__regex=r"https://fw-up\.test/api/diagnostics/firewall/log").mock(
        return_value=httpx.Response(200, json={"rows": [_row("dig-up-1", _now())]})
    )
    respx_mock.get(url__regex=r"https://fw-down\.test/api/diagnostics/firewall/log").mock(
        side_effect=httpx.ConnectError("no route to host")
    )

    async with SessionLocal() as db:
        result = await log_ingest_service.poll_all_hosts(db)

    assert result["hosts_polled"] == 2
    assert result["hosts_failed"] == 1
    assert result["inserted"] == 1

    async with SessionLocal() as db:
        up_rows = (await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == uuid.UUID(up["id"])))).all()
        down_rows = (await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == uuid.UUID(down["id"])))).all()
        assert len(up_rows) == 1
        assert len(down_rows) == 0


@pytest.mark.asyncio
async def test_poll_all_hosts_skips_disabled_hosts(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw-disabled", "https://fw-disabled.test")
    resp = await client.patch(f"/api/hosts/{host['id']}", json={"enabled": False})
    assert resp.status_code == 200, resp.text

    route = respx_mock.get(url__regex=r".*/diagnostics/firewall/log").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )

    async with SessionLocal() as db:
        result = await log_ingest_service.poll_all_hosts(db)

    assert result["hosts_polled"] == 0
    assert not route.called


# ── prune_old_entries ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prune_old_entries_deletes_only_past_retention(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw-prune", "https://fw-prune.test")
    host_id = uuid.UUID(host["id"])
    now = _now()

    async with SessionLocal() as db:
        db.add(FirewallLogEntry(
            host_id=host_id, timestamp=now - timedelta(days=60), action="pass",
            interface="wan", digest="old-1", raw={},
        ))
        db.add(FirewallLogEntry(
            host_id=host_id, timestamp=now - timedelta(days=1), action="pass",
            interface="wan", digest="recent-1", raw={},
        ))
        await db.commit()

    async with SessionLocal() as db:
        deleted = await log_ingest_service.prune_old_entries(db, retention_days=30)

    assert deleted == 1
    async with SessionLocal() as db:
        remaining = (await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == host_id))).all()
        assert [r.digest for r in remaining] == ["recent-1"]


# ── enforce_size_limit ────────────────────────────────────────────────

async def _seed_entries(host_id, count):
    now = _now()
    async with SessionLocal() as db:
        for i in range(count):
            db.add(FirewallLogEntry(
                host_id=host_id, timestamp=now - timedelta(minutes=count - i), action="pass",
                interface="wan", digest=f"size-{i}", raw={},
            ))
        await db.commit()


@pytest.mark.asyncio
async def test_enforce_size_limit_deletes_oldest_until_under_target(client, respx_mock, monkeypatch):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw-size", "https://fw-size.test")
    host_id = uuid.UUID(host["id"])
    await _seed_entries(host_id, 100)

    gb = 1024**3

    async def fake_usage(db):  # 100 entries of 0.1 GB each = 10 GB live
        return {"used_bytes": 10 * gb, "live_bytes": 10 * gb, "entries": 100, "bytes_per_entry": gb // 10}

    monkeypatch.setattr(log_ingest_service, "storage_usage", fake_usage)
    async with SessionLocal() as db:
        deleted = await log_ingest_service.enforce_size_limit(db, max_size_gb=5)

    # target = 5 GB * 0.95 = 4.75 GB -> must drop 5.25 GB = 53 entries (ceil)
    assert deleted == 53
    async with SessionLocal() as db:
        remaining = (await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == host_id))).all()
    assert len(remaining) == 47
    # the oldest ones went first
    assert "size-99" in {r.digest for r in remaining}
    assert "size-0" not in {r.digest for r in remaining}


@pytest.mark.asyncio
async def test_enforce_size_limit_noop_when_under_limit_or_unlimited(client, respx_mock, monkeypatch):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw-size2", "https://fw-size2.test")
    host_id = uuid.UUID(host["id"])
    await _seed_entries(host_id, 10)
    gb = 1024**3

    async def fake_usage(db):
        return {"used_bytes": 2 * gb, "live_bytes": 2 * gb, "entries": 10, "bytes_per_entry": gb // 5}

    monkeypatch.setattr(log_ingest_service, "storage_usage", fake_usage)
    async with SessionLocal() as db:
        assert await log_ingest_service.enforce_size_limit(db, max_size_gb=5) == 0
        assert await log_ingest_service.enforce_size_limit(db, max_size_gb=0) == 0
    async with SessionLocal() as db:
        assert len((await db.scalars(select(FirewallLogEntry).where(FirewallLogEntry.host_id == host_id))).all()) == 10


@pytest.mark.asyncio
async def test_storage_usage_runs_against_real_postgres(client):
    async with SessionLocal() as db:
        usage = await log_ingest_service.storage_usage(db)
    assert usage["used_bytes"] >= 0 and usage["live_bytes"] <= usage["used_bytes"]


@pytest.mark.asyncio
async def test_poll_all_hosts_warning_names_the_real_reason(client, respx_mock, caplog):
    """Die Warnung darf nicht pauschal „nicht erreichbar“ sagen: eine Firewall,
    die antwortet, aber kaputtes HTTP liefert, ist erreichbar — der Grund muss
    im Log stehen."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    await _create_host(client, "fw-broken", "https://fw-broken.test")
    respx_mock.get(url__regex=r"https://fw-broken\.test/api/diagnostics/firewall/log").mock(
        side_effect=httpx.RemoteProtocolError("malformed chunk footer")
    )

    with caplog.at_level("WARNING", logger=log_ingest_service.logger.name):
        async with SessionLocal() as db:
            result = await log_ingest_service.poll_all_hosts(db)

    assert result["hosts_failed"] == 1
    warning = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "fw-broken" in warning
    assert "malformed chunk footer" in warning
    assert "nicht erreichbar" not in warning
