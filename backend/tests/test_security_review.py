"""Regression tests for the 2026-10-07 security review.

Everything runs against mocked OPNsense responses and the disposable test
database; no real firewall, credential or backup is involved.
"""
import asyncio
import datetime
import ipaddress
import json
import ssl
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.audit import AuditEvent
from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
from app.models.managed_certificate import ManagedCertificate
from app.models.notification import NotificationSettings
from app.models.rule_deployment import RuleDeployment
from app.models.user import AdminUser
from app.routers.auth import _accept_second_factor
from app.services import (
    auth_service,
    backup_store,
    notification_delivery,
    notification_secret_store,
    opnsense_client,
    totp_service,
)
from app.services.redaction import redact_data, redact_text
from app.services.rule_deploy_service import sweep_expired_deployments

PASSWORD = "a-long-test-password-1"


# ── helpers ───────────────────────────────────────────────────────────

async def _login(username: str, password: str, expect: int = 200) -> AsyncClient:
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    resp = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == expect, resp.text
    return ac


async def _make_user(client, username: str, role: str = "read_only") -> dict:
    resp = await client.post("/api/users", json={"username": username, "password": PASSWORD, "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _host(client, respx_mock, name="fw-sec", url="https://fw-sec.test") -> dict:
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": name, "base_url": url, "api_key": "key", "api_secret": "secret-value", "verify_tls": False},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _audit(**filters) -> list[AuditEvent]:
    async with SessionLocal() as db:
        query = select(AuditEvent).order_by(AuditEvent.created_at)
        for column, value in filters.items():
            query = query.where(getattr(AuditEvent, column) == value)
        return list((await db.scalars(query)).all())


# ── SEC-01: raw backup download is admin-only and audited ─────────────

@pytest.mark.asyncio
async def test_reader_cannot_download_raw_backup_but_can_list(client, respx_mock):
    host = await _host(client, respx_mock)
    marker = b"<opnsense><password>SYNTHETIC-HASH-MARKER</password></opnsense>"
    respx_mock.get("https://fw-sec.test/api/core/backup/download/this").mock(return_value=httpx.Response(200, content=marker))
    created = (await client.post(f"/api/hosts/{host['id']}/backups")).json()
    await _make_user(client, "sec01-reader")
    reader = await _login("sec01-reader", PASSWORD)
    try:
        assert (await reader.get(f"/api/hosts/{host['id']}/backups")).status_code == 200  # metadata stays visible
        denied = await reader.get(f"/api/hosts/{host['id']}/backups/{created['id']}")
        assert denied.status_code == 403
        assert b"SYNTHETIC-HASH-MARKER" not in denied.content
        assert denied.json()["detail"]["code"] == "READ_ONLY_ACCESS"
    finally:
        await reader.aclose()

    ok = await client.get(f"/api/hosts/{host['id']}/backups/{created['id']}")
    assert ok.status_code == 200 and ok.content == marker

    events = await _audit(kind="backup", action="download")
    assert {e.status for e in events} == {"denied", "accepted"}
    assert all("SYNTHETIC-HASH-MARKER" not in json.dumps(e.details) + (e.message or "") for e in events)


@pytest.mark.asyncio
async def test_anonymous_cannot_download_backup(anon_client):
    resp = await anon_client.get(f"/api/hosts/{uuid.uuid4()}/backups/x.xml")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_backup_of_other_host_is_not_reachable_by_traversal(client, respx_mock):
    host = await _host(client, respx_mock)
    other = backup_store.save_snapshot(uuid.uuid4(), b"<other/>")
    resp = await client.get(f"/api/hosts/{host['id']}/backups/..%2F{uuid.uuid4()}%2F{other}")
    assert resp.status_code == 404


# ── SEC-02: password change / reset ends every session ────────────────

@pytest.mark.asyncio
async def test_admin_password_reset_revokes_all_sessions_of_target(client):
    user = await _make_user(client, "sec02-target", "admin")
    session_a = await _login("sec02-target", PASSWORD)
    session_b = await _login("sec02-target", PASSWORD)
    try:
        assert (await session_a.get("/api/hosts")).status_code == 200
        reset = await client.post(f"/api/users/{user['id']}/reset-password", json={"new_password": "brand-new-password-9"})
        assert reset.status_code == 200, reset.text
        for stale in (session_a, session_b):
            assert (await stale.get("/api/hosts")).status_code == 401
            assert (await stale.get("/api/auth/me")).json() == {"authenticated": False}
    finally:
        await session_a.aclose()
        await session_b.aclose()
    fresh = await _login("sec02-target", "brand-new-password-9")
    await fresh.aclose()


@pytest.mark.asyncio
async def test_self_service_password_change_revokes_other_sessions_but_keeps_new_one(client):
    await _make_user(client, "sec02-self", "admin")
    attacker = await _login("sec02-self", PASSWORD)
    me = await _login("sec02-self", PASSWORD)
    try:
        resp = await me.post(
            "/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "another-long-password-2"}
        )
        assert resp.status_code == 200, resp.text
        assert (await me.get("/api/hosts")).status_code == 200  # replacement session works
        assert (await attacker.get("/api/hosts")).status_code == 401
    finally:
        await attacker.aclose()
        await me.aclose()


@pytest.mark.asyncio
async def test_login_that_raced_a_password_change_is_refused():
    async with SessionLocal() as db:
        user = (await db.scalars(select(AdminUser).where(AdminUser.username == settings.admin_username))).one()
        verified_hash, user_id = user.password_hash, user.id
        with pytest.raises(Exception) as exc:
            await auth_service.create_session(db, user, verified_hash="$argon2id$stale-hash-from-before-the-reset")
        assert getattr(exc.value, "status_code", None) == 401
    async with SessionLocal() as db:
        user = await db.get(AdminUser, user_id)
        token, _ = await auth_service.create_session(db, user, verified_hash=verified_hash)  # the real hash is accepted
        await auth_service.revoke_session(db, token)


@pytest.mark.asyncio
async def test_enabling_and_resetting_2fa_end_other_sessions(client):
    user = await _make_user(client, "sec02-mfa", "admin")
    other = await _login("sec02-mfa", PASSWORD)
    me = await _login("sec02-mfa", PASSWORD)
    try:
        setup = (await me.post("/api/auth/2fa/setup", json={"password": PASSWORD})).json()
        code = totp_service.code_at(setup["secret"])
        assert (await me.post("/api/auth/2fa/enable", json={"code": code})).status_code == 200
        assert (await me.get("/api/hosts")).status_code == 200
        assert (await other.get("/api/hosts")).status_code == 401
        # admin recovery reset also forces a fresh login
        assert (await client.post(f"/api/users/{user['id']}/reset-2fa")).status_code == 200
        assert (await me.get("/api/hosts")).status_code == 401
    finally:
        await other.aclose()
        await me.aclose()


# ── SEC-03: SMTP verifies the peer ────────────────────────────────────

def _smtp_row(**overrides) -> NotificationSettings:
    values = dict(
        smtp_host="smtp.test", smtp_port=587, smtp_security="starttls", smtp_username=None,
        smtp_password_set=False, smtp_from_address="opnexus@example.test", smtp_to_addresses=["ops@example.test"],
    )
    values.update(overrides)
    return NotificationSettings(**values)


def test_smtp_context_requires_certificate_and_hostname():
    ctx = notification_delivery._tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


@pytest.mark.parametrize("mode", ["starttls", "ssl"])
def test_smtp_passes_verifying_context_in_both_tls_modes(mode):
    with patch("app.services.notification_delivery.smtplib.SMTP") as plain, patch(
        "app.services.notification_delivery.smtplib.SMTP_SSL"
    ) as implicit:
        notification_delivery._send_email_sync(_smtp_row(smtp_security=mode), "s", "b")
    if mode == "ssl":
        ctx = implicit.call_args.kwargs["context"]
    else:
        ctx = plain.return_value.starttls.call_args.kwargs["context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def _make_ip_cert():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "smtp.local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
    )


async def _smtp_tls_server(cert_pem: str, key_pem: str, received: list):
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as cf, tempfile.NamedTemporaryFile("w", suffix=".pem") as kf:
        cf.write(cert_pem); cf.flush(); kf.write(key_pem); kf.flush()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cf.name, kf.name)

    async def handle(reader, writer):
        try:
            writer.write(b"220 test ESMTP\r\n")
            data_mode = False
            while True:
                line = await reader.readline()
                if not line:
                    break
                if data_mode:
                    if line == b".\r\n":
                        data_mode = False
                        received.append(True)
                        writer.write(b"250 queued\r\n")
                    continue
                cmd = line.decode().strip().upper()
                if cmd.startswith("DATA"):
                    data_mode = True
                    writer.write(b"354 go\r\n")
                elif cmd.startswith("QUIT"):
                    writer.write(b"221 bye\r\n")
                    break
                else:
                    writer.write(b"250 ok\r\n")
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_smtp_rejects_untrusted_certificate_and_accepts_trusted_one(monkeypatch):
    cert_pem, key_pem = _make_ip_cert()
    received: list = []
    server, port = await _smtp_tls_server(cert_pem, key_pem, received)
    row = _smtp_row(smtp_host="127.0.0.1", smtp_port=port, smtp_security="ssl")
    try:
        # Self-signed and not in the trust store -> handshake must fail, nothing is sent.
        with pytest.raises(notification_delivery.NotificationDeliveryError):
            await notification_delivery.send_email(row, "subject", "body")
        assert received == []

        with tempfile.NamedTemporaryFile("w", suffix=".pem") as ca:
            ca.write(cert_pem); ca.flush()
            monkeypatch.setattr(settings, "smtp_ca_file", ca.name)
            await notification_delivery.send_email(row, "subject", "body")
        assert received == [True]
    finally:
        server.close()
        await server.wait_closed()


def test_smtp_plaintext_authentication_is_refused_by_default():
    notification_secret_store.save_secret("smtp_password", "hunter2")
    try:
        row = _smtp_row(smtp_security="none", smtp_username="opnexus", smtp_password_set=True)
        with patch("app.services.notification_delivery.smtplib.SMTP") as smtp_cls:
            with pytest.raises(notification_delivery.NotificationDeliveryError):
                notification_delivery._send_email_sync(row, "s", "b")
            smtp_cls.assert_not_called()
    finally:
        notification_secret_store.delete_secret("smtp_password")


# ── SEC-11: missing notification secrets fail closed ──────────────────

@pytest.mark.asyncio
async def test_webhook_with_missing_signing_secret_sends_nothing(respx_mock):
    notification_secret_store.delete_secret("webhook_signing_secret")
    route = respx_mock.post("https://hook.test/x").mock(return_value=httpx.Response(200))
    row = NotificationSettings(webhook_enabled=True, webhook_url="https://hook.test/x", webhook_signing_secret_set=True)
    from app.models.notification import NotificationProblem

    problem = NotificationProblem(problem_type="host_offline", dedup_key="k", context={})
    with pytest.raises(notification_delivery.NotificationDeliveryError):
        await notification_delivery.send_webhook(row, problem, "opened")
    assert not route.called


def test_smtp_with_configured_but_missing_password_does_not_send_unauthenticated():
    notification_secret_store.delete_secret("smtp_password")
    row = _smtp_row(smtp_username="opnexus", smtp_password_set=True)
    with patch("app.services.notification_delivery.smtplib.SMTP") as smtp_cls:
        with pytest.raises(notification_delivery.NotificationDeliveryError):
            notification_delivery._send_email_sync(row, "s", "b")
        smtp_cls.assert_not_called()


def test_compose_mounts_a_persistent_notification_secret_volume():
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    if not path.exists():
        pytest.skip("repository root (docker-compose.yml) is not mounted in this container")
    compose = path.read_text()
    assert "notification_secrets:/data/notification-secrets" in compose
    assert "NOTIFICATION_SECRET_DIR: /data/notification-secrets" in compose


# ── SEC-04: unconfirmed writes keep their recovery obligation ─────────

def _rule_payload(host_id: str) -> dict:
    return {
        "description": "Allow SSH", "interface": "lan", "direction": "in", "protocol": "tcp", "action": "pass",
        "source_net": "any", "destination_net": "lan", "destination_port": "22", "target_host_ids": [host_id],
    }


@pytest.mark.asyncio
async def test_apply_timeout_after_successful_write_stays_recoverable(client, respx_mock):
    host = await _host(client, respx_mock)
    rule_id = (await client.post("/api/rules", json=_rule_payload(host["id"]))).json()["id"]
    respx_mock.post("https://fw-sec.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "live-uuid"})
    )
    respx_mock.post("https://fw-sec.test/api/firewall/filter/apply").mock(side_effect=httpx.ReadTimeout("lost reply"))

    dep = (await client.post(f"/api/rules/{rule_id}/deploy")).json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["revert_deadline"] is not None  # recovery obligation survives the ambiguous apply

    # the sweeper picks it up after the deadline and removes the possibly-live rule
    async with SessionLocal() as db:
        row = (await db.scalars(select(RuleDeployment))).one()
        row.revert_deadline = datetime.datetime.now(datetime.timezone.utc) - timedelta(seconds=1)
        await db.commit()
    del_route = respx_mock.post("https://fw-sec.test/api/firewall/filter/delRule/live-uuid").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )
    respx_mock.post("https://fw-sec.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    async with SessionLocal() as db:
        await sweep_expired_deployments(db)
    assert del_route.called


@pytest.mark.asyncio
async def test_rejected_write_does_not_arm_a_revert(client, respx_mock):
    host = await _host(client, respx_mock)
    rule_id = (await client.post("/api/rules", json=_rule_payload(host["id"]))).json()["id"]
    add = respx_mock.post("https://fw-sec.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "failed", "validations": {"rule.interface": "invalid"}})
    )
    dep = (await client.post(f"/api/rules/{rule_id}/deploy")).json()["deployments"][0]
    assert add.called
    assert dep["status"] == "error" and dep["revert_deadline"] is None  # nothing live, nothing to undo


@pytest.mark.asyncio
async def test_hierarchy_reorder_failure_keeps_deadline(client, respx_mock):
    host = await _host(client, respx_mock)
    rule_id = (await client.post("/api/rules", json=_rule_payload(host["id"]))).json()["id"]
    respx_mock.post("https://fw-sec.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "live-uuid"})
    )
    respx_mock.post("https://fw-sec.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    with patch(
        "app.services.policy_hierarchy_deploy_service.reassert_after_managed_change",
        side_effect=opnsense_client.OpnsenseAPIError("ordering failed"),
    ):
        dep = (await client.post(f"/api/rules/{rule_id}/deploy")).json()["deployments"][0]
    assert dep["status"] == "error" and dep["revert_deadline"] is not None


# ── SEC-05: no credentials over plain HTTP ────────────────────────────

@pytest.mark.parametrize(
    "url",
    ["http://fw.test", "ftp://fw.test", "https://user:pw@fw.test", "https://fw.test/?x=1", "https://fw.test/#f", "https://"],
)
@pytest.mark.asyncio
async def test_unsafe_firewall_urls_are_rejected(client, url):
    resp = await client.post("/api/hosts", json={"name": "bad", "base_url": url, "api_key": "k", "api_secret": "s"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_https_ip_and_name_urls_are_accepted(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    for name, url in (("by-ip", "https://10.1.2.3"), ("by-name-port", "https://fw.example.test:8443")):
        resp = await client.post("/api/hosts", json={"name": name, "base_url": url, "api_key": "k", "api_secret": "s", "verify_tls": False})
        assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_existing_http_host_never_receives_credentials(respx_mock):
    from app.models.host import OpnsenseHost

    route = respx_mock.get("http://legacy.test/api/core/firmware/info").mock(return_value=httpx.Response(200, json={}))
    legacy = OpnsenseHost(id=uuid.uuid4(), name="legacy", base_url="http://legacy.test", api_key="k", verify_tls=True)
    with pytest.raises(opnsense_client.OpnsenseAPIError):
        await opnsense_client.get_firmware_info(legacy)
    assert not route.called


def test_http_override_is_explicit(monkeypatch):
    from app.services.firewall_url import check_base_url

    assert check_base_url("http://lab.test")
    monkeypatch.setattr(settings, "allow_insecure_firewall_urls", True)
    assert check_base_url("http://lab.test") is None


# ── SEC-06: second factor is single-use even under concurrency ────────

async def _mfa_user(client, username: str) -> tuple[uuid.UUID, str]:
    await _make_user(client, username, "admin")
    secret = totp_service.generate_secret()
    async with SessionLocal() as db:
        user = (await db.scalars(select(AdminUser).where(AdminUser.username == username))).one()
        totp_service.save_secret(user.id, secret)
        user.totp_enabled = True
        user.totp_last_step = 0
        await db.commit()
        return user.id, secret


async def _try_code(user_id, code) -> bool:
    async with SessionLocal() as db:
        user = await db.get(AdminUser, user_id)
        return await _accept_second_factor(db, user, code)


@pytest.mark.asyncio
async def test_same_totp_code_is_accepted_exactly_once_concurrently(client):
    user_id, secret = await _mfa_user(client, "sec06-totp")
    code = totp_service.code_at(secret)
    results = await asyncio.gather(*[_try_code(user_id, code) for _ in range(4)])
    assert sorted(results) == [False, False, False, True]
    assert await _try_code(user_id, code) is False  # and sequential replay stays refused


@pytest.mark.asyncio
async def test_same_recovery_code_is_accepted_exactly_once_concurrently(client):
    user_id, _ = await _mfa_user(client, "sec06-recovery")
    plain, hashes = totp_service.new_recovery_codes()
    async with SessionLocal() as db:
        (await db.get(AdminUser, user_id)).totp_recovery_hashes = hashes
        await db.commit()
    results = await asyncio.gather(*[_try_code(user_id, plain[0]) for _ in range(4)])
    assert sorted(results) == [False, False, False, True]
    async with SessionLocal() as db:
        assert len((await db.get(AdminUser, user_id)).totp_recovery_hashes) == len(hashes) - 1
    assert await _try_code(user_id, plain[1]) is True  # other codes keep working


# ── SEC-07: throttle state is bounded, hashing leaves the event loop ──

def test_throttle_evicts_expired_records_and_stays_bounded(monkeypatch):
    throttle = auth_service._LoginThrottle()
    for i in range(100):
        throttle.record_failure(f"10.0.0.{i}", f"user{i}")
    assert len(throttle._records) == 200 and len(throttle._failures_since_success) == 100
    later = time.monotonic() + settings.login_window_seconds + settings.login_lockout_seconds + 1
    throttle._purge(later, force=True)
    assert throttle._records == {}
    throttle._purge(later + throttle.COUNTER_TTL_SECONDS + 1, force=True)
    assert throttle._failures_since_success == {}

    monkeypatch.setattr(throttle, "MAX_RECORDS", 50)
    monkeypatch.setattr(throttle, "MAX_COUNTERS", 10)
    for i in range(100):
        throttle.record_failure(f"192.168.0.{i}", f"name{i}")
    throttle._purge(time.monotonic(), force=True)
    assert len(throttle._records) <= 50 and len(throttle._failures_since_success) <= 10


@pytest.mark.asyncio
async def test_password_hashing_runs_off_the_event_loop():
    main_thread = threading.get_ident()
    seen = []
    real = auth_service.verify_password

    def spy(stored, password):
        seen.append(threading.get_ident())
        return real(stored, password)

    stored = auth_service.hash_password("pw-for-thread-test")
    with patch.object(auth_service, "verify_password", spy):
        assert await auth_service.verify_password_async(stored, "pw-for-thread-test") is True
    assert seen and seen[0] != main_thread


# ── SEC-08: upstream errors never carry secrets ───────────────────────

def test_redaction_keeps_validation_messages_readable():
    body = (
        '{"result": "failed", "validations": {"instance.cert": "Please select a valid certificate from the list.",'
        ' "instance.auth": "Authentication method required"}, "api_key": "k-MARKER"}'
    )
    out = redact_text(body)
    assert "Please select a valid certificate" in out
    assert "Authentication method required" in out
    assert "k-MARKER" not in out  # secrets outside "validations" are still masked


def test_redaction_scrubs_json_pem_and_known_secrets():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"
    body = json.dumps({
        "result": "failed",
        "validations": {"rule.description": "bad", "ipsec.pre_shared_key": "psk-MARKER"},
        "echo": {"password": "pw-MARKER", "private_key": pem, "note": pem},
    })
    out = redact_text(body, "api-key-MARKER") + redact_data({"api_secret": "x-MARKER", "msg": "token=tok-MARKER"})
    for marker in ("psk-MARKER", "pw-MARKER", "MIIEvQIBADANBg", "x-MARKER", "tok-MARKER"):
        assert marker not in out
    assert "rule.description" in redact_text(body)  # useful diagnostics survive
    assert len(redact_text("x" * 5000)) <= 305
    assert "api-key-MARKER" not in redact_text("error for api-key-MARKER", "api-key-MARKER")


@pytest.mark.asyncio
async def test_reflected_secrets_do_not_reach_reader_status_or_deploy_message(client, respx_mock):
    host = await _host(client, respx_mock)
    leak = {"result": "failed", "validations": {"x": "bad"}, "request": {"api_secret": "secret-value", "psk": "PSK-MARKER"}}
    respx_mock.post("https://fw-sec.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json=leak)
    )
    rule_id = (await client.post("/api/rules", json=_rule_payload(host["id"]))).json()["id"]
    dep = (await client.post(f"/api/rules/{rule_id}/deploy")).json()["deployments"][0]
    assert "PSK-MARKER" not in json.dumps(dep) and "secret-value" not in json.dumps(dep)

    respx_mock.get(url__regex=r".*/getVipStatus").mock(
        return_value=httpx.Response(500, text='{"error": "boom", "password": "PW-MARKER"} secret-value')
    )
    listing = await client.get("/api/hosts")
    assert "PW-MARKER" not in listing.text and "secret-value" not in listing.text


# ── SEC-09: host deletion guards VPN / certificate / ACME objects ─────

@pytest.mark.asyncio
async def test_host_with_deployed_certificate_or_vpn_cannot_be_deleted(client, respx_mock):
    host = await _host(client, respx_mock)
    cert = await client.post(
        "/api/certificates/managed",
        json={
            "host_id": host["id"], "action": "import", "description": "c", "commonname": "", "country": "DE",
            "state": "", "city": "", "organization": "", "email": "", "key_type": "2048", "digest": "sha256",
            "cert_type": "server_cert", "lifetime": 397, "caref": None, "csr_payload": None,
            "crt_payload": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----", "private_key": "PRIVATE-MARKER",
        },
    )
    assert cert.status_code == 201, cert.text
    cert_id = uuid.UUID(cert.json()["id"])

    # never-deployed draft: deletion is fine and takes its key file along
    from app.services import cert_key_store

    assert cert_key_store.read_secret(cert_id) == "PRIVATE-MARKER"
    async with SessionLocal() as db:
        row = await db.get(ManagedCertificate, cert_id)
        row.opnsense_uuid = "remote-cert"
        await db.commit()
    blocked = await client.delete(f"/api/hosts/{host['id']}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "HOST_HAT_NOCH_VPN_ODER_ZERTIFIKATS_OBJEKTE_DIESE_ZUERST_ENTFERNEN"

    async with SessionLocal() as db:
        row = await db.get(ManagedCertificate, cert_id)
        row.opnsense_uuid = None
        await db.commit()
    assert (await client.delete(f"/api/hosts/{host['id']}")).status_code == 204
    assert cert_key_store.read_secret(cert_id) is None


@pytest.mark.asyncio
async def test_host_with_vpn_deployment_cannot_be_deleted(client, respx_mock):
    host = await _host(client, respx_mock)
    tunnel = await client.post("/api/ipsec/tunnels", json={
        "description": "t", "local_address": "203.0.113.1", "remote_address": "198.51.100.1", "ike_version": "ikev2",
        "proposal": "default", "local_identity": "203.0.113.1", "remote_identity": "198.51.100.1",
        "local_networks": "10.10.0.0/24", "remote_networks": "10.20.0.0/24", "esp_proposal": "default",
        "enabled": True, "target_host_ids": [host["id"]], "target_group_ids": [], "pre_shared_key": "psk-value-123456",
    })
    assert tunnel.status_code == 201, tunnel.text
    async with SessionLocal() as db:
        db.add(IpsecTunnelDeployment(tunnel_id=uuid.UUID(tunnel.json()["id"]), host_id=uuid.UUID(host["id"]), status="success"))
        await db.commit()
    resp = await client.delete(f"/api/hosts/{host['id']}")
    assert resp.status_code == 409


# ── SEC-10: security-relevant operations are audited ──────────────────

@pytest.mark.asyncio
async def test_account_security_notification_and_vpn_changes_are_audited(client, respx_mock):
    await _make_user(client, "sec10-user", "admin")
    me = await _login("sec10-user", PASSWORD)
    try:
        await me.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "another-long-password-3"})
        await me.post("/api/auth/2fa/setup", json={"password": "wrong-password-xx"})
    finally:
        await me.aclose()
    await client.put("/api/notifications/settings", json={"webhook_enabled": False, "webhook_url": "https://hook.test/TOKEN-MARKER"})
    host = await _host(client, respx_mock)
    await client.post("/api/ipsec/tunnels", json={
        "description": "t", "local_address": "203.0.113.1", "remote_address": "198.51.100.1", "ike_version": "ikev2",
        "proposal": "default", "local_identity": "203.0.113.1", "remote_identity": "198.51.100.1",
        "local_networks": "10.10.0.0/24", "remote_networks": "10.20.0.0/24", "esp_proposal": "default",
        "enabled": True, "target_host_ids": [host["id"]], "target_group_ids": [], "pre_shared_key": "PSK-MARKER-123456",
    })

    seen = {(e.kind, e.action, e.status) for e in await _audit()}
    assert ("auth", "change-password", "accepted") in seen
    assert ("auth", "2fa/setup", "rejected") in seen
    assert ("notification", "edit", "accepted") in seen
    assert ("ipsec_tunnel", "create", "accepted") in seen
    dump = json.dumps([[e.details, e.message, e.entity_name] for e in await _audit()])
    for marker in ("TOKEN-MARKER", "PSK-MARKER", PASSWORD, "another-long-password-3"):
        assert marker not in dump
    assert {e.actor for e in await _audit(kind="auth")} == {"sec10-user"}


@pytest.mark.asyncio
async def test_reader_write_attempt_on_a_sensitive_resource_is_recorded_as_denied(client):
    await _make_user(client, "sec10-reader")
    reader = await _login("sec10-reader", PASSWORD)
    try:
        assert (await reader.put("/api/notifications/settings", json={"webhook_enabled": False})).status_code == 403
    finally:
        await reader.aclose()
    denied = await _audit(kind="notification", status="denied")
    assert denied and denied[0].actor == "sec10-reader"


# ── SEC-13: MFA self-service has an attempt budget ────────────────────

@pytest.mark.asyncio
async def test_2fa_setup_and_disable_are_rate_limited(client):
    await _make_user(client, "sec13-user", "admin")
    me = await _login("sec13-user", PASSWORD)
    try:
        codes = [(await me.post("/api/auth/2fa/setup", json={"password": f"wrong-password-{i}"})).status_code for i in range(7)]
        assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
        # the correct password is refused too while locked: the budget cannot be skipped by guessing on
        assert (await me.post("/api/auth/2fa/setup", json={"password": PASSWORD})).status_code == 429
    finally:
        await me.aclose()


@pytest.mark.asyncio
async def test_2fa_enable_budget_survives_new_setup_and_pending_enrolment_expires(client):
    await _make_user(client, "sec13-enable", "admin")
    me = await _login("sec13-enable", PASSWORD)
    try:
        statuses = []
        for _ in range(7):
            await me.post("/api/auth/2fa/setup", json={"password": PASSWORD})  # fresh enrolment each round
            statuses.append((await me.post("/api/auth/2fa/enable", json={"code": "000000"})).status_code)
        assert statuses[:5] == [401] * 5 and set(statuses[5:]) == {429}
    finally:
        await me.aclose()

    user_id = uuid.uuid4()
    totp_service.save_secret(user_id, "ABCDEFGHIJKLMNOP", pending=True)
    assert totp_service.read_secret(user_id, pending=True)
    path = totp_service._path(user_id, True)
    old = time.time() - totp_service.PENDING_TTL_SECONDS - 5
    import os

    os.utime(path, (old, old))
    assert totp_service.read_secret(user_id, pending=True) is None
    totp_service.delete_secrets(user_id)


# ── SEC-14: VPN approval is mandatory and bound to the secret ─────────

@pytest.mark.asyncio
async def test_vpn_deploy_requires_approval_and_secret_rotation_invalidates_it(client, respx_mock):
    host = await _host(client, respx_mock)
    payload = {
        "description": "t", "local_address": "203.0.113.1", "remote_address": "198.51.100.1", "ike_version": "ikev2",
        "proposal": "default", "local_identity": "203.0.113.1", "remote_identity": "198.51.100.1",
        "local_networks": "10.10.0.0/24", "remote_networks": "10.20.0.0/24", "esp_proposal": "default",
        "enabled": True, "target_host_ids": [host["id"]], "target_group_ids": [], "pre_shared_key": "first-psk-value-1",
    }
    tunnel_id = (await client.post("/api/ipsec/tunnels", json=payload)).json()["id"]
    url = f"/api/ipsec/tunnels/{tunnel_id}"
    calls = respx_mock.route(host="fw-sec.test", method="POST").mock(return_value=httpx.Response(200, json={}))

    assert (await client.post(f"{url}/deploy")).status_code == 422  # no approval at all
    approved = (await client.get(f"{url}/preview")).json()["token"]
    await asyncio.sleep(0.01)
    current = (await client.get("/api/ipsec/tunnels")).json()[0]
    rotated = await client.put(url, json={**payload, "pre_shared_key": "second-psk-value-2", "expected_updated_at": current["updated_at"]})
    assert rotated.status_code == 200, rotated.text
    stale = await client.post(f"{url}/deploy", json={"preview_token": approved})
    assert stale.status_code == 409  # secret-only change invalidates the earlier approval
    assert not calls.called
    preview = (await client.get(f"{url}/preview")).json()
    assert "first-psk-value-1" not in json.dumps(preview) and "second-psk-value-2" not in json.dumps(preview)


# ── SEC-15: validation errors do not echo secrets ─────────────────────

@pytest.mark.asyncio
async def test_validation_errors_do_not_echo_submitted_secrets(client, respx_mock):
    host = await _host(client, respx_mock)
    resp = await client.post("/api/certificates/managed", json={
        "host_id": host["id"], "action": "manual", "description": "c", "private_key": "PRIVATE-KEY-MARKER",
        "crt_payload": "x" * 5, "key_type": "2048", "digest": "sha256", "cert_type": "server_cert",
        "lifetime": 397, "country": "DE",
    })
    assert resp.status_code == 422
    assert "PRIVATE-KEY-MARKER" not in resp.text
    body = resp.json()["detail"]
    assert isinstance(body, list) and all(set(item) == {"loc", "msg", "type"} for item in body)

    bad_login = await client.post("/api/auth/change-password", json={"current_password": "x", "new_password": "SECRET-MK"})
    assert bad_login.status_code == 422 and "SECRET-MK" not in bad_login.text


# ── SEC-16: capability URLs are not readable by read-only users ───────

@pytest.mark.asyncio
async def test_reader_sees_only_the_webhook_host(client):
    url = "https://hooks.example.test:8443/services/T000/B000/SECRET-PATH-TOKEN?key=QUERY-TOKEN"
    assert (await client.put("/api/notifications/settings", json={"webhook_url": url})).status_code == 200
    await _make_user(client, "sec16-reader")
    reader = await _login("sec16-reader", PASSWORD)
    try:
        resp = await reader.get("/api/notifications/settings")
        assert resp.status_code == 200
        assert "TOKEN" not in resp.text and "T000" not in resp.text
        assert resp.json()["webhook_url"].startswith("https://hooks.example.test:8443")
    finally:
        await reader.aclose()
    assert (await client.get("/api/notifications/settings")).json()["webhook_url"] == url  # admin keeps full value
    await client.put("/api/notifications/settings", json={"webhook_url": ""})


# ── HARD-02: same host but other scheme is not same-origin ────────────

@pytest.mark.asyncio
async def test_cross_scheme_origin_is_blocked_on_https_deployments(client, monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", True)
    # The fixture logged in over plain HTTP; an HTTPS deployment only accepts the __Host- cookie name (1.29).
    client.cookies.set("__Host-opnexus-session", client.cookies.get("opnexus-session"))
    blocked = await client.put("/api/logs/settings", json={"retention_days": 5}, headers={"Origin": "http://test"})
    assert blocked.status_code == 403
    allowed = await client.put("/api/logs/settings", json={"retention_days": 5}, headers={"Origin": "https://test"})
    assert allowed.status_code == 200


# ── HARD-03: secret files are created private, atomically, without following links ──

def test_secret_files_are_never_world_readable_and_ignore_planted_symlinks(tmp_path):
    import os
    import stat

    from app.services import secure_files

    old_umask = os.umask(0o022)
    try:
        target = tmp_path / "outside.txt"
        target.write_text("untouched")
        dest = tmp_path / "secrets" / "host.secret"
        dest.parent.mkdir()
        dest.symlink_to(target)  # planted link at the destination
        secure_files.write_private(dest, "s3cret")
    finally:
        os.umask(old_umask)
    assert not dest.is_symlink() and dest.read_text() == "s3cret"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert target.read_text() == "untouched"
    assert [p.name for p in dest.parent.iterdir()] == ["host.secret"]  # no temp files left behind


# ── SEC-04 (intent record): lost replies and crashes keep a recovery trail ──

@pytest.mark.asyncio
async def test_lost_reply_on_first_write_is_flagged_not_retried_blindly(client, respx_mock):
    host = await _host(client, respx_mock)
    rule_id = (await client.post("/api/rules", json=_rule_payload(host["id"]))).json()["id"]
    add = respx_mock.post("https://fw-sec.test/api/firewall/filter/addRule").mock(side_effect=httpx.ReadTimeout("reply lost"))
    dep = (await client.post(f"/api/rules/{rule_id}/deploy")).json()["deployments"][0]
    assert dep["status"] == "revert_failed" and "Ausgang unklar" in dep["message"]
    async with SessionLocal() as db:
        assert (await db.scalars(select(RuleDeployment))).one().opnsense_rule_uuid is None
    # a second deploy must NOT issue another addRule (could create a duplicate)
    again = await client.post(f"/api/rules/{rule_id}/deploy")
    assert again.status_code >= 400 and add.call_count == 1


@pytest.mark.asyncio
async def test_connect_failure_is_a_plain_error_and_may_be_retried(client, respx_mock):
    host = await _host(client, respx_mock)
    rule_id = (await client.post("/api/rules", json=_rule_payload(host["id"]))).json()["id"]
    respx_mock.post("https://fw-sec.test/api/firewall/filter/addRule").mock(side_effect=httpx.ConnectError("refused"))
    dep = (await client.post(f"/api/rules/{rule_id}/deploy")).json()["deployments"][0]
    assert dep["status"] == "error" and dep["revert_deadline"] is None


@pytest.mark.asyncio
async def test_lost_reply_on_update_keeps_armed_rollback(client, respx_mock):
    host = await _host(client, respx_mock)
    rule_id = (await client.post("/api/rules", json=_rule_payload(host["id"]))).json()["id"]
    respx_mock.post("https://fw-sec.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "live-uuid"})
    )
    respx_mock.post("https://fw-sec.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    await client.post(f"/api/rules/{rule_id}/deploy")
    await client.post(f"/api/rules/{rule_id}/confirm")
    rule = next(r for r in (await client.get("/api/rules")).json() if r["id"] == rule_id)
    await client.put(f"/api/rules/{rule_id}", json={**_rule_payload(host["id"]), "destination_port": "2222", "expected_updated_at": rule["updated_at"]})
    respx_mock.post("https://fw-sec.test/api/firewall/filter/setRule/live-uuid").mock(side_effect=httpx.ReadTimeout("reply lost"))
    dep = (await client.post(f"/api/rules/{rule_id}/deploy")).json()["deployments"][0]
    assert dep["status"] == "error" and dep["revert_deadline"] is not None  # sweeper will set the previous payload back


@pytest.mark.asyncio
async def test_process_death_after_intent_leaves_sweepable_or_flagged_row(client, respx_mock):
    """Simulate the process dying inside the remote call: the intent row is already committed."""
    host = await _host(client, respx_mock)
    rule_id = (await client.post("/api/rules", json=_rule_payload(host["id"]))).json()["id"]
    respx_mock.post("https://fw-sec.test/api/firewall/filter/addRule").mock(side_effect=KeyboardInterrupt("process killed"))
    with pytest.raises(BaseException):
        await client.post(f"/api/rules/{rule_id}/deploy")
    async with SessionLocal() as db:
        row = (await db.scalars(select(RuleDeployment))).one()
        assert row.status == "revert_failed" and row.opnsense_rule_uuid is None  # visible, blocks retries, notifies


# ── audit events built from unvalidated request paths ────────────────────

@pytest.mark.asyncio
async def test_junk_path_segments_give_422_not_500(client):
    """Fuzzing found 500s/dropped connections: the audit event copied the raw
    path segment (300 chars, NUL byte) into VARCHAR(36)/Postgres text."""
    for path in (
        "/api/hosts/" + "x" * 300,
        "/api/hosts/%00/services/ntpd/restart",
        "/api/rules/not-a-uuid/deploy",
    ):
        method = "PATCH" if path.count("/") == 3 else "POST"
        resp = await client.request(method, path, json={})
        assert resp.status_code in (404, 422), (path[:40], resp.status_code, resp.text[:100])


@pytest.mark.asyncio
async def test_nul_byte_in_a_text_field_is_a_422_not_a_500(client):
    for path, body in (
        ("/api/groups", {"name": "a\u0000b", "description": "x", "host_ids": []}),
        ("/api/rules", {"description": "a\u0000b", "interface": "lan", "target_host_ids": []}),
    ):
        resp = await client.post(path, json=body)
        assert resp.status_code == 422, (path, resp.status_code, resp.text[:120])
        assert resp.json()["detail"]["code"] == "INVALID_CHARACTERS"
