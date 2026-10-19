"""The operator role: rolls out and operates, but cannot change access or destinations."""
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent
from tests.test_api_tokens import _bearer, _login

PW = "operator-password-1"


async def _operator(client, name="oper"):
    resp = await client.post("/api/users", json={"username": name, "password": PW, "role": "operator"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "operator"
    return await _login(name, PW)


@pytest.mark.asyncio
async def test_operator_can_deploy_but_not_manage_access(client):
    op = await _operator(client)
    try:
        assert (await op.post("/api/groups", json={"name": "ops-group"})).status_code == 201
        for method, path, body in [
            ("POST", "/api/users", {"username": "x", "password": "x" * 12, "role": "admin"}),
            ("PATCH", "/api/users/00000000-0000-0000-0000-000000000000/role", {"role": "admin"}),
            ("POST", "/api/hosts", {}),
            ("PUT", "/api/notifications/settings", {}),
            ("POST", "/api/system/updater/update", {}),
        ]:
            resp = await op.request(method, path, json=body)
            assert resp.status_code == 403, (path, resp.text)
            assert resp.json()["detail"]["code"] == "ADMIN_REQUIRED"
    finally:
        await op.aclose()


@pytest.mark.asyncio
async def test_operator_keeps_self_service(client):
    op = await _operator(client, "selfserve")
    try:
        assert (await op.put("/api/settings", json={"theme": "dark"})).status_code == 200
        resp = await op.post("/api/auth/change-password", json={"current_password": PW, "new_password": "another-password-1"})
        assert resp.status_code == 200, resp.text
        assert (await op.get("/api/users")).status_code == 200
    finally:
        await op.aclose()


@pytest.mark.asyncio
async def test_operator_tokens_are_capped_at_deploy_and_inherit_the_limits(client):
    op = await _operator(client, "tokop")
    try:
        denied = await op.post("/api/tokens", json={"name": "a", "scope": "admin", "current_password": PW})
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "API_TOKEN_SCOPE_TOO_HIGH"
        made = await op.post("/api/tokens", json={"name": "ci", "scope": "deploy", "current_password": PW})
        assert made.status_code == 201, made.text
    finally:
        await op.aclose()
    async with _bearer(made.json()["token"]) as api:
        assert (await api.post("/api/groups", json={"name": "from-op-token"})).status_code == 201
        assert (await api.post("/api/users", json={"username": "y", "password": "y" * 12})).status_code == 403


@pytest.mark.asyncio
async def test_operator_cannot_download_raw_backups(client, respx_mock):
    import httpx
    from tests.test_rules import _create_host
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    op = await _operator(client, "nobackup")
    try:
        resp = await op.get(f"/api/hosts/{host['id']}/backups/anything.xml")
        assert resp.status_code == 403
    finally:
        await op.aclose()


@pytest.mark.asyncio
async def test_refused_operator_writes_are_audited(client):
    op = await _operator(client, "audited")
    try:
        await op.post("/api/users", json={"username": "z", "password": "z" * 12})
    finally:
        await op.aclose()
    async with SessionLocal() as db:
        rows = (await db.scalars(select(AuditEvent).where(AuditEvent.actor == "audited", AuditEvent.status == "denied"))).all()
    assert rows and rows[0].kind == "user"


@pytest.mark.asyncio
async def test_demoting_an_admin_to_operator_takes_effect_at_once(client):
    resp = await client.post("/api/users", json={"username": "wasadmin", "password": PW, "role": "admin"})
    uid = resp.json()["id"]
    user = await _login("wasadmin", PW)
    try:
        assert (await user.post("/api/groups", json={"name": "g1"})).status_code == 201
        assert (await client.patch(f"/api/users/{uid}/role", json={"role": "operator"})).status_code == 200
        assert (await user.post("/api/users", json={"username": "w", "password": "w" * 12})).status_code == 403
    finally:
        await user.aclose()
