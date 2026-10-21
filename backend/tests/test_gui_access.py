"""GUI access (1.27): settings, one-time tickets and the gateway's internal redeem/revalidate/end API."""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.audit import AuditEvent
from app.models.gui_access import GuiSession, GuiTicket
from app.models.host import OpnsenseHost
from app.routers.auth import SECURE_COOKIE_NAME

GW_TOKEN = "gateway-token-" + "x" * 40
SITES = "https://opnexus.example:9441,https://opnexus.example:9442,https://opnexus.example:9443"
PW = "gui-user-password-1"


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


@pytest.fixture
def gui_env(monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", True)
    monkeypatch.setattr(settings, "gui_gateway_token", GW_TOKEN)
    monkeypatch.setattr(settings, "opnexus_gui_sites", SITES)


async def _https_login(username: str, password: str) -> AsyncClient:
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="https://opnexus.example")
    resp = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    assert ac.cookies.get(SECURE_COOKIE_NAME)
    return ac


@pytest_asyncio.fixture
async def admin(gui_env):
    ac = await _https_login(settings.admin_username, settings.admin_password)
    yield ac
    await ac.aclose()


async def _user(admin: AsyncClient, name: str, role: str) -> AsyncClient:
    resp = await admin.post("/api/users", json={"username": name, "password": PW, "role": role})
    if resp.status_code == 409 or (resp.status_code == 400 and "BENUTZERNAME" in resp.text):
        users = (await admin.get("/api/users")).json()
        uid = next(u["id"] for u in users if u["username"] == name)
        await admin.patch(f"/api/users/{uid}/role", json={"role": role})
    else:
        assert resp.status_code == 201, resp.text
    return await _https_login(name, PW)


async def _host(name="fw-gui", base_url="https://fw-gui.test", verify_tls=True, enabled=True) -> uuid.UUID:
    async with SessionLocal() as db:
        host = OpnsenseHost(name=name, base_url=base_url, api_key="apikey-must-not-leak", verify_tls=verify_tls,
                            enabled=enabled, tls_ca_pem="-----BEGIN CERTIFICATE-----\nPIN\n-----END CERTIFICATE-----\n")
        db.add(host)
        await db.commit()
        return host.id


async def _enable(admin: AsyncClient, **over) -> dict:
    body = {"enabled": True, "idle_timeout_minutes": 15, "absolute_timeout_minutes": 60, "slot_count": 4, **over}
    resp = await admin.put("/api/settings/gui-access", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ticket_from(url: str) -> str:
    return url.split("?t=", 1)[1]


async def _internal(path: str, body: dict, token: str = GW_TOKEN):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://backend:8000") as ac:
        return await ac.post(f"/internal/gui/{path}", json=body, headers={"X-Onx-Gateway-Token": token})


async def _redeem(admin: AsyncClient, url: str, slot: int | None = None, session_hash: str | None = None):
    return await _internal("redeem", {
        "ticket_hash": sha(_ticket_from(url)), "slot": slot or int(url.split(":")[2].split("/")[0]),
        "session_hash": session_hash or sha(admin.cookies.get(SECURE_COOKIE_NAME)), "client_ip": "192.0.2.10",
    })


# ── settings ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_default_off_and_switchable(admin):
    data = (await admin.get("/api/settings/gui-access")).json()
    assert data["enabled"] is False and data["available"] is True and data["slots"] == [9441, 9442, 9443]
    assert (await _enable(admin, slot_count=2))["slots"] == [9441, 9442]
    bad = await admin.put("/api/settings/gui-access", json={"enabled": True, "idle_timeout_minutes": 90,
                                                              "absolute_timeout_minutes": 60, "slot_count": 2})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_cannot_enable_without_https_cookies(admin, monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as plain:
        await plain.post("/api/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        resp = await plain.put("/api/settings/gui-access", json={"enabled": True, "idle_timeout_minutes": 15,
                                                                  "absolute_timeout_minutes": 60, "slot_count": 4})
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "GUI_GATEWAY_NOT_CONFIGURED"
        assert "cookie_secure" in (await plain.get("/api/settings/gui-access")).json()["unavailable_reasons"]


@pytest.mark.asyncio
async def test_settings_admin_only(admin):
    op = await _user(admin, "gui-op-settings", "operator")
    assert (await op.get("/api/settings/gui-access")).status_code == 403
    assert (await op.put("/api/settings/gui-access", json={"enabled": False, "idle_timeout_minutes": 15,
                                                             "absolute_timeout_minutes": 60, "slot_count": 4})).status_code == 403


# ── tickets ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ticket_issued_and_only_hash_stored(admin):
    host_id = await _host()
    await _enable(admin)
    resp = await admin.post(f"/api/hosts/{host_id}/gui-ticket")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["url"].startswith("https://opnexus.example:9441/__onx/redeem?t=") and data["expires_in"] == 30
    raw = _ticket_from(data["url"])
    async with SessionLocal() as db:
        ticket = (await db.scalars(select(GuiTicket))).one()
        assert ticket.ticket_hash == sha(raw) and raw not in repr(ticket.__dict__)
        assert ticket.session_hash == sha(admin.cookies.get(SECURE_COOKIE_NAME))
        assert (ticket.expires_at - ticket.created_at) == timedelta(seconds=30)
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "gui_access"))).all()
        assert [e.action for e in events] == ["ticket"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role,code", [("operator", "ADMIN_REQUIRED"), ("read_only", "READ_ONLY_ACCESS")])
async def test_ticket_refused_for_non_admins(admin, role, code):
    host_id = await _host()
    await _enable(admin)
    other = await _user(admin, f"gui-{role}", role)
    resp = await other.post(f"/api/hosts/{host_id}/gui-ticket")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == code


@pytest.mark.asyncio
async def test_ticket_refused_for_api_tokens(admin, anon_client):
    host_id = await _host()
    await _enable(admin)
    token = (await admin.post("/api/tokens", json={"name": "gui", "scope": "admin", "current_password": settings.admin_password})).json()["token"]
    resp = await anon_client.post(f"/api/hosts/{host_id}/gui-ticket", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_ticket_refused_when_switch_off_or_not_configured(admin, monkeypatch):
    host_id = await _host()
    resp = await admin.post(f"/api/hosts/{host_id}/gui-ticket")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "GUI_ACCESS_DISABLED"
    await _enable(admin)
    monkeypatch.setattr(settings, "gui_gateway_token", "short")
    resp = await admin.post(f"/api/hosts/{host_id}/gui-ticket")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "GUI_GATEWAY_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_ticket_refused_without_secure_cookie(admin, monkeypatch):
    host_id = await _host()
    await _enable(admin)
    monkeypatch.setattr(settings, "cookie_secure", False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as plain:
        await plain.post("/api/auth/login", json={"username": settings.admin_username, "password": settings.admin_password})
        resp = await plain.post(f"/api/hosts/{host_id}/gui-ticket")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] in ("GUI_REQUIRES_HTTPS", "GUI_GATEWAY_NOT_CONFIGURED")
    monkeypatch.setattr(settings, "cookie_secure", True)
    # A legacy plain-name cookie (pre-migration) is not enough either.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://opnexus.example") as legacy:
        legacy.cookies.set("opnexus-session", admin.cookies.get(SECURE_COOKIE_NAME))
        resp = await legacy.post(f"/api/hosts/{host_id}/gui-ticket")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "GUI_REQUIRES_HTTPS"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,reason", [
    ({"verify_tls": False}, "tls"), ({"base_url": "http://fw-plain.test"}, "https"), ({"enabled": False}, "disabled"),
])
async def test_ticket_refused_for_ineligible_host(admin, kwargs, reason):
    host_id = await _host(**kwargs)
    await _enable(admin)
    resp = await admin.post(f"/api/hosts/{host_id}/gui-ticket")
    assert resp.status_code == 409 and resp.json()["detail"] == {"code": "GUI_HOST_NOT_ELIGIBLE", "params": {"reason": reason}}


@pytest.mark.asyncio
async def test_slots_fixed_per_firewall_and_limited(admin):
    a, b, c = await _host("fw-a"), await _host("fw-b", "https://fw-b.test"), await _host("fw-c", "https://fw-c.test")
    await _enable(admin, slot_count=2)
    slot_a = (await admin.post(f"/api/hosts/{a}/gui-ticket")).json()["slot"]
    slot_b = (await admin.post(f"/api/hosts/{b}/gui-ticket")).json()["slot"]
    assert slot_a != slot_b and {slot_a, slot_b} == {9441, 9442}
    assert (await admin.post(f"/api/hosts/{a}/gui-ticket")).json()["slot"] == slot_a
    full = await admin.post(f"/api/hosts/{c}/gui-ticket")
    assert full.status_code == 409 and full.json()["detail"]["code"] == "GUI_NO_FREE_SLOT"


# ── internal API ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_internal_api_needs_gateway_token(admin, monkeypatch):
    body = {"gui_session_id": str(uuid.uuid4()), "session_hash": "0" * 64}
    assert (await _internal("revalidate", body, token="wrong")).status_code == 401
    assert (await _internal("revalidate", body, token="")).status_code == 401
    monkeypatch.setattr(settings, "gui_gateway_token", "")
    assert (await _internal("revalidate", body, token="")).status_code == 404
    # Not part of the public API surface.
    assert "/internal/gui/redeem" not in (await admin.get("/api/openapi.json")).text


@pytest.mark.asyncio
async def test_redeem_returns_no_credentials_and_is_single_use(admin):
    host_id = await _host()
    await _enable(admin)
    url = (await admin.post(f"/api/hosts/{host_id}/gui-ticket")).json()["url"]
    resp = await _redeem(admin, url)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["upstream_origin"] == "https://fw-gui.test" and "PIN" in data["ca_pem"]
    assert data["idle_timeout_seconds"] == 900 and data["revalidate_seconds"] == 20
    assert "apikey-must-not-leak" not in resp.text and "secret" not in resp.text.lower() and "api_key" not in resp.text
    again = await _redeem(admin, url)
    assert again.status_code == 403 and again.json()["detail"]["reason"] == "replayed"
    async with SessionLocal() as db:
        actions = [e.action for e in (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "gui_access")
                                                        .order_by(AuditEvent.created_at))).all()]
    assert actions[:2] == ["ticket", "redeem"] and actions.count("redeem") == 2


@pytest.mark.asyncio
async def test_redeem_bound_to_session_slot_and_time(admin):
    host_id = await _host()
    await _enable(admin)
    url = (await admin.post(f"/api/hosts/{host_id}/gui-ticket")).json()["url"]
    wrong = await _redeem(admin, url, session_hash=sha("another-browser-session"))
    assert wrong.status_code == 403 and wrong.json()["detail"]["reason"] == "session"
    assert (await _redeem(admin, url)).json()["detail"]["reason"] == "replayed"  # burned by the failed attempt

    url = (await admin.post(f"/api/hosts/{host_id}/gui-ticket")).json()["url"]
    assert (await _redeem(admin, url, slot=9443)).json()["detail"]["reason"] == "slot"

    url = (await admin.post(f"/api/hosts/{host_id}/gui-ticket")).json()["url"]
    async with SessionLocal() as db:
        await db.execute(update(GuiTicket).where(GuiTicket.ticket_hash == sha(_ticket_from(url)))
                         .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await db.commit()
    assert (await _redeem(admin, url)).json()["detail"]["reason"] == "expired"
    unknown = await _internal("redeem", {"ticket_hash": sha("nope"), "session_hash": sha("x"), "slot": 9441})
    assert unknown.status_code == 403 and unknown.json()["detail"]["reason"] == "unknown"


async def _open(admin: AsyncClient, host_id) -> dict:
    url = (await admin.post(f"/api/hosts/{host_id}/gui-ticket")).json()["url"]
    data = (await _redeem(admin, url)).json()
    return {"gui_session_id": data["gui_session_id"], "session_hash": sha(admin.cookies.get(SECURE_COOKIE_NAME))}


@pytest.mark.asyncio
async def test_revalidate_ok_then_logout_ends_it(admin):
    host_id = await _host()
    await _enable(admin)
    ref = await _open(admin, host_id)
    ok = (await _internal("revalidate", ref)).json()
    assert ok["ok"] is True and ok["revalidate_seconds"] == 20
    await admin.post("/api/auth/logout")
    assert (await _internal("revalidate", ref)).json() == {"ok": False, "reason": "session"}
    assert (await _internal("revalidate", ref)).json()["ok"] is False
    async with SessionLocal() as db:
        ends = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "gui_access", AuditEvent.action == "end"))).all()
        assert len(ends) == 1 and ends[0].details["reason"] == "session" and "duration_seconds" in ends[0].details


@pytest.mark.asyncio
async def test_revalidate_fails_after_demotion(admin):
    host_id = await _host()
    await _enable(admin)
    second = await _user(admin, "gui-second-admin", "admin")
    ref = await _open(second, host_id)
    users = (await admin.get("/api/users")).json()
    uid = next(u["id"] for u in users if u["username"] == "gui-second-admin")
    assert (await admin.patch(f"/api/users/{uid}/role", json={"role": "operator"})).status_code == 200
    assert (await _internal("revalidate", ref)).json()["ok"] is False


@pytest.mark.asyncio
async def test_revalidate_fails_when_switched_off(admin):
    host_id = await _host()
    await _enable(admin)
    ref = await _open(admin, host_id)
    await _enable(admin, enabled=False)
    assert (await _internal("revalidate", ref)).json()["ok"] is False
    # The live re-check alone (switch flipped without the end-all sweep) also ends it.
    await _enable(admin)
    ref = await _open(admin, host_id)
    from app.models.gui_access import GuiAccessSettings
    async with SessionLocal() as db:
        await db.execute(update(GuiAccessSettings).values(enabled=False))
        await db.commit()
    assert (await _internal("revalidate", ref)).json() == {"ok": False, "reason": "disabled"}


@pytest.mark.asyncio
async def test_revalidate_enforces_absolute_limit_and_end(admin):
    host_id = await _host()
    await _enable(admin)
    ref = await _open(admin, host_id)
    async with SessionLocal() as db:
        await db.execute(update(GuiSession).values(started_at=datetime.now(timezone.utc) - timedelta(minutes=61)))
        await db.commit()
    assert (await _internal("revalidate", ref)).json() == {"ok": False, "reason": "absolute"}
    ref2 = await _open(admin, host_id)
    assert (await _internal("end", {**ref2, "reason": "idle"})).json() == {"ok": True}
    assert (await _internal("revalidate", ref2)).json() == {"ok": False, "reason": "idle"}
    assert (await _internal("end", {**ref2, "session_hash": sha("x"), "reason": "idle"})).json() == {"ok": False}
