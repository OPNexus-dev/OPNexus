"""1.27 prerequisite hardening: __Host- session cookie (with soft migration) and
the Sec-Fetch-Site check for cookie-authenticated /api requests."""
import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.config import settings
from app.main import app
from app.routers.auth import COOKIE_NAME, SECURE_COOKIE_NAME

PW = "tokpass-12345678"


def _https_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def _login(ac: AsyncClient) -> None:
    resp = await ac.post("/api/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
    assert resp.status_code == 200, resp.text


def _set_cookies(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


@pytest.mark.asyncio
async def test_secure_login_sets_host_prefixed_cookie_and_drops_legacy(monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", True)
    async with _https_client() as ac:
        resp = await ac.post("/api/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        assert resp.status_code == 200
        cookies = _set_cookies(resp)
        new = [c for c in cookies if c.startswith(f"{SECURE_COOKIE_NAME}=")]
        assert new and "Secure" in new[0] and "Path=/" in new[0] and "HttpOnly" in new[0] and "Domain" not in new[0]
        assert any(c.startswith(f'{COOKIE_NAME}=""') or c.startswith(f"{COOKIE_NAME}=;") or "Max-Age=0" in c
                   for c in cookies if c.startswith(f"{COOKIE_NAME}="))
        assert (await ac.get("/api/hosts")).status_code == 200


@pytest.mark.asyncio
async def test_plain_name_cookie_without_https():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        assert any(c.startswith(f"{COOKIE_NAME}=") for c in _set_cookies(resp))
        assert not any(c.startswith(SECURE_COOKIE_NAME) for c in _set_cookies(resp))


@pytest.mark.asyncio
async def test_legacy_cookie_is_migrated_not_logged_out(monkeypatch):
    # A session issued before the upgrade (plain cookie name) ...
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await _login(ac)
        legacy = ac.cookies.get(COOKIE_NAME)
    assert legacy
    monkeypatch.setattr(settings, "cookie_secure", True)
    async with _https_client() as ac:
        ac.cookies.set(COOKIE_NAME, legacy)
        assert (await ac.get("/api/hosts")).status_code == 200  # still accepted
        me = await ac.get("/api/auth/me")
        assert me.json()["authenticated"] is True
        moved = [c for c in _set_cookies(me) if c.startswith(f"{SECURE_COOKIE_NAME}={legacy}")]
        assert moved and "Secure" in moved[0]


@pytest.mark.asyncio
async def test_host_cookie_wins_over_tossed_plain_cookie(monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", True)
    async with _https_client() as ac:
        await _login(ac)
        real = ac.cookies.get(SECURE_COOKIE_NAME)
    async with _https_client() as ac:
        # A script on another port may set a plain-name cookie; it is ignored ...
        ac.cookies.set(SECURE_COOKIE_NAME, real)
        ac.cookies.set(COOKIE_NAME, "attacker-token")
        assert (await ac.get("/api/hosts")).status_code == 200
    async with _https_client() as ac:
        # ... and a bad __Host- cookie never falls back to the plain one.
        ac.cookies.set(SECURE_COOKIE_NAME, "garbage")
        ac.cookies.set(COOKIE_NAME, real)
        assert (await ac.get("/api/hosts")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("site,expected", [
    ("same-site", 403), ("cross-site", 403), ("Cross-Site", 403),
    ("same-origin", 200), ("none", 200), (None, 200),
])
async def test_sec_fetch_site_on_cookie_requests(client, site, expected):
    headers = {"Sec-Fetch-Site": site} if site else {}
    resp = await client.get("/api/hosts", headers=headers)
    assert resp.status_code == expected, resp.text
    if expected == 403:
        assert resp.json()["detail"]["code"] == "CROSS_SITE_REQUEST_BLOCKED"


@pytest.mark.asyncio
async def test_sec_fetch_same_site_allowed_only_for_configured_frontend(client, monkeypatch):
    monkeypatch.setattr(settings, "frontend_origin", "https://opnexus.example:3100")
    ok = await client.get("/api/hosts", headers={"Sec-Fetch-Site": "same-site", "Origin": "https://opnexus.example:3100"})
    assert ok.status_code == 200
    slot = await client.get("/api/hosts", headers={"Sec-Fetch-Site": "same-site", "Origin": "https://opnexus.example:9441"})
    assert slot.status_code == 403
    # Writes from a slot page are refused as well (before auth even runs).
    write = await client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "same-site", "Origin": "https://opnexus.example:9441"})
    assert write.status_code == 403


@pytest.mark.asyncio
async def test_sec_fetch_does_not_touch_token_requests(client, anon_client):
    created = (await client.post("/api/tokens", json={"name": "sfs", "scope": "read", "current_password": settings.admin_password})).json()
    resp = await anon_client.get("/api/hosts", headers={"Authorization": f"Bearer {created['token']}", "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_sec_fetch_exempts_sso_callback(client):
    resp = await client.get("/api/auth/oidc/callback?code=x&state=y", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert resp.status_code != 403 or resp.json().get("detail", {}).get("code") != "CROSS_SITE_REQUEST_BLOCKED"


@pytest.mark.asyncio
async def test_startup_refused_when_slot_origin_is_a_frontend_origin(monkeypatch):
    monkeypatch.setattr(settings, "frontend_origin", "https://opnexus.example, https://opnexus.example:9441")
    monkeypatch.setattr(settings, "opnexus_gui_sites", "https://opnexus.example:9441,https://opnexus.example:9442")
    assert settings.gui_origin_conflicts() == ["https://opnexus.example:9441"]
    called = []

    async def _no_migrate():
        called.append(1)
    monkeypatch.setattr(main.migrate, "run", _no_migrate)
    with pytest.raises(RuntimeError):
        async with main.lifespan(app):
            pass
    assert not called
    monkeypatch.setattr(settings, "opnexus_gui_sites", "https://opnexus.example:9442")
    assert settings.gui_origin_conflicts() == []
