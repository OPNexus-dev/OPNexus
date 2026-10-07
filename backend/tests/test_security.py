"""Transport/security hardening: Origin check, headers, TLS pinning (with a real TLS server)."""
import asyncio
import datetime
import ssl
import tempfile

import httpx
import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.services import opnsense_client, tls_pin_service


def _make_cert(cn="OPNsense.localdomain"):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return cert_pem, key_pem


@pytest_asyncio.fixture
async def tls_server():
    """A local HTTPS server (self-signed cert whose name does not match 127.0.0.1)."""
    cert_pem, key_pem = _make_cert()
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as cf, tempfile.NamedTemporaryFile("w", suffix=".pem") as kf:
        cf.write(cert_pem); cf.flush(); kf.write(key_pem); kf.flush()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cf.name, kf.name)

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            body = b'{"rows": []}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    yield f"https://127.0.0.1:{port}", cert_pem
    server.close()
    await server.wait_closed()


# ── Origin check / headers ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_origin_write_is_blocked(client):
    resp = await client.put("/api/logs/settings", json={"retention_days": 5}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "CROSS_ORIGIN_BLOCKED"


@pytest.mark.asyncio
async def test_same_origin_and_headerless_writes_pass(client):
    ok = await client.put("/api/logs/settings", json={"retention_days": 5}, headers={"Origin": "http://test"})
    assert ok.status_code == 200
    no_header = await client.put("/api/logs/settings", json={"retention_days": 6})
    assert no_header.status_code == 200
    null_origin = await client.put("/api/logs/settings", json={"retention_days": 6}, headers={"Origin": "null"})
    assert null_origin.status_code == 403


@pytest.mark.asyncio
async def test_reads_ignore_origin_and_responses_carry_security_headers(client):
    resp = await client.get("/api/logs/settings", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_multiple_allowed_origins_are_supported(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "frontend_origin", "https://10.0.0.5, https://opnexus.lan/")
    assert settings.allowed_origins == ["https://10.0.0.5", "https://opnexus.lan"]
    for origin in ("https://10.0.0.5", "https://opnexus.lan"):
        resp = await client.put("/api/logs/settings", json={"retention_days": 5}, headers={"Origin": origin})
        assert resp.status_code == 200, origin


# ── TLS pinning ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_returns_fingerprint_without_trusting(client, tls_server):
    url, cert_pem = tls_server
    resp = await client.post("/api/hosts/tls-probe", json={"base_url": url})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sha256"] == tls_pin_service.parse_pem(cert_pem)[0]["sha256"]
    assert data["self_signed"] is True and data["subject"] == "OPNsense.localdomain"


@pytest.mark.asyncio
async def test_probe_rejects_non_https_and_unreachable(client):
    assert (await client.post("/api/hosts/tls-probe", json={"base_url": "http://x.test"})).status_code == 502
    assert (await client.post("/api/hosts/tls-probe", json={"base_url": "https://127.0.0.1:9"})).status_code == 502


@pytest.mark.asyncio
async def test_pinned_host_connects_by_ip_and_by_other_name(client, tls_server):
    url, cert_pem = tls_server
    created = await client.post("/api/hosts", json={"name": "fw-pin", "base_url": url, "api_key": "k", "api_secret": "s", "tls_ca_pem": cert_pem})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["tls_pinned"] is True and body["verify_tls"] is True
    assert body["tls_fingerprint"] == tls_pin_service.parse_pem(cert_pem)[0]["sha256"]

    from app.database import SessionLocal
    from app.models.host import OpnsenseHost
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, body["id"])
        assert await opnsense_client.get_vip_status(host) == {"rows": []}   # addressed by IP
        host.base_url = url.replace("127.0.0.1", "localhost")               # addressed by a name
        assert await opnsense_client.get_vip_status(host) == {"rows": []}


@pytest.mark.asyncio
async def test_wrong_certificate_is_rejected_before_any_request(client, tls_server):
    url, _ = tls_server
    other_pem, _ = _make_cert("somebody-else")
    resp = await client.post("/api/hosts", json={"name": "fw-bad", "base_url": url, "api_key": "k", "api_secret": "s", "tls_ca_pem": other_pem})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "TLS_PIN_MISMATCH"
    junk = await client.post("/api/hosts", json={"name": "fw-junk", "base_url": url, "api_key": "k", "api_secret": "s", "tls_ca_pem": "not a cert"})
    assert junk.json()["detail"]["code"] == "TLS_PEM_INVALID"


@pytest.mark.asyncio
async def test_pin_mismatch_at_request_time_gives_clear_error(client, tls_server):
    url, cert_pem = tls_server
    other_pem, _ = _make_cert("somebody-else")
    from app.models.host import OpnsenseHost
    from app.services import opnsense_secret_store
    import uuid
    host = OpnsenseHost(id=uuid.uuid4(), name="fw-rot", base_url=url, api_key="k", verify_tls=True, tls_ca_pem=other_pem)
    opnsense_secret_store.save_secret(host.id, "s")
    with pytest.raises(opnsense_client.OpnsenseAPIError, match="festgesetzten"):
        await opnsense_client.get_vip_status(host)


@pytest.mark.asyncio
async def test_clear_pin_and_patch_pin(client, tls_server):
    url, cert_pem = tls_server
    created = (await client.post("/api/hosts", json={"name": "fw-pc", "base_url": url, "api_key": "k", "api_secret": "s"})).json()
    assert created["tls_pinned"] is False
    pinned = await client.patch(f"/api/hosts/{created['id']}", json={"tls_ca_pem": cert_pem})
    assert pinned.status_code == 200 and pinned.json()["tls_pinned"] is True
    cleared = await client.patch(f"/api/hosts/{created['id']}", json={"clear_tls_pin": True, "verify_tls": False})
    assert cleared.json()["tls_pinned"] is False and cleared.json()["verify_tls"] is False
