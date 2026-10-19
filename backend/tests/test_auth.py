import pytest
from starlette.requests import Request

from app.config import settings
from app.routers.auth import client_ip


def _make_request(headers: dict, client_host: str = "9.9.9.9") -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw_headers, "client": (client_host, 12345)})


@pytest.mark.asyncio
async def test_login_success(anon_client):
    resp = await anon_client.post(
        "/api/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True
    assert "opnexus-session" in resp.cookies


@pytest.mark.asyncio
async def test_login_wrong_password(anon_client):
    resp = await anon_client.post(
        "/api/auth/login",
        json={"username": settings.admin_username, "password": "definitely-wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_lockout_after_repeated_failures(anon_client):
    for _ in range(settings.login_max_attempts):
        resp = await anon_client.post(
            "/api/auth/login",
            json={"username": settings.admin_username, "password": "wrong"},
        )
        assert resp.status_code == 401

    resp = await anon_client.post(
        "/api/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password},
    )
    assert resp.status_code == 429


def test_client_ip_ignores_forwarded_header_by_default(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    req = _make_request({"x-forwarded-for": "1.2.3.4"}, client_host="9.9.9.9")
    assert client_ip(req) == "9.9.9.9"


def test_client_ip_uses_forwarded_header_when_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    req = _make_request({"x-forwarded-for": "1.2.3.4, 10.0.0.5"}, client_host="10.0.0.5")
    assert client_ip(req) == "1.2.3.4"


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(anon_client):
    resp = await anon_client.get("/api/hosts")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_reports_authenticated_state(anon_client, client):
    resp = await anon_client.get("/api/auth/me")
    assert resp.json()["authenticated"] is False

    resp = await client.get("/api/auth/me")
    assert resp.json() == {"authenticated": True, "username": settings.admin_username, "role": "admin"}


@pytest.mark.asyncio
async def test_logout_invalidates_session(client):
    resp = await client.get("/api/hosts")
    assert resp.status_code == 200

    await client.post("/api/auth/logout")

    resp = await client.get("/api/hosts")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_health_stays_public_but_docs_require_auth(anon_client):
    resp = await anon_client.get("/api/health")
    assert resp.status_code == 200

    # /docs/redoc/openapi.json used to be public — now require a session
    # like any other endpoint, since fully documenting a firewall-fleet
    # API surface to an anonymous caller is free recon for an attacker.
    for path in ("/docs", "/redoc", "/openapi.json", "/api/openapi.json"):
        resp = await anon_client.get(path)
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_openapi_schema_reachable_once_logged_in(client):
    # The schema lives under /api (the proxy only routes /api to the backend); the
    # interactive Swagger/ReDoc pages are gone (their CDN scripts violate the CSP).
    assert (await client.get("/api/openapi.json")).status_code == 200
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404
