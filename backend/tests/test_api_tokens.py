"""API tokens: creation, scopes, session-only boundaries, revocation, audit."""
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.api_token import ApiToken
from app.services import api_token_service

PASSWORD = settings.admin_password


async def _make(client, scope="read", name="ci", **extra):
    resp = await client.post("/api/tokens", json={"name": name, "scope": scope, "current_password": PASSWORD, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _bearer(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    )


async def _login(username: str, password: str) -> AsyncClient:
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    resp = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return ac


@pytest.mark.parametrize(
    "scope,method,path,denied",
    [
        ("read", "GET", "/api/hosts", False),
        ("read", "POST", "/api/rules", True),
        # Secret-bearing reads need the admin scope; MCP (read-only tools) is open to every scope.
        ("read", "GET", "/api/hosts/abc/backups/config-1.xml", True),
        ("deploy", "GET", "/api/hosts/abc/backups/config-1.xml", True),
        ("admin", "GET", "/api/hosts/abc/backups/config-1.xml", False),
        ("read", "GET", "/api/hosts/abc/backups", False),
        ("read", "GET", "/api/notifications/settings", True),
        ("admin", "GET", "/api/notifications/settings", False),
        ("read", "POST", "/api/mcp", False),
        ("deploy", "POST", "/api/rules", False),
        ("deploy", "DELETE", "/api/aliases/x", False),
        ("deploy", "POST", "/api/hosts", True),
        ("deploy", "POST", "/api/hosts/tls-probe", True),
        ("deploy", "PATCH", "/api/hosts/abc", True),
        ("deploy", "DELETE", "/api/hosts/abc", True),
        ("deploy", "POST", "/api/hosts/abc/maintenance/execute", False),
        ("deploy", "POST", "/api/users", True),
        ("deploy", "PUT", "/api/settings", True),
        ("deploy", "PUT", "/api/notifications/settings", True),
        ("deploy", "PUT", "/api/logs/settings", True),
        ("deploy", "POST", "/api/system/updater/update", True),
        ("admin", "POST", "/api/users", False),
        ("admin", "POST", "/api/hosts", False),
        # Session-only for every scope, reads included.
        ("admin", "GET", "/api/tokens", True),
        ("admin", "POST", "/api/tokens", True),
        ("read", "GET", "/api/tokens", True),
        ("admin", "POST", "/api/auth/change-password", True),
        ("admin", "POST", "/api/auth/2fa/disable", True),
    ],
)
def test_scope_matrix(scope, method, path, denied):
    assert api_token_service.denies(scope, method, path) is denied


@pytest.mark.asyncio
async def test_create_needs_password_and_shows_token_once(client):
    bad = await client.post("/api/tokens", json={"name": "x", "current_password": "wrong"})
    assert bad.status_code == 401
    assert bad.json()["detail"]["code"] == "AKTUELLES_PASSWORT_IST_FALSCH"

    created = await _make(client, expires_in_days=30)
    assert created["token"].startswith("opx_")
    assert created["scope"] == "read"
    assert created["prefix"] == created["token"][:10]
    assert created["expires_at"] is not None

    listed = (await client.get("/api/tokens")).json()
    assert [t["id"] for t in listed] == [created["id"]]
    assert "token" not in listed[0] and created["token"] not in str(listed)


@pytest.mark.asyncio
async def test_only_the_hash_is_stored(client):
    created = await _make(client)
    async with SessionLocal() as db:
        row = await db.scalar(select(ApiToken))
    assert row.token_hash == api_token_service.hash_token(created["token"])
    assert created["token"] not in (row.token_hash, row.prefix)


@pytest.mark.asyncio
async def test_bearer_authenticates_without_a_session(client, anon_client):
    created = await _make(client)
    async with _bearer(created["token"]) as api:
        assert (await api.get("/api/hosts")).status_code == 200
        me = (await api.get("/api/auth/me")).json()
    assert me["authenticated"] and me["via"] == "token" and me["scope"] == "read"
    assert (await anon_client.get("/api/hosts")).status_code == 401


@pytest.mark.asyncio
async def test_read_scope_cannot_write(client):
    created = await _make(client, "read")
    async with _bearer(created["token"]) as api:
        resp = await api.post("/api/groups", json={"name": "g"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "API_TOKEN_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_deploy_scope_writes_fleet_objects_but_not_access_control(client):
    created = await _make(client, "deploy")
    async with _bearer(created["token"]) as api:
        assert (await api.post("/api/groups", json={"name": "from-ci"})).status_code == 201
        for method, path, body in [
            ("POST", "/api/users", {"username": "evil", "password": "evil-password-123", "role": "admin"}),
            ("POST", "/api/hosts", {}),
        ]:
            resp = await api.request(method, path, json=body)
            assert resp.status_code == 403, (path, resp.text)
            assert resp.json()["detail"]["code"] == "API_TOKEN_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_tokens_cannot_manage_tokens_or_accounts(client):
    created = await _make(client, "admin")
    async with _bearer(created["token"]) as api:
        for method, path in [("GET", "/api/tokens"), ("POST", "/api/tokens"), ("DELETE", f"/api/tokens/{created['id']}"),
                             ("POST", "/api/auth/change-password")]:
            resp = await api.request(method, path, json={"name": "x", "current_password": PASSWORD})
            assert resp.status_code == 403, (path, resp.text)
            assert resp.json()["detail"]["code"] == "API_TOKEN_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_admin_scope_acts_as_admin_and_is_audited_with_token_name(client):
    created = await _make(client, "admin", name="terraform")
    async with _bearer(created["token"]) as api:
        resp = await api.post("/api/users", json={"username": "viaapi", "password": "via-api-password-1", "role": "read_only"})
    assert resp.status_code == 201, resp.text
    items = (await client.get("/api/audit", params={"kind": "user"})).json()["items"]
    assert any(e["actor"] == "admin (API: terraform)" for e in items), items


@pytest.mark.asyncio
async def test_bad_or_expired_token_is_401_and_never_falls_back_to_the_cookie(client):
    # `client` carries a valid session cookie; a broken bearer header must still fail.
    resp = await client.get("/api/hosts", headers={"Authorization": "Bearer opx_doesnotexist"})
    assert resp.status_code == 401

    created = await _make(client)
    async with SessionLocal() as db:
        await db.execute(update(ApiToken).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await db.commit()
    async with _bearer(created["token"]) as api:
        assert (await api.get("/api/hosts")).status_code == 401


@pytest.mark.asyncio
async def test_non_token_bearer_values_are_ignored(client):
    # Only opx_ values are API tokens; anything else leaves normal cookie auth alone.
    resp = await client.get("/api/hosts", headers={"Authorization": "Bearer something-else"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_revoked_token_stops_working(client):
    created = await _make(client)
    async with _bearer(created["token"]) as api:
        assert (await api.get("/api/hosts")).status_code == 200
        assert (await client.delete(f"/api/tokens/{created['id']}")).status_code == 204
        assert (await api.get("/api/hosts")).status_code == 401
    assert (await client.delete(f"/api/tokens/{created['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_last_used_is_recorded(client):
    created = await _make(client)
    async with _bearer(created["token"]) as api:
        await api.get("/api/hosts")
    listed = (await client.get("/api/tokens")).json()[0]
    assert listed["last_used_at"] is not None


@pytest.mark.asyncio
async def test_read_only_user_gets_only_read_tokens_and_sees_only_their_own(client):
    resp = await client.post("/api/users", json={"username": "tokviewer", "password": "viewer-password-123", "role": "read_only"})
    assert resp.status_code == 201, resp.text
    admin_token = await _make(client, "admin", name="admins")
    viewer = await _login("tokviewer", "viewer-password-123")
    try:
        denied = await viewer.post("/api/tokens", json={"name": "x", "scope": "deploy", "current_password": "viewer-password-123"})
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "API_TOKEN_SCOPE_TOO_HIGH"
        ok = await viewer.post("/api/tokens", json={"name": "grafana", "scope": "read", "current_password": "viewer-password-123"})
        assert ok.status_code == 201, ok.text
        mine = (await viewer.get("/api/tokens")).json()
        assert [t["name"] for t in mine] == ["grafana"]
        # Not someone else's token.
        assert (await viewer.delete(f"/api/tokens/{admin_token['id']}")).status_code == 404
    finally:
        await viewer.aclose()
    # The admin sees both and may revoke the viewer's.
    all_tokens = {t["name"]: t for t in (await client.get("/api/tokens")).json()}
    assert set(all_tokens) == {"admins", "grafana"} and all_tokens["grafana"]["owner"] == "tokviewer"
    assert (await client.delete(f"/api/tokens/{all_tokens['grafana']['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_demoted_owner_makes_the_token_read_only(client):
    users = await client.post("/api/users", json={"username": "demotee", "password": "demotee-password-1", "role": "admin"})
    uid = users.json()["id"]
    owner = await _login("demotee", "demotee-password-1")
    try:
        created = (await owner.post("/api/tokens", json={"name": "t", "scope": "admin", "current_password": "demotee-password-1"})).json()
    finally:
        await owner.aclose()
    assert (await client.patch(f"/api/users/{uid}/role", json={"role": "read_only"})).status_code == 200
    async with _bearer(created["token"]) as api:
        resp = await api.post("/api/groups", json={"name": "g"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "READ_ONLY_ACCESS"


@pytest.mark.asyncio
async def test_password_reset_and_user_delete_revoke_tokens(client):
    resp = await client.post("/api/users", json={"username": "resetme", "password": "resetme-password-1", "role": "admin"})
    uid = resp.json()["id"]
    owner = await _login("resetme", "resetme-password-1")
    try:
        t1 = (await owner.post("/api/tokens", json={"name": "a", "current_password": "resetme-password-1"})).json()
    finally:
        await owner.aclose()
    async with _bearer(t1["token"]) as api:
        assert (await api.get("/api/hosts")).status_code == 200
        assert (await client.post(f"/api/users/{uid}/reset-password", json={"new_password": "brand-new-password-1"})).status_code == 200
        assert (await api.get("/api/hosts")).status_code == 401
    assert (await client.delete(f"/api/users/{uid}")).status_code == 204
    async with SessionLocal() as db:
        assert (await db.scalars(select(ApiToken))).all() == []


@pytest.mark.asyncio
async def test_token_count_is_limited(client, monkeypatch):
    monkeypatch.setattr(api_token_service, "MAX_TOKENS_PER_USER", 2)
    await _make(client, name="1")
    await _make(client, name="2")
    resp = await client.post("/api/tokens", json={"name": "3", "current_password": PASSWORD})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "API_TOKEN_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_wrong_password_attempts_are_throttled(client):
    codes = []
    for _ in range(15):
        codes.append((await client.post("/api/tokens", json={"name": "x", "current_password": "nope"})).status_code)
    assert 429 in codes


@pytest.mark.asyncio
async def test_openapi_schema_is_served_behind_auth_with_bearer_scheme(client, anon_client):
    assert (await anon_client.get("/api/openapi.json")).status_code == 401
    schema = (await client.get("/api/openapi.json")).json()
    assert schema["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert "/api/tokens" in schema["paths"] and "/api/hosts" in schema["paths"]
    created = await _make(client)
    async with _bearer(created["token"]) as api:
        assert (await api.get("/api/openapi.json")).status_code == 200
    # No interactive docs page (its CDN scripts would be blocked by the CSP anyway).
    assert (await client.get("/docs")).status_code != 200
