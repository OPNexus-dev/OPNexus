import pytest

from httpx import ASGITransport, AsyncClient

from app.main import app


async def _login_as(username: str, password: str) -> AsyncClient:
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    resp = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return ac


async def _demote_all_other_admins(client: AsyncClient, keep_username: str = "admin") -> None:
    """`admin_users` is preserved across the whole test session (see
    conftest's `_clean_tables`), so earlier tests in this module may have
    left extra admin-role accounts behind. The "last admin" guard tests
    need `keep_username` to be provably the *only* admin, regardless of
    what ran before — demoting every other admin-role account makes that
    deterministic without ever touching `keep_username` itself."""
    users = (await client.get("/api/users")).json()
    for u in users:
        if u["role"] == "admin" and u["username"] != keep_username:
            resp = await client.patch(f"/api/users/{u['id']}/role", json={"role": "read_only"})
            assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_bootstrap_admin_has_admin_role(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_create_user_defaults_to_read_only(client):
    resp = await client.post("/api/users", json={"username": "viewer1", "password": "viewer-password-123"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "read_only"
    assert body["username"] == "viewer1"


@pytest.mark.asyncio
async def test_create_user_duplicate_username_rejected(client):
    resp = await client.post("/api/users", json={"username": "dupe", "password": "password-123456", "role": "admin"})
    assert resp.status_code == 201, resp.text
    resp = await client.post("/api/users", json={"username": "dupe", "password": "password-654321", "role": "admin"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_users_includes_bootstrap_admin_and_new_user(client):
    await client.post("/api/users", json={"username": "listed-user", "password": "password-123456"})
    resp = await client.get("/api/users")
    assert resp.status_code == 200, resp.text
    usernames = [u["username"] for u in resp.json()]
    assert "admin" in usernames
    assert "listed-user" in usernames


@pytest.mark.asyncio
async def test_update_role_and_reset_password(client):
    created = (await client.post("/api/users", json={"username": "role-target", "password": "password-123456"})).json()

    resp = await client.patch(f"/api/users/{created['id']}/role", json={"role": "admin"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"

    resp = await client.post(f"/api/users/{created['id']}/reset-password", json={"new_password": "brand-new-password-1"})
    assert resp.status_code == 200, resp.text

    new_client = await _login_as("role-target", "brand-new-password-1")
    try:
        me = await new_client.get("/api/auth/me")
        assert me.json()["role"] == "admin"
    finally:
        await new_client.aclose()


@pytest.mark.asyncio
async def test_cannot_demote_the_last_admin(client):
    # Never actually demote the shared bootstrap "admin" — every other test
    # in the whole session logs in as it via the `client` fixture, so if
    # this guard had a bug and let the demotion through, that one wrong
    # mutation would cascade into unrelated failures across the entire
    # suite. Establish "admin is provably the only admin" first, then
    # attempt (and expect to be rejected) the demotion.
    await _demote_all_other_admins(client)
    users = (await client.get("/api/users")).json()
    admin_id = next(u["id"] for u in users if u["username"] == "admin")

    resp = await client.patch(f"/api/users/{admin_id}/role", json={"role": "read_only"})
    assert resp.status_code == 409, resp.text

    me = await client.get("/api/auth/me")
    assert me.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_can_demote_admin_when_another_admin_exists(client):
    # Exercises the same success path via two fresh, disposable admin
    # accounts — never the shared bootstrap "admin" (see the comment on
    # test_cannot_demote_the_last_admin for why that one must stay admin).
    a = (await client.post("/api/users", json={"username": "admin-a", "password": "password-123456", "role": "admin"})).json()
    await client.post("/api/users", json={"username": "admin-b", "password": "password-123456", "role": "admin"})

    resp = await client.patch(f"/api/users/{a['id']}/role", json={"role": "read_only"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "read_only"


@pytest.mark.asyncio
async def test_cannot_delete_self(client):
    me = (await client.get("/api/auth/me")).json()
    users = (await client.get("/api/users")).json()
    own_id = next(u["id"] for u in users if u["username"] == me["username"])

    resp = await client.delete(f"/api/users/{own_id}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_user_succeeds_for_non_admin_target(client):
    created = (await client.post("/api/users", json={"username": "to-delete", "password": "password-123456"})).json()
    resp = await client.delete(f"/api/users/{created['id']}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_read_only_user_blocked_from_writes_but_not_reads(client):
    await client.post("/api/users", json={"username": "viewer3", "password": "password-123456", "role": "read_only"})
    viewer = await _login_as("viewer3", "password-123456")
    try:
        get_resp = await viewer.get("/api/hosts")
        assert get_resp.status_code == 200, get_resp.text

        write_resp = await viewer.post(
            "/api/hosts",
            json={"name": "blocked-host", "base_url": "https://blocked.test", "api_key": "k", "api_secret": "s", "verify_tls": False},
        )
        assert write_resp.status_code == 403, write_resp.text
        assert write_resp.json()["detail"]["code"] == "READ_ONLY_ACCESS"
    finally:
        await viewer.aclose()


@pytest.mark.asyncio
async def test_read_only_user_can_still_change_own_password(client):
    await client.post("/api/users", json={"username": "viewer4", "password": "password-123456", "role": "read_only"})
    viewer = await _login_as("viewer4", "password-123456")
    try:
        resp = await viewer.post(
            "/api/auth/change-password",
            json={"current_password": "password-123456", "new_password": "a-different-password-1"},
        )
        assert resp.status_code == 200, resp.text
    finally:
        await viewer.aclose()


@pytest.mark.asyncio
async def test_read_only_user_can_still_change_own_theme(client):
    # PUT /settings only ever writes the caller's own UserPreferences row
    # (see routers/settings.py) — a purely cosmetic self-service action,
    # not an app-config change, so it must stay allowed even for read_only.
    await client.post("/api/users", json={"username": "viewer5", "password": "password-123456", "role": "read_only"})
    viewer = await _login_as("viewer5", "password-123456")
    try:
        resp = await viewer.put("/api/settings", json={"theme": "dark"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["theme"] == "dark"
    finally:
        await viewer.aclose()


@pytest.mark.asyncio
async def test_non_admins_only_see_their_own_account_and_so_do_their_tokens(client):
    """T2 (1.32 test run): GET /api/users used to hand every logged-in account the whole directory."""
    from httpx import ASGITransport, AsyncClient

    from app.config import settings
    from app.main import app

    for name, role in (("t2-reader", "read_only"), ("t2-operator", "operator"), ("t2-other", "admin")):
        assert (await client.post("/api/users", json={"username": name, "password": "password-123456", "role": role})).status_code == 201
    for name in ("t2-reader", "t2-operator"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as own:
            assert (await own.post("/api/auth/login", json={"username": name, "password": "password-123456"})).status_code == 200
            assert [u["username"] for u in (await own.get("/api/users")).json()] == [name]
    assert {"admin", "t2-reader", "t2-operator", "t2-other"} <= {u["username"] for u in (await client.get("/api/users")).json()}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as reader:
        await reader.post("/api/auth/login", json={"username": "t2-reader", "password": "password-123456"})
        token = (await reader.post("/api/tokens", json={"name": "t2", "scope": "read", "current_password": "password-123456"})).json()["token"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as api:
        assert [u["username"] for u in (await api.get("/api/users")).json()] == ["t2-reader"]  # a token never sees more than its owner


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["demote", "delete"])
async def test_two_admins_acting_on_each_other_at_once_never_remove_the_last_admin(client, monkeypatch, how):
    """Both requests counted the other admin before either one changed anything (audit AUD-06)."""
    import asyncio
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models.user import AdminUser
    from app.routers import users

    await _demote_all_other_admins(client)
    ids = {}
    for name in ("race-a", "race-b"):
        made = await client.post("/api/users", json={"username": name, "password": "race-password-123", "role": "admin"})
        assert made.status_code == 201, made.text
        ids[name] = made.json()["id"]
    a = await _login_as("race-a", "race-password-123")
    b = await _login_as("race-b", "race-password-123")
    bootstrap = next(u["id"] for u in (await client.get("/api/users")).json() if u["username"] == "admin")
    assert (await client.patch(f"/api/users/{bootstrap}/role", json={"role": "operator"})).status_code == 200

    # Without the lock both requests reach the count together; with it the second one waits, so the first gives up waiting.
    original = users._admin_count
    arrived = 0
    both = asyncio.Event()

    async def synchronized_count(db, exclude_id=None):
        nonlocal arrived
        count = await original(db, exclude_id)
        arrived += 1
        if arrived >= 2:
            both.set()
        try:
            await asyncio.wait_for(both.wait(), 1)
        except asyncio.TimeoutError:
            pass
        return count

    monkeypatch.setattr(users, "_admin_count", synchronized_count)
    try:
        if how == "demote":
            first = a.patch(f"/api/users/{ids['race-b']}/role", json={"role": "operator"})
            second = b.patch(f"/api/users/{ids['race-a']}/role", json={"role": "operator"})
        else:
            first = a.delete(f"/api/users/{ids['race-b']}")
            second = b.delete(f"/api/users/{ids['race-a']}")
        responses = await asyncio.gather(first, second)
        codes = sorted(r.status_code for r in responses)
        assert codes[0] in (200, 204) and codes[1] in (403, 409), codes
        async with SessionLocal() as db:
            admins = list((await db.scalars(select(AdminUser).where(AdminUser.role == "admin"))).all())
            assert len(admins) == 1
    finally:
        monkeypatch.undo()
        async with SessionLocal() as db:  # restore the fixture account, drop the helpers
            for row in (await db.scalars(select(AdminUser).where(AdminUser.username.in_(("race-a", "race-b"))))).all():
                await db.delete(row)
            boot = await db.get(AdminUser, __import__("uuid").UUID(bootstrap))
            boot.role = "admin"
            await db.commit()
        await a.aclose()
        await b.aclose()
