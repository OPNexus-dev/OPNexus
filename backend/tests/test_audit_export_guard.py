"""Zieladress-Schutz für den Audit-Export, Offsite-S3 und den Update-Feed (1.29-Rest): Loopback, Link-Local und
Metadaten-Dienst sind als Ziel nie erlaubt, auch nicht über Weiterleitungen oder DNS-Antworten."""
import pytest

from app.models.audit_export import AuditExportSettings
from app.services import audit_export_service as svc
from app.services import s3_client, update_check_service, url_guard


def _row(**kw) -> AuditExportSettings:
    row = AuditExportSettings(id=1)
    for key, value in kw.items():
        setattr(row, key, value)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "169.254.169.254", "::1", "0.0.0.0"])
async def test_syslog_refuses_forbidden_targets(host):
    row = _row(syslog_host=host, syslog_port=514, syslog_protocol="udp")
    with pytest.raises(svc.ExportError, match="Syslog-Ziel nicht erlaubt"):
        await svc.send_syslog(row, [{"time": "t", "status": "accepted"}])


@pytest.mark.asyncio
async def test_syslog_refuses_a_name_that_resolves_to_loopback(monkeypatch):
    async def fake(host, port=None):
        return ["203.0.113.5", "127.0.0.1"]
    monkeypatch.setattr(url_guard, "resolve_host", fake)
    with pytest.raises(svc.ExportError, match="nicht erlaubt"):
        await svc.send_syslog(_row(syslog_host="logs.example.test", syslog_port=514, syslog_protocol="tcp"),
                              [{"time": "t", "status": "accepted"}])


@pytest.mark.asyncio
async def test_syslog_connects_to_the_checked_address_not_the_name(monkeypatch):
    seen = {}

    async def fake(host, port=None):
        return ["203.0.113.5"]

    def send(row, lines, addresses):
        seen["addresses"] = addresses

    monkeypatch.setattr(url_guard, "resolve_host", fake)
    monkeypatch.setattr(svc, "_send_syslog_sync", send)
    await svc.send_syslog(_row(syslog_host="logs.example.test", syslog_port=514, syslog_protocol="tcp"),
                          [{"time": "t", "status": "accepted"}])
    assert seen["addresses"] == ["203.0.113.5"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1:9/x", "http://localhost/x", "http://169.254.169.254/latest", "ftp://example.test/x"])
async def test_webhook_refuses_forbidden_targets(url):
    with pytest.raises(svc.ExportError, match="Webhook-Ziel nicht erlaubt"):
        await svc.send_webhook(_row(webhook_url=url), [])


@pytest.mark.asyncio
async def test_webhook_refuses_a_rebound_name(monkeypatch):
    async def fake(host, port=None):
        return ["127.0.0.1"]
    monkeypatch.setattr(url_guard, "resolve_host", fake)
    with pytest.raises(svc.ExportError, match="nicht erlaubt"):
        await svc.send_webhook(_row(webhook_url="https://hooks.example.test/a"), [])


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1:9000", "https://localhost:9000", "https://169.254.169.254"])
def test_s3_endpoint_must_not_be_local(endpoint):
    with pytest.raises(s3_client.S3Error, match="Ungültiger Endpunkt"):
        s3_client.S3Client(endpoint=endpoint, region="", bucket="b", access_key="a", secret_key="s")


def test_s3_private_lan_endpoint_is_fine():
    s3_client.S3Client(endpoint="https://10.0.0.5:9000", region="", bucket="b", access_key="a", secret_key="s")


@pytest.mark.asyncio
async def test_s3_connection_is_refused_when_the_name_resolves_to_loopback(monkeypatch):
    async def fake(host, port=None):
        return ["127.0.0.1"]
    monkeypatch.setattr(url_guard, "resolve_host", fake)
    client = s3_client.S3Client(endpoint="https://s3.example.test", region="", bucket="b", access_key="a", secret_key="s")
    with pytest.raises(s3_client.S3Error, match="Zieladresse nicht erlaubt"):
        await client.put("k", b"x")


@pytest.mark.asyncio
async def test_update_feed_refuses_a_rebound_name(monkeypatch):
    async def fake(host, port=None):
        return ["169.254.169.254"]
    monkeypatch.setattr(url_guard, "resolve_host", fake)
    with pytest.raises(Exception):
        await update_check_service._fetch("https://updates.example.test/latest.json")


@pytest.mark.asyncio
async def test_update_feed_is_capped_while_reading_not_afterwards(monkeypatch):
    """Eine endlose Antwort wird nach _MAX_BYTES abgebrochen, ohne dass sie komplett im Speicher landet."""
    import asyncio

    sent = {"bytes": 0}

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ntransfer-encoding: chunked\r\n\r\n")
        chunk = b"x" * 65536
        try:
            for _ in range(2000):  # ~130 MB angeboten
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
                sent["bytes"] += len(chunk)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(url_guard, "_forbidden", lambda ip: False)
    try:
        with pytest.raises(ValueError, match="too large"):
            await update_check_service._fetch(f"http://127.0.0.1:{port}/latest.json")
    finally:
        server.close()
    assert sent["bytes"] < 10_000_000
