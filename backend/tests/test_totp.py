"""Optional two-factor sign-in."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import auth_service, totp_service

PASSWORD = "correct-horse-battery"


def test_rfc6238_vector():
    # RFC 6238 appendix B, SHA-1, T=59s -> 94287082 (8 digits) => 287082 with 6
    assert totp_service.code_at("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59) == "287082"


def test_verify_window_and_replay():
    secret = totp_service.generate_secret()
    now = 1_700_000_000.0
    code = totp_service.code_at(secret, now)
    step = totp_service.verify_code(secret, code, 0, now)
    assert step == int(now // 30)
    assert totp_service.verify_code(secret, code, step, now) is None          # replay
    assert totp_service.verify_code(secret, code, 0, now + 30) == step        # clock drift +1
    assert totp_service.verify_code(secret, code, 0, now + 120) is None       # too old
    assert totp_service.verify_code(secret, "12ab56", 0, now) is None


def test_recovery_codes_are_single_use():
    plain, hashes = totp_service.new_recovery_codes()
    assert len(plain) == 10 and len(set(plain)) == 10
    remaining = totp_service.consume_recovery(hashes, plain[0].upper())
    assert remaining is not None and len(remaining) == 9
    assert totp_service.consume_recovery(remaining, plain[0]) is None


async def _make_user(client, name):
    resp = await client.post("/api/users", json={"username": name, "password": PASSWORD, "role": "admin"})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(name, **extra):
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    resp = await c.post("/api/auth/login", json={"username": name, "password": PASSWORD, **extra})
    return c, resp


async def _enroll(name):
    c, resp = await _login(name)
    assert resp.status_code == 200
    setup = await c.post("/api/auth/2fa/setup", json={"password": PASSWORD})
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/OPNexus:")
    enable = await c.post("/api/auth/2fa/enable", json={"code": totp_service.code_at(secret)})
    assert enable.status_code == 200, enable.text
    return c, secret, enable.json()["recovery_codes"]


@pytest.mark.asyncio
async def test_full_flow_login_requires_second_factor(client):
    await _make_user(client, "alice")
    c, secret, recovery = await _enroll("alice")
    await c.aclose()

    _, no_code = await _login("alice")
    assert no_code.status_code == 401 and no_code.json()["detail"]["code"] == "TOTP_REQUIRED"
    _, bad = await _login("alice", totp_code="000000")
    assert bad.json()["detail"]["code"] == "TOTP_INVALID"

    # the code used while enabling is spent; a later step is accepted once
    import time
    from unittest.mock import patch
    with patch("time.time", return_value=time.time() + 30):
        code = totp_service.code_at(secret)
        c2, ok = await _login("alice", totp_code=code)
        assert ok.status_code == 200
        _, replay = await _login("alice", totp_code=code)
        assert replay.json()["detail"]["code"] == "TOTP_INVALID"
    assert (await c2.get("/api/auth/me")).json()["authenticated"] is True

    c3, via_recovery = await _login("alice", totp_code=recovery[0])
    assert via_recovery.status_code == 200
    _, reused = await _login("alice", totp_code=recovery[0])
    assert reused.json()["detail"]["code"] == "TOTP_INVALID"
    status_ = (await c3.get("/api/auth/2fa")).json()
    assert status_ == {"enabled": True, "recovery_codes_left": 9}


@pytest.mark.asyncio
async def test_wrong_password_never_reveals_two_factor(client):
    await _make_user(client, "bob")
    c, _, _ = await _enroll("bob")
    resp = await AsyncClient(transport=ASGITransport(app=app), base_url="http://test").post(
        "/api/auth/login", json={"username": "bob", "password": "wrong-password-xx"})
    assert resp.json()["detail"]["code"] == "UNGUELTIGER_BENUTZERNAME_ODER_PASSWORT"


@pytest.mark.asyncio
async def test_wrong_codes_are_throttled(client):
    await _make_user(client, "carol")
    await _enroll("carol")
    for _ in range(5):
        _, r = await _login("carol", totp_code="111111")
    _, locked = await _login("carol", totp_code="111111")
    assert locked.status_code == 429


@pytest.mark.asyncio
async def test_setup_needs_password_and_enable_needs_valid_code(client):
    await _make_user(client, "dave")
    c, _ = await _login("dave")
    assert (await c.post("/api/auth/2fa/setup", json={"password": "nope-nope-nope"})).status_code == 401
    assert (await c.post("/api/auth/2fa/enable", json={"code": "123456"})).json()["detail"]["code"] == "TOTP_SETUP_MISSING"
    await c.post("/api/auth/2fa/setup", json={"password": PASSWORD})
    assert (await c.post("/api/auth/2fa/enable", json={"code": "000000"})).json()["detail"]["code"] == "TOTP_INVALID"
    assert (await c.get("/api/auth/2fa")).json()["enabled"] is False


@pytest.mark.asyncio
async def test_disable_needs_password_and_code_and_admin_can_reset(client):
    eve = await _make_user(client, "eve")
    c, secret, recovery = await _enroll("eve")
    bad = await c.post("/api/auth/2fa/disable", json={"password": PASSWORD, "code": "000000"})
    assert bad.status_code == 401
    ok = await c.post("/api/auth/2fa/disable", json={"password": PASSWORD, "code": recovery[1]})
    assert ok.status_code == 200 and ok.json() == {"enabled": False}
    _, plain = await _login("eve")
    assert plain.status_code == 200

    await _enroll("eve")
    reset = await client.post(f"/api/users/{eve['id']}/reset-2fa")
    assert reset.status_code == 200 and reset.json()["totp_enabled"] is False
    _, after = await _login("eve")
    assert after.status_code == 200


@pytest.mark.asyncio
async def test_read_only_user_can_manage_own_second_factor(client):
    await client.post("/api/users", json={"username": "frank", "password": PASSWORD, "role": "read_only"})
    c, _ = await _login("frank")
    assert (await c.post("/api/auth/2fa/setup", json={"password": PASSWORD})).status_code == 200


@pytest.mark.asyncio
async def test_password_login_racing_2fa_enable_gets_no_session(client, monkeypatch):
    """A sign-in decided "no second factor" must not outlive 2FA being enabled in the meantime (audit AUD-02)."""
    import asyncio
    from sqlalchemy import func, select
    from app.database import SessionLocal
    from app.models.user import AdminUser, Session
    await _make_user(client, "racer")
    owner, _ = await _login("racer")
    setup = await owner.post("/api/auth/2fa/setup", json={"password": PASSWORD})
    secret = setup.json()["secret"]
    reached, resume = asyncio.Event(), asyncio.Event()
    original = auth_service.finalize_login

    async def paused(db, user, ip):
        reached.set()
        await resume.wait()
        return await original(db, user, ip)

    monkeypatch.setattr(auth_service, "finalize_login", paused)
    other = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    login = asyncio.create_task(other.post("/api/auth/login", json={"username": "racer", "password": PASSWORD}))
    try:
        await asyncio.wait_for(reached.wait(), 10)
        enable = await owner.post("/api/auth/2fa/enable", json={"code": totp_service.code_at(secret)})
        assert enable.status_code == 200, enable.text
    finally:
        resume.set()
    resp = await login
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "TOTP_REQUIRED"
    assert (await other.get("/api/auth/2fa")).status_code == 401
    async with SessionLocal() as db:
        racer = await db.scalar(select(AdminUser.id).where(AdminUser.username == "racer"))
        assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == racer)) == 1  # the owner's
    await owner.aclose()
    await other.aclose()
