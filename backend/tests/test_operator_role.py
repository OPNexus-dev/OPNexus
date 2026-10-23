"""The operator role: rolls out and operates, but cannot change access or destinations."""
import httpx
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
        listed = await op.get("/api/users")  # self-service: only the own account, never the directory (T2)
        assert listed.status_code == 200 and [u["username"] for u in listed.json()] == ["selfserve"]
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


# ── separation of duties ─────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,rollout", [
    ("POST", "/api/rules/abc/deploy", True),
    ("POST", "/api/aliases/abc/deploy", True),
    ("POST", "/api/port-forward/abc/deploy", True),
    ("POST", "/api/rules/abc/deployments/def/remove", True),
    ("POST", "/api/rules/abc/deployments/def/retry-rollback", True),
    ("POST", "/api/hosts/abc/maintenance/execute", True),
    ("POST", "/api/hosts/abc/maintenance/carp-execute", True),
    ("POST", "/api/hosts/abc/maintenance/schedule", True),
    ("POST", "/api/hosts/abc/network/changes", True),
    ("DELETE", "/api/rules/abc", True),
    ("DELETE", "/api/aliases/abc", True),
    ("DELETE", "/api/port-forward/abc", True),
    ("DELETE", "/api/one-to-one/abc", True),
    ("DELETE", "/api/outbound-nat/abc", True),
    ("DELETE", "/api/policy-hierarchy/rules/abc", True),
    ("DELETE", "/api/ipsec/tunnels/abc", True),
    ("DELETE", "/api/certificates/managed/abc", True),
    ("DELETE", "/api/hosts/abc/snapshots/def", True),
    ("POST", "/api/certificates/managed/abc/push", True),
    ("POST", "/api/acme/accounts/abc/register", True),
    ("POST", "/api/acme/certificates/abc/sign", True),
    ("POST", "/api/acme/certificates/abc/poll", True),
    ("POST", "/api/policy-hierarchy/groups/abc/reassert-ordering", True),
    ("POST", "/api/hosts/abc/services/unbound/restart", True),
    ("DELETE", "/api/groups/abc", False),
    ("DELETE", "/api/rules/abc/extra", False),
    ("POST", "/api/rules/abc/preview", False),
    ("POST", "/api/rules", False),
    ("PUT", "/api/rules/abc", False),
    ("POST", "/api/rules/abc/confirm", False),
    ("GET", "/api/rules/abc/deploy", False),
    ("POST", "/api/hosts/abc/network/changes/preview", False),
    ("POST", "/api/hosts/abc/snapshots", True),
    ("GET", "/api/hosts/abc/snapshots", False),
])
def test_what_counts_as_a_rollout(method, path, rollout):
    from app.services import api_token_service
    assert api_token_service.is_rollout(method, path) is rollout


@pytest.mark.asyncio
async def test_with_separation_of_duties_operators_prepare_but_only_admins_deploy(client):
    op = await _operator(client, "dutyop")
    try:
        assert (await op.get("/api/settings/governance")).json()["operators_may_deploy"] is True
        assert (await op.put("/api/settings/governance", json={"operators_may_deploy": False})).status_code == 403
        assert (await client.put("/api/settings/governance", json={"operators_may_deploy": False})).json()["operators_may_deploy"] is False
        # Preparing still works ...
        assert (await op.post("/api/groups", json={"name": "prepared"})).status_code == 201
        # ... rolling out does not, whatever the target (the 403 comes before any lookup).
        for path in ("/api/rules/00000000-0000-0000-0000-000000000000/deploy", "/api/hosts/00000000-0000-0000-0000-000000000000/maintenance/execute"):
            resp = await op.post(path, json={})
            assert resp.status_code == 403 and resp.json()["detail"]["code"] == "DEPLOY_NEEDS_ADMIN", path
        # An admin is not affected (404 = got past the gate and looked for the rule).
        assert (await client.post("/api/rules/00000000-0000-0000-0000-000000000000/deploy", json={})).status_code in (404, 422)
        # Switching it back lets the operator through the gate again.
        await client.put("/api/settings/governance", json={"operators_may_deploy": True})
        assert (await op.post("/api/rules/00000000-0000-0000-0000-000000000000/deploy", json={})).status_code in (404, 422)
    finally:
        await op.aclose()
        await client.put("/api/settings/governance", json={"operators_may_deploy": True})


@pytest.mark.asyncio
async def test_the_refusal_is_audited_and_covers_operator_tokens(client):
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models.audit import AuditEvent
    op = await _operator(client, "dutytok")
    try:
        token = (await op.post("/api/tokens", json={"name": "ci", "scope": "deploy", "current_password": PW})).json()["token"]
    finally:
        await op.aclose()
    await client.put("/api/settings/governance", json={"operators_may_deploy": False})
    try:
        async with _bearer(token) as api:
            resp = await api.post("/api/rules/00000000-0000-0000-0000-000000000000/deploy", json={})
            assert resp.status_code == 403 and resp.json()["detail"]["code"] == "DEPLOY_NEEDS_ADMIN"
    finally:
        await client.put("/api/settings/governance", json={"operators_may_deploy": True})
    async with SessionLocal() as db:
        rows = (await db.scalars(select(AuditEvent).where(AuditEvent.status == "denied", AuditEvent.actor.like("dutytok%")))).all()
    assert rows and rows[0].kind == "rule" and "dutytok (API: ci)" in {r.actor for r in rows}


@pytest.mark.asyncio
async def test_channel_test_button_is_admin_only_on_the_server_too(client):
    """The UI hides "Testen" for non-admins; the server refuses it regardless (a test sends a message to an outside server)."""
    channel_id = "00000000-0000-0000-0000-000000000001"  # refused before the id is even looked up
    op = await _operator(client, "chantest-op")
    ro = None
    try:
        assert (await client.post("/api/users", json={"username": "chantest-ro", "password": PW, "role": "read_only"})).status_code == 201
        ro = await _login("chantest-ro", PW)
        resp = await op.post(f"/api/notifications/channels/{channel_id}/test")
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "ADMIN_REQUIRED"
        resp = await ro.post(f"/api/notifications/channels/{channel_id}/test")
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "READ_ONLY_ACCESS"
        for who in (op, ro):
            assert (await who.post("/api/notifications/test", json={"channel": "webhook"})).status_code == 403
    finally:
        await op.aclose()
        if ro is not None:
            await ro.aclose()


@pytest.mark.asyncio
async def test_creating_a_snapshot_is_a_rollout_and_follows_the_separation_of_duties(client, respx_mock):
    """A boot environment is created ON the firewall: with deploying admin-only an operator may not do it (AUD-05)."""
    from tests.test_snapshots import BASE, ROW, _host
    host_id = await _host(client, respx_mock, "fw-oper-snap")
    new = {"uuid": "s9", "name": "by_operator", "active": "-", "mountpoint": "-", "size": "0", "created_str": "now"}
    respx_mock.get(f"{BASE}/api/core/snapshots/isSupported").mock(return_value=httpx.Response(200, json={"supported": True}))
    respx_mock.get(f"{BASE}/api/core/snapshots/search").mock(return_value=httpx.Response(200, json={"rows": [ROW, new]}))
    add = respx_mock.post(f"{BASE}/api/core/snapshots/add").mock(return_value=httpx.Response(200, json="OK"))
    op = await _operator(client, "snapop")
    try:
        assert (await op.post(f"/api/hosts/{host_id}/snapshots", json={"name": "by_operator"})).status_code == 201
        assert add.call_count == 1
        assert (await client.put("/api/settings/governance", json={"operators_may_deploy": False})).status_code == 200
        resp = await op.post(f"/api/hosts/{host_id}/snapshots", json={"name": "by_operator"})
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "DEPLOY_NEEDS_ADMIN"
        assert add.call_count == 1  # nothing was sent to the firewall
        assert (await client.post(f"/api/hosts/{host_id}/snapshots", json={"name": "by_operator"})).status_code == 201  # an admin may
    finally:
        await client.put("/api/settings/governance", json={"operators_may_deploy": True})
        await op.aclose()
