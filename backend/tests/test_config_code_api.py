"""HTTP API of configuration as code (1.32 P7): rights matrix, audit, errors, limits."""
import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.audit import AuditEvent
from app.models.rule import FirewallRule
from app.services import api_token_service
from tests.config_code_seed import seed_everything

PW = "config-code-password-1"


async def _login(username: str, password: str) -> AsyncClient:
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    resp = await ac.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return ac


def _bearer(token: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"})


async def _token(client, scope: str) -> str:
    resp = await client.post("/api/tokens", json={"name": f"t-{scope}", "scope": scope, "current_password": settings.admin_password})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def _changed_file(client) -> str:
    text = (await client.get("/api/config/export")).text
    data = yaml.safe_load(text)
    data["rules"][0]["description"] = "über die API"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


@pytest.mark.parametrize("scope,method,path,denied", [
    ("read", "GET", "/api/config/export", False),
    ("read", "POST", "/api/config/import/plan", False),
    ("read", "POST", "/api/config/import/apply", True),
    ("deploy", "POST", "/api/config/import/plan", False),
    ("deploy", "POST", "/api/config/import/apply", True),
    ("admin", "POST", "/api/config/import/apply", False),
    # the read-only exception is exactly this path, not its neighbours
    ("read", "POST", "/api/config/import/plan/x", True),
    ("read", "POST", "/api/config/import", True),
])
def test_token_scope_matrix(scope, method, path, denied):
    assert api_token_service.denies(scope, method, path) is denied


def test_operators_may_plan_but_not_apply():
    assert not api_token_service.operator_denies("POST", "/api/config/import/plan")
    assert api_token_service.operator_denies("POST", "/api/config/import/apply")


async def test_export_plan_apply_as_admin_with_audit(client):
    await seed_everything()
    resp = await client.get("/api/config/export")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/yaml")
    assert "attachment" in resp.headers["content-disposition"] and resp.headers["cache-control"] == "no-store"
    text = await _changed_file(client)
    plan = (await client.post("/api/config/import/plan", json={"yaml": text})).json()
    assert plan["summary"]["update"] == 1 and plan["applicable"] and len(plan["token"]) == 64
    assert [i["key"] for i in plan["items"]] == ["allow-dns-lan"]  # unchanged objects are not listed
    resp = await client.post("/api/config/import/apply", json={"yaml": text, "plan_token": plan["token"]})
    assert resp.status_code == 200 and resp.json()["summary"]["update"] == 1
    async with SessionLocal() as db:
        events = (await db.execute(select(AuditEvent))).scalars().all()
        rule = await db.scalar(select(FirewallRule).where(FirewallRule.config_key == "allow-dns-lan"))
    assert rule.description == "über die API"
    requests = [e for e in events if e.kind == "config" and e.action == "apply"]
    assert [e.status for e in requests] == ["requested", "accepted"] and requests[0].entity_id is None
    assert not any(e.action == "plan" for e in events)  # planning is like a preview: not audited
    assert all("über die API" not in str(e.details) for e in events if e.kind == "config")
    assert any(e.kind == "rule" and e.action == "import_update" and e.actor == settings.admin_username for e in events)


async def test_stale_blocked_invalid_and_too_large(client):
    await seed_everything()
    text = await _changed_file(client)
    plan = (await client.post("/api/config/import/plan", json={"yaml": text})).json()
    resp = await client.post("/api/config/import/apply", json={"yaml": text + "\n# anders\n", "plan_token": "0" * 64})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "CONFIG_PLAN_STALE"
    resp = await client.post("/api/config/import/plan", json={"yaml": "a: &x 1\n"})
    detail = resp.json()["detail"]
    assert resp.status_code == 422 and detail["code"] == "CONFIG_INVALID" and detail["params"]["errors"][0]["code"] == "yaml_anchor_forbidden"
    resp = await client.post("/api/config/import/plan", json={"yaml": "#" * (1024 * 1024 + 1)})
    assert resp.status_code == 413 and resp.json()["detail"]["code"] == "CONFIG_TOO_LARGE"
    resp = await client.post("/api/config/import/plan", content=b"x" * (4 * 1024 * 1024 + 1), headers={"content-type": "application/json"})
    assert resp.status_code == 413
    resp = await client.post("/api/config/import/plan", json={"yaml": "opnexus_config: 9\n"})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "CONFIG_UNSUPPORTED_VERSION"
    resp = await client.post("/api/config/import/plan", json={"yaml": "opnexus_config: 1\n", "extra": 1})
    assert resp.status_code == 422 and resp.json()["detail"]["params"]["errors"][0]["code"] == "bad_request"
    resp = await client.get("/api/config/export?kinds=rules,secrets")
    assert resp.status_code == 422
    blocked = "opnexus_config: 1\naliases: []\n"
    plan = (await client.post("/api/config/import/plan", json={"yaml": blocked, "prune": True})).json()
    assert not plan["applicable"] and plan["items"][0]["action"] == "blocked"
    resp = await client.post("/api/config/import/apply", json={"yaml": blocked, "prune": True, "plan_token": plan["token"]})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "CONFIG_BLOCKED"


async def test_read_only_account_can_export_and_plan_but_not_apply(client):
    await seed_everything()
    assert (await client.post("/api/users", json={"username": "cfg-reader", "password": PW, "role": "read_only"})).status_code == 201
    text = await _changed_file(client)
    reader = await _login("cfg-reader", PW)
    try:
        assert (await reader.get("/api/config/export")).status_code == 200
        plan = await reader.post("/api/config/import/plan", json={"yaml": text})
        assert plan.status_code == 200
        resp = await reader.post("/api/config/import/apply", json={"yaml": text, "plan_token": plan.json()["token"]})
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "READ_ONLY_ACCESS"
    finally:
        await reader.aclose()


async def test_operator_can_plan_but_not_apply(client):
    await seed_everything()
    assert (await client.post("/api/users", json={"username": "cfg-operator", "password": PW, "role": "operator"})).status_code == 201
    text = await _changed_file(client)
    op = await _login("cfg-operator", PW)
    try:
        plan = await op.post("/api/config/import/plan", json={"yaml": text})
        assert plan.status_code == 200
        resp = await op.post("/api/config/import/apply", json={"yaml": text, "plan_token": plan.json()["token"]})
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "ADMIN_REQUIRED"
    finally:
        await op.aclose()
    async with SessionLocal() as db:
        events = (await db.execute(select(AuditEvent).where(AuditEvent.kind == "config"))).scalars().all()
    assert [e.status for e in events] == ["denied"]


@pytest.mark.parametrize("scope,plan_status,apply_status", [("read", 200, 403), ("deploy", 200, 403), ("admin", 200, 200)])
async def test_tokens(client, scope, plan_status, apply_status):
    await seed_everything()
    text = await _changed_file(client)
    async with _bearer(await _token(client, scope)) as api:
        assert (await api.get("/api/config/export")).status_code == 200
        plan = await api.post("/api/config/import/plan", json={"yaml": text})
        assert plan.status_code == plan_status
        resp = await api.post("/api/config/import/apply", json={"yaml": text, "plan_token": plan.json()["token"]})
        assert resp.status_code == apply_status, resp.text


async def test_group_changes_are_audited_now(client):
    resp = await client.post("/api/groups", json={"name": "Neu", "description": "", "host_ids": []})
    assert resp.status_code == 201
    async with SessionLocal() as db:
        events = (await db.execute(select(AuditEvent).where(AuditEvent.kind == "group"))).scalars().all()
    assert [e.status for e in events] == ["requested", "accepted"]


async def test_too_many_waiting_plans_are_turned_away(client, monkeypatch):
    from app.routers import config_code

    monkeypatch.setattr(config_code, "_waiting", config_code.MAX_WAITING)
    resp = await client.post("/api/config/import/plan", json={"yaml": "opnexus_config: 1\n"})
    assert resp.status_code == 429 and resp.json()["detail"]["code"] == "CONFIG_BUSY"
    monkeypatch.setattr(config_code, "_waiting", 0)
    assert (await client.post("/api/config/import/plan", json={"yaml": "opnexus_config: 1\n"})).status_code == 200


async def test_a_wrong_shaped_section_is_answered_with_422_not_a_server_error(client):
    resp = await client.post("/api/config/import/plan", json={"yaml": "opnexus_config: 1\naliases: 42\n"})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "CONFIG_INVALID" and detail["params"]["errors"]
