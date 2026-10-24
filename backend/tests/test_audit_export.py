"""Audit export: cursor semantics, wire formats (syslog UDP/TCP, signed webhook), admin-only settings."""
import asyncio
import hashlib
import hmac
import json
import socket
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.audit_export import AuditExportSettings
from app.services import audit_export_service as svc
from app.services import notification_secret_store, url_guard
from tests.test_api_tokens import _login


@pytest.fixture(autouse=True)
def _loopback_allowed(monkeypatch):
    """Diese Tests sprechen echte Sockets auf 127.0.0.1 an; der Zieladress-Schutz selbst steht in
    tests/test_audit_export_guard.py."""
    monkeypatch.setattr(url_guard, "_forbidden", lambda ip: False)


async def _event(n: int, *, age_s: float = 60, status="accepted", details=None) -> AuditEvent:
    async with SessionLocal() as db:
        event = AuditEvent(
            created_at=datetime.now(timezone.utc) - timedelta(seconds=age_s + (10 - n) * 0.001), correlation_id="c", actor="admin", kind="rule",
            action="deploy", status=status, entity_name=f"rule-{n}", message=f"m{n}", details=details or {},
        )
        db.add(event)
        await db.commit()
        return event


async def _configure(**fields):
    async with SessionLocal() as db:
        row = await svc.get_settings(db)
        for key, value in fields.items():
            setattr(row, key, value)
        await db.commit()


async def _run(lag=0):
    async with SessionLocal() as db:
        return await svc.run_once(db, lag_seconds=lag)


async def _state():
    async with SessionLocal() as db:
        return await svc.get_settings(db)


class _UdpReceiver:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2)
        self.port = self.sock.getsockname()[1]

    def receive(self, n):
        return [self.sock.recvfrom(65535)[0].decode() for _ in range(n)]


class _TcpReceiver:
    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.data = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.server.accept()
        while chunk := conn.recv(65535):
            self.data += chunk
        conn.close()


@pytest.fixture(autouse=True)
async def _reset(tmp_path):
    async with SessionLocal() as db:
        await db.execute(AuditExportSettings.__table__.delete())
        await db.commit()
    yield


@pytest.mark.asyncio
async def test_idle_when_nothing_is_enabled():
    await _event(1)
    assert await _run() == 0
    assert (await _state()).cursor_created_at is None


@pytest.mark.asyncio
async def test_first_round_starts_from_now_without_replaying_history():
    await _event(1, age_s=3600)
    udp = _UdpReceiver()
    await _configure(syslog_enabled=True, syslog_host="127.0.0.1", syslog_port=udp.port)
    assert await _run() == 0  # sets the cursor
    assert (await _state()).cursor_created_at is not None
    await asyncio.sleep(0.05)
    await _event(2, age_s=0)
    assert await _run() == 1
    (line,) = udp.receive(1)
    assert '"entity_name": "rule-2"' in line and "rule-1" not in line


@pytest.mark.asyncio
async def test_syslog_udp_line_is_rfc5424_with_severity_by_status_and_no_details():
    udp = _UdpReceiver()
    await _configure(syslog_enabled=True, syslog_host="127.0.0.1", syslog_port=udp.port, cursor_created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    await _event(1, status="accepted", details={"after_payload": {"password": "hunter2"}})
    await _event(2, status="denied")
    assert await _run() == 2
    ok, bad = udp.receive(2)
    assert ok.startswith("<134>1 ") and bad.startswith("<132>1 ")  # local0.info / local0.warning
    assert " opnexus - AUDIT - {" in ok
    body = json.loads(ok.split(" - AUDIT - ", 1)[1])
    assert body["actor"] == "admin" and body["kind"] == "rule" and "details" not in body
    assert "hunter2" not in ok


@pytest.mark.asyncio
async def test_syslog_tcp_uses_octet_counting():
    tcp = _TcpReceiver()
    await _configure(syslog_enabled=True, syslog_host="127.0.0.1", syslog_port=tcp.port, syslog_protocol="tcp",
                     cursor_created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    await _event(1)
    assert await _run() == 1
    tcp.thread.join(timeout=3)
    length, _, rest = tcp.data.partition(b" ")
    assert int(length) == len(rest) and rest.startswith(b"<134>1 ")


@pytest.mark.asyncio
async def test_failure_keeps_the_cursor_and_the_next_round_retries(respx_mock):
    await _configure(webhook_enabled=True, webhook_url="https://hook.test/secret-token",
                     cursor_created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    await _event(1)
    route = respx_mock.post("https://hook.test/secret-token").mock(return_value=httpx.Response(500))
    assert await _run() == 0
    state = await _state()
    assert state.cursor_id is None and "HTTP 500" in state.last_error and "secret-token" not in state.last_error
    route.mock(return_value=httpx.Response(200))
    assert await _run() == 1
    state = await _state()
    assert state.last_error is None and state.last_success_at is not None and state.cursor_id is not None
    assert await _run() == 0  # nothing new


@pytest.mark.asyncio
async def test_webhook_batch_is_signed(respx_mock):
    notification_secret_store.save_secret(svc.SECRET_NAME, "s3cret")
    await _configure(webhook_enabled=True, webhook_url="https://hook.test/in", webhook_signing_secret_set=True,
                     cursor_created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    await _event(1)
    await _event(2)
    route = respx_mock.post("https://hook.test/in").mock(return_value=httpx.Response(200))
    assert await _run() == 2
    request = route.calls.last.request
    assert request.headers["X-OPNexus-Signature"] == "sha256=" + hmac.new(b"s3cret", request.content, hashlib.sha256).hexdigest()
    payload = json.loads(request.content)
    assert [e["entity_name"] for e in payload["events"]] == ["rule-1", "rule-2"] and payload["source"] == "opnexus"


@pytest.mark.asyncio
async def test_missing_signing_secret_fails_closed(respx_mock):
    notification_secret_store.delete_secret(svc.SECRET_NAME)
    await _configure(webhook_enabled=True, webhook_url="https://hook.test/in", webhook_signing_secret_set=True,
                     cursor_created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    await _event(1)
    assert await _run() == 0  # respx would fail the test on any request: nothing was sent
    assert "nicht lesbar" in (await _state()).last_error


@pytest.mark.asyncio
async def test_batches_are_ordered_and_resume_without_gaps_or_duplicates(respx_mock, monkeypatch):
    monkeypatch.setattr(svc, "BATCH", 2)
    await _configure(webhook_enabled=True, webhook_url="https://hook.test/in", cursor_created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    for n in range(1, 6):
        await _event(n)
    route = respx_mock.post("https://hook.test/in").mock(return_value=httpx.Response(200))
    assert [await _run(), await _run(), await _run(), await _run()] == [2, 2, 1, 0]
    names = [e["entity_name"] for call in route.calls for e in json.loads(call.request.content)["events"]]
    assert names == [f"rule-{n}" for n in range(1, 6)]


@pytest.mark.asyncio
async def test_very_fresh_events_wait_for_the_lag():
    udp = _UdpReceiver()
    await _configure(syslog_enabled=True, syslog_host="127.0.0.1", syslog_port=udp.port, cursor_created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    await _event(1, age_s=0.2)
    assert await _run(lag=3) == 0
    assert await _run(lag=0) == 1


# ── Settings API ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_roundtrip_hides_the_secret_and_enabling_starts_from_now(client):
    got = (await client.get("/api/settings/audit-export")).json()
    assert got["syslog_enabled"] is False and got["webhook_signing_secret_set"] is False
    put = await client.put("/api/settings/audit-export", json={
        "syslog_enabled": True, "syslog_host": "127.0.0.1", "syslog_port": 5514, "syslog_protocol": "tcp",
        "webhook_signing_secret": "shh"})
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["syslog_protocol"] == "tcp" and body["webhook_signing_secret_set"] is True and "shh" not in put.text
    assert (await _state()).cursor_created_at is not None
    cleared = (await client.put("/api/settings/audit-export", json={"clear_webhook_signing_secret": True, "syslog_enabled": False})).json()
    assert cleared["webhook_signing_secret_set"] is False
    assert (await _state()).cursor_created_at is None


@pytest.mark.asyncio
async def test_settings_validation(client):
    for body in ({"syslog_protocol": "carrier-pigeon"}, {"syslog_port": 0}, {"webhook_url": "x" * 501}):
        assert (await client.put("/api/settings/audit-export", json=body)).status_code == 422


@pytest.mark.asyncio
async def test_test_endpoint_needs_a_channel_and_reports_delivery_failures(client, respx_mock):
    assert (await client.post("/api/settings/audit-export/test")).status_code == 502
    await client.put("/api/settings/audit-export", json={"webhook_enabled": True, "webhook_url": "https://hook.test/in"})
    respx_mock.post("https://hook.test/in").mock(return_value=httpx.Response(200))
    assert (await client.post("/api/settings/audit-export/test")).json() == {"success": True}


@pytest.mark.asyncio
async def test_only_admins_may_read_or_change_it(client):
    for role in ("operator", "read_only"):
        name = f"exp-{role}"
        assert (await client.post("/api/users", json={"username": name, "password": "export-password-1", "role": role})).status_code == 201
        user = await _login(name, "export-password-1")
        try:
            assert (await user.get("/api/settings/audit-export")).status_code == 403
            assert (await user.put("/api/settings/audit-export", json={"syslog_enabled": True})).status_code == 403
        finally:
            await user.aclose()
