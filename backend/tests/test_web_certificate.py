"""Own web certificate of the proxy through the updater (/api/system/web-certificate)."""
import json

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.audit import AuditEvent
from tests.test_users import _login_as

UPDATER = "http://updater.test:8080"
CERT = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\nMIIBsecretKEYmaterial\n-----END PRIVATE KEY-----\n"
INFO = {"subject": "CN=lab", "issuer": "CN=lab", "not_after": "Jan  1 00:00:00 2030 GMT", "days_left": 900, "names": ["IP:10.0.0.5"],
        "fingerprint": "AA:BB", "uncovered": ["opnexus.lan"]}


@pytest.fixture(autouse=True)
def _updater(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("t" * 40)
    monkeypatch.setattr(settings, "updater_url", UPDATER)
    monkeypatch.setattr(settings, "updater_token_file", str(token))


async def _audit():
    async with SessionLocal() as db:
        rows = (await db.execute(select(AuditEvent).where(AuditEvent.action.like("web_certificate_%")).order_by(AuditEvent.id))).scalars().all()
        return [(r.action, r.message, json.dumps(r.details)) for r in rows]


@pytest.mark.asyncio
async def test_status_passes_the_updaters_answer_through(client, respx_mock):
    route = respx_mock.get(f"{UPDATER}/cert/status").mock(return_value=httpx.Response(200, json={"status": INFO, "error": None, "job": None, "log": [], "ha_role": None}))
    body = (await client.get("/api/system/web-certificate")).json()
    assert body["available"] is True and body["status"]["fingerprint"] == "AA:BB" and route.called
    assert route.calls[0].request.headers["authorization"] == "Bearer " + "t" * 40


@pytest.mark.asyncio
async def test_status_without_the_updater_is_unavailable_not_an_error(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "updater_token_file", str(tmp_path / "missing"))
    body = (await client.get("/api/system/web-certificate")).json()
    assert body == {"available": False, "reason": "not_enabled"}


@pytest.mark.asyncio
async def test_status_when_the_updater_does_not_answer(client, respx_mock):
    respx_mock.get(f"{UPDATER}/cert/status").mock(side_effect=httpx.ConnectError("down"))
    assert (await client.get("/api/system/web-certificate")).json() == {"available": False, "reason": "unreachable"}


@pytest.mark.asyncio
async def test_everything_is_for_admins_only(client, respx_mock):
    await client.post("/api/users", json={"username": "viewer-cert", "password": "password-123456", "role": "read_only"})
    viewer = await _login_as("viewer-cert", "password-123456")
    try:
        body = {"cert": CERT, "key": KEY}
        assert (await viewer.get("/api/system/web-certificate")).status_code == 403
        assert (await viewer.post("/api/system/web-certificate/check", json=body)).status_code == 403
        assert (await viewer.post("/api/system/web-certificate", json=body)).status_code == 403
        assert (await viewer.delete("/api/system/web-certificate")).status_code == 403
    finally:
        await viewer.aclose()
    assert len(respx_mock.calls) == 0     # nothing reached the updater
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=__import__("app.main", fromlist=["app"]).app), base_url="http://test")
    try:
        assert (await anon.get("/api/system/web-certificate")).status_code in (401, 403)
    finally:
        await anon.aclose()


@pytest.mark.asyncio
async def test_check_forwards_and_returns_the_details(client, respx_mock):
    route = respx_mock.post(f"{UPDATER}/cert/check").mock(return_value=httpx.Response(200, json={"certificate": INFO}))
    resp = await client.post("/api/system/web-certificate/check", json={"cert": CERT, "key": KEY})
    assert resp.status_code == 200 and resp.json()["certificate"]["uncovered"] == ["opnexus.lan"]
    assert json.loads(route.calls[0].request.content) == {"cert": CERT, "key": KEY}


@pytest.mark.asyncio
async def test_install_checks_first_starts_the_job_and_audits_without_the_key(client, respx_mock):
    respx_mock.post(f"{UPDATER}/cert/check").mock(return_value=httpx.Response(200, json={"certificate": INFO}))
    inst = respx_mock.post(f"{UPDATER}/cert/install").mock(return_value=httpx.Response(202, json={"job": {"id": "j1", "verb": "install", "state": "running"}}))
    resp = await client.post("/api/system/web-certificate", json={"cert": CERT, "key": KEY})
    assert resp.status_code == 202 and resp.json()["job"]["id"] == "j1" and inst.called
    events = await _audit()
    assert events and events[-1][0] == "web_certificate_install"
    text = " ".join(e[1] + e[2] for e in events)
    async with SessionLocal() as db:      # the generic request entries (method and path only) must not carry the key either
        every = " ".join(f"{r.message} {r.details}" for r in (await db.execute(select(AuditEvent).where(AuditEvent.entity_id == "web-certificate"))).scalars())
    assert "secretKEYmaterial" not in every and "MIIBfake" not in every
    assert "CN=lab" in text and "AA:BB" in text and "opnexus.lan" in text
    assert "secretKEYmaterial" not in text and "PRIVATE KEY" not in text and "MIIBfake" not in text


@pytest.mark.asyncio
async def test_a_refused_certificate_gives_the_reason_and_no_audit_entry_and_no_install(client, respx_mock):
    before = len(await _audit())
    respx_mock.post(f"{UPDATER}/cert/check").mock(return_value=httpx.Response(400, json={"error": "invalid_certificate", "detail": "Das Zertifikat ist abgelaufen."}))
    inst = respx_mock.post(f"{UPDATER}/cert/install").mock(return_value=httpx.Response(202, json={"job": {}}))
    resp = await client.post("/api/system/web-certificate", json={"cert": CERT, "key": KEY})
    detail = resp.json()["detail"]
    assert resp.status_code == 400 and detail["code"] == "WEB_CERT_INVALID" and detail["params"]["reason"] == "Das Zertifikat ist abgelaufen."
    assert not inst.called and len(await _audit()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,status,code", [
    ("not_pem", 400, "WEB_CERT_NOT_PEM"), ("too_large", 413, "WEB_CERT_TOO_LARGE"), ("ha_standby", 409, "WEB_CERT_STANDBY"),
    ("busy", 409, "WEB_CERT_BUSY"), ("not_release_mode", 409, "WEB_CERT_NOT_RELEASE"), ("something_new", 409, "UPDATER_REJECTED")])
async def test_updater_refusals_become_stable_error_codes(client, respx_mock, answer, status, code):
    respx_mock.post(f"{UPDATER}/cert/check").mock(return_value=httpx.Response(200, json={"certificate": INFO}))
    respx_mock.post(f"{UPDATER}/cert/install").mock(return_value=httpx.Response(status if status != 400 else 400, json={"error": answer}))
    resp = await client.post("/api/system/web-certificate", json={"cert": CERT, "key": KEY})
    assert resp.status_code == status and resp.json()["detail"]["code"] == code


@pytest.mark.asyncio
async def test_install_without_the_updater_is_503(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "updater_token_file", str(tmp_path / "missing"))
    resp = await client.post("/api/system/web-certificate", json={"cert": CERT, "key": KEY})
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "UPDATER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_remove_starts_the_job_and_is_audited(client, respx_mock):
    respx_mock.post(f"{UPDATER}/cert/remove").mock(return_value=httpx.Response(202, json={"job": {"id": "j2", "verb": "remove", "state": "running"}}))
    resp = await client.delete("/api/system/web-certificate")
    assert resp.status_code == 202 and resp.json()["job"]["verb"] == "remove"
    assert (await _audit())[-1][0] == "web_certificate_remove"


@pytest.mark.asyncio
async def test_oversized_or_empty_bodies_are_refused_by_the_api(client, respx_mock):
    for body in ({"cert": "", "key": KEY}, {"cert": CERT}, {"cert": CERT, "key": "x" * 50000}):
        assert (await client.post("/api/system/web-certificate/check", json=body)).status_code == 422
    assert len(respx_mock.calls) == 0


@pytest.mark.asyncio
async def test_each_endpoint_checks_the_role_itself_not_only_the_middleware(respx_mock):
    """The middleware already blocks writes for read-only accounts; the handlers must not rely on that alone."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.routers import system
    viewer = SimpleNamespace(role="read_only", username="v")
    body = system.WebCertPayload(cert=CERT, key=KEY)
    for call in (lambda: system.web_certificate(user=viewer), lambda: system.web_certificate_check(body, user=viewer),
                 lambda: system.web_certificate_install(body, db=None, user=viewer), lambda: system.web_certificate_remove(db=None, user=viewer)):
        with pytest.raises(HTTPException) as err:
            await call()
        assert err.value.status_code == 403
    assert len(respx_mock.calls) == 0
