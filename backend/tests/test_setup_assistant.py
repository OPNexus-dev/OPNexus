"""First-run setup assistant: per-user "done" flag and the password reset tool."""
import pytest
from sqlalchemy import select

from app import reset_password
from app.config import settings
from app.database import SessionLocal
from app.models.user import AdminUser, Session
from app.services import auth_service


@pytest.mark.asyncio
async def test_setup_done_defaults_to_false_and_persists(client):
    assert (await client.get("/api/settings")).json()["setup_done"] is False

    resp = await client.put("/api/settings", json={"setup_done": True})
    assert resp.status_code == 200
    assert resp.json()["setup_done"] is True
    assert (await client.get("/api/settings")).json()["setup_done"] is True


@pytest.mark.asyncio
async def test_other_preference_changes_keep_setup_done(client):
    await client.put("/api/settings", json={"setup_done": True})
    resp = await client.put("/api/settings", json={"theme": "dark", "language": "en"})
    body = resp.json()
    assert (body["theme"], body["language"], body["setup_done"]) == ("dark", "en", True)

    # ...and the other direction: saving setup_done leaves theme/language alone.
    resp = await client.put("/api/settings", json={"setup_done": False})
    body = resp.json()
    assert (body["theme"], body["language"], body["setup_done"]) == ("dark", "en", False)


@pytest.mark.asyncio
async def test_setup_done_rejects_non_boolean(client):
    assert (await client.put("/api/settings", json={"setup_done": "maybe"})).status_code == 422


@pytest.mark.asyncio
async def test_reset_password_prints_new_password_and_ends_sessions(client, capsys):
    async with SessionLocal() as db:
        before = (await db.execute(select(Session))).scalars().all()
        assert before, "the client fixture logged in, so a session must exist"

    assert await reset_password.main(None) == 0
    out = capsys.readouterr().out
    lines = dict(line.split(": ", 1) for line in out.strip().splitlines())
    assert lines["Username"] == settings.admin_username
    new_password = lines["Password"]
    assert len(new_password) >= 20 and new_password != settings.admin_password

    async with SessionLocal() as db:
        assert (await db.execute(select(Session))).scalars().all() == []
        user = (await db.execute(select(AdminUser).where(AdminUser.username == settings.admin_username))).scalar_one()
        assert await auth_service.verify_password_async(user.password_hash, new_password)
        assert not await auth_service.verify_password_async(user.password_hash, settings.admin_password)
        # Put the shared bootstrap admin back for the tests that follow.
        user.password_hash = await auth_service.hash_password_async(settings.admin_password)
        await db.commit()


@pytest.mark.asyncio
async def test_reset_password_unknown_user_fails(capsys):
    assert await reset_password.main("does-not-exist") == 1
    assert "No such user" in capsys.readouterr().err
