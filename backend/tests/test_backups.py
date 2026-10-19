"""Scheduled backups, retention, encrypted offsite copy: crypto, S3 signing, schedule logic, API."""
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.backup_settings import BackupSettings, BackupUpload
from app.services import backup_crypto, backup_schedule_service as svc, backup_store, notification_checks, notification_secret_store
from app.services import s3_client
from tests.test_api_tokens import _login
from tests.test_rules import _create_host

CONFIG = b"<?xml version='1.0'?><opnsense><system><hostname>fw</hostname></system></opnsense>"


# ── encryption ───────────────────────────────────────────────────────────

def test_encryption_roundtrip_and_every_failure_mode():
    blob = backup_crypto.encrypt(CONFIG, "correct horse battery")
    assert blob.startswith(backup_crypto.MAGIC) and CONFIG not in blob
    assert backup_crypto.decrypt(blob, "correct horse battery") == CONFIG
    assert backup_crypto.encrypt(CONFIG, "correct horse battery") != blob  # fresh salt and nonce
    with pytest.raises(backup_crypto.DecryptError):
        backup_crypto.decrypt(blob, "wrong passphrase!")
    tampered = bytearray(blob)
    tampered[-1] ^= 1
    with pytest.raises(backup_crypto.DecryptError):
        backup_crypto.decrypt(bytes(tampered), "correct horse battery")
    relabelled = bytearray(blob)
    relabelled[len(backup_crypto.MAGIC)] ^= 1  # salt is authenticated header data
    with pytest.raises(backup_crypto.DecryptError):
        backup_crypto.decrypt(bytes(relabelled), "correct horse battery")
    for junk in (b"", b"OPXBAK1", b"not a backup at all, really not" * 3):
        with pytest.raises(backup_crypto.DecryptError):
            backup_crypto.decrypt(junk, "x" * 12)


def test_decrypt_cli_reads_stdin_and_the_environment():
    blob = backup_crypto.encrypt(CONFIG, "cli passphrase 123")
    ok = subprocess.run([sys.executable, "-m", "app.backup_decrypt"], input=blob, capture_output=True,
                        env={**os.environ, "BACKUP_PASSPHRASE": "cli passphrase 123"})
    assert ok.returncode == 0 and ok.stdout == CONFIG
    bad = subprocess.run([sys.executable, "-m", "app.backup_decrypt"], input=blob, capture_output=True,
                         env={**os.environ, "BACKUP_PASSPHRASE": "nope nope nope!"})
    assert bad.returncode == 1 and bad.stdout == b""


# ── SigV4 (examples from the AWS S3 documentation) ───────────────────────

_AK, _SK = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_EMPTY = s3_client._sha256(b"")


def test_sigv4_matches_the_aws_get_object_example():
    headers = s3_client.sign(
        method="GET", host="examplebucket.s3.amazonaws.com", path="/test.txt", query="", payload_hash=_EMPTY,
        access_key=_AK, secret_key=_SK, region="us-east-1", now=datetime(2013, 5, 24, tzinfo=timezone.utc),
        extra_headers={"Range": "bytes=0-9"},
    )
    assert headers["authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in headers["authorization"]


def test_sigv4_matches_the_aws_put_object_example_with_an_escaped_key():
    body = b"Welcome to Amazon S3."
    headers = s3_client.sign(
        method="PUT", host="examplebucket.s3.amazonaws.com", path="/test$file.text", query="", payload_hash=s3_client._sha256(body),
        access_key=_AK, secret_key=_SK, region="us-east-1", now=datetime(2013, 5, 24, tzinfo=timezone.utc),
        extra_headers={"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
    )
    assert headers["authorization"].endswith("Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd")


def _s3(**kw):
    return s3_client.S3Client(endpoint="https://s3.test", region="eu-1", bucket="bk", access_key="AK", secret_key="SK", **kw)


@pytest.mark.asyncio
async def test_s3_client_requests_and_errors(respx_mock):
    put = respx_mock.put("https://s3.test/bk/a/b%20c.opxbak").mock(return_value=httpx.Response(200))
    await _s3().put("a/b c.opxbak", b"data")
    request = put.calls.last.request
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/") and "/eu-1/s3/aws4_request" in request.headers["authorization"]
    assert request.headers["x-amz-content-sha256"] == s3_client._sha256(b"data") and request.content == b"data"
    respx_mock.put("https://bk.s3.test/x").mock(return_value=httpx.Response(403))
    with pytest.raises(s3_client.S3Error, match="HTTP 403"):
        await _s3(path_style=False).put("x", b"1")
    respx_mock.delete("https://s3.test/bk/gone").mock(return_value=httpx.Response(404))
    await _s3().delete("gone")  # deleting what is not there is fine
    respx_mock.put("https://s3.test/bk/net").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(s3_client.S3Error, match="Verbindung"):
        await _s3().put("net", b"1")


def test_s3_endpoint_must_be_https_unless_the_lab_switch_is_on(monkeypatch):
    with pytest.raises(s3_client.S3Error):
        s3_client.S3Client(endpoint="http://minio.lan:9000", region="r", bucket="b", access_key="a", secret_key="s")
    for bad in ("ftp://x", "minio", ""):
        with pytest.raises(s3_client.S3Error):
            s3_client.S3Client(endpoint=bad, region="r", bucket="b", access_key="a", secret_key="s")
    monkeypatch.setattr(s3_client.settings, "allow_insecure_offsite", True)
    s3_client.S3Client(endpoint="http://minio.lan:9000", region="r", bucket="b", access_key="a", secret_key="s")


@pytest.mark.asyncio
async def test_s3_connection_test_proves_write_read_delete(respx_mock):
    store = {}
    respx_mock.put(url__regex=r"https://s3.test/bk/probe").mock(side_effect=lambda r: store.update(v=r.content) or httpx.Response(200))
    respx_mock.get(url__regex=r"https://s3.test/bk/probe").mock(side_effect=lambda r: httpx.Response(200, content=store["v"]))
    gone = respx_mock.delete(url__regex=r"https://s3.test/bk/probe").mock(return_value=httpx.Response(204))
    await _s3().test("probe")
    assert gone.called
    respx_mock.get(url__regex=r"https://s3.test/bk/probe").mock(return_value=httpx.Response(200, content=b"other"))
    with pytest.raises(s3_client.S3Error):
        await _s3().test("probe")


# ── schedule, retention, offsite ─────────────────────────────────────────

def _mock_fw(respx_mock, content=CONFIG):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    return respx_mock.get(url__regex=r".*/core/backup/download/this").mock(return_value=httpx.Response(200, content=content))


async def _cfg(**fields):
    async with SessionLocal() as db:
        row = await svc.get_settings(db)
        for k, v in fields.items():
            setattr(row, k, v)
        await db.commit()


async def _run(force=False):
    async with SessionLocal() as db:
        return await svc.run_due(db, force=force)


def _age(host_id, snapshot_id, hours):
    import app.services.backup_store as bs
    path = bs._host_dir(host_id) / snapshot_id
    t = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    os.utime(path, (t, t))


@pytest.mark.asyncio
async def test_a_backup_is_taken_when_due_and_not_before(client, respx_mock):
    download = _mock_fw(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    assert (await _run())["taken"] == 1 and len(backup_store.list_snapshots(host["id"])) == 1
    assert (await _run())["taken"] == 0  # fresh, nothing due
    first = backup_store.list_snapshots(host["id"])[0]["id"]
    _age(host["id"], first, 25)
    assert (await _run())["taken"] == 1 and download.call_count == 2
    assert (await _run(force=True))["taken"] == 1  # "run now" ignores the interval


@pytest.mark.asyncio
async def test_disabled_schedule_takes_nothing_and_disabled_hosts_are_skipped(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    await _cfg(scheduled_enabled=False)
    assert (await _run())["taken"] == 0
    await _cfg(scheduled_enabled=True)
    await client.patch(f"/api/hosts/{host['id']}", json={"enabled": False})
    assert (await _run())["taken"] == 0


@pytest.mark.asyncio
async def test_retention_keeps_only_the_newest_n(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    await _cfg(keep_count=3)
    for _ in range(6):
        backup_store.save_snapshot(host["id"], CONFIG)
    result = await _run()
    kept = backup_store.list_snapshots(host["id"])
    assert len(kept) == 3 and result["pruned"] >= 3


@pytest.mark.asyncio
async def test_failure_is_counted_a_garbage_answer_is_not_stored_and_others_continue(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    ok = await _create_host(client, "fw-ok", "https://ok.test")
    bad = await _create_host(client, "fw-bad", "https://bad.test")
    junk = await _create_host(client, "fw-junk", "https://junk.test")
    respx_mock.get("https://ok.test/api/core/backup/download/this").mock(return_value=httpx.Response(200, content=CONFIG))
    respx_mock.get("https://bad.test/api/core/backup/download/this").mock(return_value=httpx.Response(500))
    respx_mock.get("https://junk.test/api/core/backup/download/this").mock(return_value=httpx.Response(200, content=b"<html>login</html>"))
    result = await _run()
    assert (result["taken"], result["failed"]) == (1, 2)
    assert backup_store.list_snapshots(ok["id"]) and not backup_store.list_snapshots(bad["id"]) and not backup_store.list_snapshots(junk["id"])


async def _offsite_on(passphrase="offsite passphrase 1"):
    notification_secret_store.save_secret(svc.S3_SECRET, "SK")
    notification_secret_store.save_secret(svc.PASSPHRASE, passphrase)
    await _cfg(offsite_enabled=True, s3_endpoint="https://s3.test", s3_bucket="bk", s3_access_key="AK", s3_secret_set=True, passphrase_set=True, s3_prefix="/opnexus/")


@pytest.mark.asyncio
async def test_offsite_upload_is_encrypted_once_only_and_named_by_host(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw 1/main", "https://fw1.test")
    await _offsite_on()
    puts = respx_mock.put(url__regex=r"https://s3\.test/bk/opnexus/.*").mock(return_value=httpx.Response(200))
    result = await _run()
    assert result["uploaded"] == 1 and puts.call_count == 1
    request = puts.calls.last.request
    assert re.fullmatch(rf"/bk/opnexus/fw-1-main-{host['id'][:8]}/\d{{8}}T\d+\.opxbak", request.url.path)
    assert CONFIG not in request.content and backup_crypto.decrypt(request.content, "offsite passphrase 1") == CONFIG
    assert (await _run())["uploaded"] == 0 and puts.call_count == 1  # ledger: nothing twice
    async with SessionLocal() as db:
        assert len((await db.scalars(select(BackupUpload))).all()) == 1
        cfg = await svc.get_settings(db)
        assert cfg.last_offsite_error is None and cfg.last_offsite_ok_at is not None


@pytest.mark.asyncio
async def test_offsite_failure_is_recorded_and_retried_next_round(client, respx_mock):
    _mock_fw(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    await _offsite_on()
    route = respx_mock.put(url__regex=r"https://s3\.test/bk/.*").mock(return_value=httpx.Response(503))
    await _run()
    async with SessionLocal() as db:
        assert "HTTP 503" in (await svc.get_settings(db)).last_offsite_error
        assert (await db.scalars(select(BackupUpload))).all() == []
    route.mock(return_value=httpx.Response(200))
    assert (await _run())["uploaded"] == 1
    async with SessionLocal() as db:
        assert (await svc.get_settings(db)).last_offsite_error is None


@pytest.mark.asyncio
async def test_incomplete_offsite_config_and_lost_passphrase_fail_closed(client, respx_mock):
    _mock_fw(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    await _cfg(offsite_enabled=True)
    await _run()
    async with SessionLocal() as db:
        assert "unvollständig" in (await svc.get_settings(db)).last_offsite_error
    await _offsite_on()
    notification_secret_store.delete_secret(svc.PASSPHRASE)  # respx would fail on any upload: nothing leaves unencrypted
    await _run()
    async with SessionLocal() as db:
        assert "Passphrase" in (await svc.get_settings(db)).last_offsite_error


@pytest.mark.asyncio
async def test_pruned_snapshots_leave_the_ledger(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    await _offsite_on()
    respx_mock.put(url__regex=r"https://s3\.test/bk/.*").mock(return_value=httpx.Response(200))
    await _cfg(keep_count=3)
    for _ in range(4):
        backup_store.save_snapshot(host["id"], CONFIG)
    await _run()
    async with SessionLocal() as db:
        names = {r.snapshot_id for r in (await db.scalars(select(BackupUpload))).all()}
    assert names <= {s["id"] for s in backup_store.list_snapshots(host["id"])}


# ── notifications ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_backup_problem_after_three_missed_intervals(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    async with SessionLocal() as db:
        assert await notification_checks.check_backup_stale(db) == {}  # brand-new host: not yet
    from app.models.host import OpnsenseHost
    async with SessionLocal() as db:
        row = await db.get(OpnsenseHost, __import__("uuid").UUID(host["id"]))
        row.created_at = datetime.now(timezone.utc) - timedelta(days=5)
        await db.commit()
    async with SessionLocal() as db:
        assert list(await notification_checks.check_backup_stale(db)) == [host["id"]]
    name = backup_store.save_snapshot(host["id"], CONFIG)
    async with SessionLocal() as db:
        assert await notification_checks.check_backup_stale(db) == {}
    _age(host["id"], name, 80)
    async with SessionLocal() as db:
        assert list(await notification_checks.check_backup_stale(db)) == [host["id"]]
    await _cfg(scheduled_enabled=False)
    async with SessionLocal() as db:
        assert await notification_checks.check_backup_stale(db) == {}


@pytest.mark.asyncio
async def test_offsite_problem_follows_the_last_error():
    await _cfg(offsite_enabled=True, last_offsite_error="HTTP 503")
    async with SessionLocal() as db:
        assert (await notification_checks.check_backup_offsite(db)) == {"offsite": {"message": "HTTP 503"}}
    await _cfg(last_offsite_error=None)
    async with SessionLocal() as db:
        assert await notification_checks.check_backup_offsite(db) == {}


# ── API ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_roundtrip_never_returns_secrets(client):
    put = await client.put("/api/settings/backups", json={
        "interval_hours": 12, "keep_count": 10, "offsite_enabled": True, "s3_endpoint": "https://s3.test/", "s3_bucket": "bk",
        "s3_access_key": "AK", "s3_secret_key": "TOPSECRET", "passphrase": "a long passphrase here"})
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["s3_endpoint"] == "https://s3.test" and body["s3_secret_set"] and body["passphrase_set"]
    assert "TOPSECRET" not in put.text and "passphrase here" not in put.text
    assert notification_secret_store.read_secret(svc.S3_SECRET) == "TOPSECRET"
    cleared = (await client.put("/api/settings/backups", json={"clear_s3_secret": True})).json()
    assert cleared["s3_secret_set"] is False and notification_secret_store.read_secret(svc.S3_SECRET) is None


@pytest.mark.asyncio
async def test_settings_validation(client):
    for body in ({"interval_hours": 0}, {"keep_count": 2}, {"passphrase": "short"}, {"s3_region": ""}):
        assert (await client.put("/api/settings/backups", json=body)).status_code == 422, body


@pytest.mark.asyncio
async def test_status_is_visible_to_everyone_settings_only_to_admins_run_now_to_operators(client, respx_mock):
    _mock_fw(respx_mock)
    await _create_host(client, "fw1", "https://fw1.test")
    for role in ("read_only", "operator"):
        name = f"bk-{role}"
        assert (await client.post("/api/users", json={"username": name, "password": "backup-password-1", "role": role})).status_code == 201
        user = await _login(name, "backup-password-1")
        try:
            status = (await user.get("/api/backups/status")).json()
            assert "s3_endpoint" not in status and status["interval_hours"] == 24
            assert (await user.get("/api/settings/backups")).status_code == 403
            assert (await user.put("/api/settings/backups", json={"keep_count": 5})).status_code == 403
            assert (await user.post("/api/settings/backups/test-offsite")).status_code == 403
            expected = 403 if role == "read_only" else 200
            assert (await user.post("/api/backups/run")).status_code == expected
        finally:
            await user.aclose()


@pytest.mark.asyncio
async def test_test_offsite_reports_the_reason(client, respx_mock):
    resp = await client.post("/api/settings/backups/test-offsite")
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "OFFSITE_TEST_FAILED"
    await _offsite_on()
    respx_mock.route(url__regex=r"https://s3\.test/bk/.*").mock(return_value=httpx.Response(403))
    resp = await client.post("/api/settings/backups/test-offsite")
    assert resp.status_code == 502 and "403" in resp.json()["detail"]["params"]["reason"]
    respx_mock.clear()
    store = {}
    respx_mock.put(url__regex=r"https://s3\.test/bk/.*").mock(side_effect=lambda r: store.update(v=r.content) or httpx.Response(200))
    respx_mock.get(url__regex=r"https://s3\.test/bk/.*").mock(side_effect=lambda r: httpx.Response(200, content=store["v"]))
    respx_mock.delete(url__regex=r"https://s3\.test/bk/.*").mock(return_value=httpx.Response(204))
    assert (await client.post("/api/settings/backups/test-offsite")).json() == {"success": True}


@pytest.mark.asyncio
async def test_settings_changes_are_audited_without_secrets(client):
    from app.models.audit import AuditEvent
    await client.put("/api/settings/backups", json={"s3_secret_key": "LEAKME-1234"})
    async with SessionLocal() as db:
        rows = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "settings"))).all()
    assert rows and "LEAKME" not in json.dumps([r.details for r in rows]) + "".join(r.message or "" for r in rows)


# ── config diff ──────────────────────────────────────────────────────────

def _xml(*, hostname="fw", password="p1", dns="1.1.1.1", cert="A" * 200, revision="r1", keylength="4096"):
    return (f"<opnsense><revision><time>{revision}</time></revision><system><hostname>{hostname}</hostname>"
            f"<user><name>root</name><password>{password}</password><apikey>KEY-{password}</apikey></user>"
            f"<dnsserver>{dns}</dnsserver></system><cert><crt>{cert}</crt><keylength>{keylength}</keylength></cert></opnsense>").encode()


@pytest.mark.asyncio
async def test_diff_shows_changes_and_hides_every_secret(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    backup_store.save_snapshot(host["id"], _xml())
    backup_store.save_snapshot(host["id"], _xml(hostname="fw-new", password="SUPERSECRET2", dns="9.9.9.9", cert="B" * 200, revision="r2", keylength="2048"))
    body = (await client.get(f"/api/hosts/{host['id']}/backup-diff")).json()
    text = "\n".join(body["lines"])
    assert not body["identical"] and body["added"] >= 4 and body["removed"] >= 4
    assert "-    <hostname>fw</hostname>" in text and "+    <hostname>fw-new</hostname>" in text
    assert "9.9.9.9" in text and "<keylength>2048</keylength>" in text       # ordinary values stay readable
    for secret in ("SUPERSECRET2", "KEY-SUPERSECRET2", "p1", "A" * 50, "B" * 50):
        assert secret not in text, secret
    assert "‹sha256:" in text                                                 # a changed secret is still visible as a change
    assert "r1" not in text and "r2" not in text                              # the volatile revision block is ignored


@pytest.mark.asyncio
async def test_identical_backups_and_explicit_selection(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    first = backup_store.save_snapshot(host["id"], _xml(revision="r1"))
    second = backup_store.save_snapshot(host["id"], _xml(revision="r2"))
    assert (await client.get(f"/api/hosts/{host['id']}/backup-diff")).json()["identical"] is True
    third = backup_store.save_snapshot(host["id"], _xml(dns="8.8.8.8"))
    body = (await client.get(f"/api/hosts/{host['id']}/backup-diff", params={"a": first, "b": third})).json()
    assert (body["a"], body["b"]) == (first, third) and not body["identical"]
    assert (await client.get(f"/api/hosts/{host['id']}/backup-diff")).json()["a"] == second  # default: previous vs newest


@pytest.mark.asyncio
async def test_diff_errors(client):
    import uuid
    assert (await client.get(f"/api/hosts/{uuid.uuid4()}/backup-diff")).status_code == 404
    host = await _create_host(client, "fw1", "https://fw1.test")
    assert (await client.get(f"/api/hosts/{host['id']}/backup-diff")).status_code == 404          # nothing to compare
    one = backup_store.save_snapshot(host["id"], _xml())
    assert (await client.get(f"/api/hosts/{host['id']}/backup-diff")).status_code == 404          # only one
    bad = backup_store.save_snapshot(host["id"], b"<html>not a config</html>")
    resp = await client.get(f"/api/hosts/{host['id']}/backup-diff", params={"a": one, "b": bad})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "BACKUP_UNLESBAR"
    assert (await client.get(f"/api/hosts/{host['id']}/backup-diff", params={"a": "../../etc/passwd", "b": one})).status_code == 404
    xxe = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><opnsense>&e;</opnsense>'
    evil = backup_store.save_snapshot(host["id"], xxe)
    assert (await client.get(f"/api/hosts/{host['id']}/backup-diff", params={"a": one, "b": evil})).status_code == 422


@pytest.mark.asyncio
async def test_diff_is_capped(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    many = "".join(f"<dns{i}>1.1.1.{i % 250}</dns{i}>" for i in range(5000))
    backup_store.save_snapshot(host["id"], f"<opnsense><system>{many}</system></opnsense>".encode())
    backup_store.save_snapshot(host["id"], f"<opnsense><system>{many.replace('1.1.1', '2.2.2')}</system></opnsense>".encode())
    body = (await client.get(f"/api/hosts/{host['id']}/backup-diff")).json()
    assert body["truncated"] is True and len(body["lines"]) == 3000
