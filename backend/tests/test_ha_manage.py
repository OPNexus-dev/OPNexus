"""U4 (docs/DESIGN-HA-UI.md section 5): the pair management behind the card in the settings.

The updater of the node does the work; these tests fix what the backend itself decides: who may, that the password is asked again for
everything that hands data to another machine or takes this one out of service, what is passed on (never the password), and how an
answer of the updater turns into an error code."""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.errors import ErrorCode
from app.main import app
from app.routers import ha_manage
from app.services import auth_service, updater_client

PW = settings.admin_password
CODE = "OPNX1-AHAKQ-AQVED-6RNLN-NHIFB-JOM4S-XRDPF-RMO32"


@pytest.fixture
def updater(monkeypatch):
    """A fake updater: records what the backend hands over, answers as told."""
    state = {"calls": [], "raise": None, "status": {"paired": False, "peer": None, "peer_reachable": None, "ha_role": None, "job": None, "log": []}}

    def record(name):
        async def call(*args, **kwargs):
            state["calls"].append((name, args, kwargs))
            if state["raise"] is not None:
                raise state["raise"]
            if name == "status":
                return state["status"]
            return {"job": {"id": "j1", "state": "running", "verb": name}} if name in ("init", "action") else {"ok": name}
        return call

    for name, attr in (("status", "ha_local_status"), ("suggest", "ha_local_suggest"), ("prepare", "ha_local_prepare"), ("init", "ha_local_init"), ("action", "ha_local_action")):
        monkeypatch.setattr(updater_client, attr, record(name))
    return state


@pytest_asyncio.fixture
async def user_client():
    """Sign in as an extra account of a given role (and remove it again: admin_users outlives the per-test clean-up)."""
    made: list[tuple[AsyncClient, str]] = []

    async def make(role):
        name = f"hm-{role}-{uuid.uuid4().hex[:6]}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
            await admin.post("/api/auth/login", json={"username": settings.admin_username, "password": PW})
            created = await admin.post("/api/users", json={"username": name, "password": "hm-secret-password-1", "role": role})
            assert created.status_code in (200, 201), created.text
            made.append((created.json()["id"], name))
        ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        assert (await ac.post("/api/auth/login", json={"username": name, "password": "hm-secret-password-1"})).status_code == 200
        return ac

    yield make
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
        await admin.post("/api/auth/login", json={"username": settings.admin_username, "password": PW})
        for user_id, _ in made:
            await admin.delete(f"/api/users/{user_id}")


def _err(resp):
    return resp.json()["detail"]["code"]


# ── status ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_status_of_an_unpaired_node_carries_the_agent_block(client, updater):
    body = (await client.get("/api/system/ha")).json()
    assert body["configured"] is False
    assert body["agent"] == {"available": True, "paired": False, "peer": None, "peer_reachable": None, "role": None, "job": None}


@pytest.mark.asyncio
async def test_without_an_updater_the_card_is_told_so(client, updater):
    updater["raise"] = updater_client.UpdaterUnavailable("not_enabled")
    body = (await client.get("/api/system/ha")).json()
    assert body["configured"] is False and body["agent"]["available"] is False


@pytest.mark.asyncio
async def test_a_paired_agent_is_reported_with_the_partner_and_the_job(client, updater):
    updater["status"].update(paired=True, peer="192.168.2.21", peer_reachable=False, ha_role="standby", job={"state": "running", "verb": "sync"})
    agent = (await client.get("/api/system/ha")).json()["agent"]
    assert agent == {"available": True, "paired": True, "peer": "192.168.2.21", "peer_reachable": False, "role": "standby", "job": {"state": "running", "verb": "sync"}}


# ── who may ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_only_administrators_manage_the_pair(user_client, updater):
    for role in ("operator", "read_only"):
        ac = await user_client(role)
        calls = (("GET", "/api/system/ha/job", None), ("POST", "/api/system/ha/suggest", {}), ("POST", "/api/system/ha/prepare", {"password": "x", "ip": "10.0.0.1"}),
                 ("POST", "/api/system/ha/init", {"code": CODE, "vip": "10.0.0.2/24", "check": True}), ("POST", "/api/system/ha/action", {"password": "x", "action": "sync"}))
        for method, path, body in calls:
            resp = await ac.request(method, path, json=body)
            assert resp.status_code == 403, f"{role} {method} {path}: {resp.status_code}"
        await ac.aclose()
    assert updater["calls"] == []


@pytest.mark.asyncio
async def test_without_a_session_nothing_opens(anon_client, updater):
    for method, path in (("GET", "/api/system/ha/job"), ("POST", "/api/system/ha/prepare"), ("POST", "/api/system/ha/action")):
        assert (await anon_client.request(method, path, json={})).status_code == 401
    assert updater["calls"] == []


@pytest.mark.asyncio
async def test_a_token_below_admin_scope_cannot_manage_the_pair(client, anon_client, updater):
    for scope in ("read", "deploy"):
        created = await client.post("/api/tokens", json={"name": f"ha-{scope}", "scope": scope, "current_password": PW})
        token = created.json()["token"]
        # every WRITE is refused; reading the job log is allowed like reading the update log (it holds neither the code nor a secret)
        for method, path, body in (("POST", "/api/system/ha/action", {"password": PW, "action": "sync"}),
                                   ("POST", "/api/system/ha/prepare", {"password": PW, "ip": "10.0.0.1"}),
                                   ("POST", "/api/system/ha/init", {"password": PW, "code": CODE, "vip": "10.0.0.2/24"}),
                                   ("POST", "/api/system/ha/suggest", {})):
            resp = await anon_client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code in (401, 403), f"{scope} {method} {path}: {resp.status_code}"
    assert updater["calls"] == []


# ── asking for the password again ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prepare_init_and_actions_need_the_right_password(client, updater):
    wrong = "not-my-password"
    cases = (("/api/system/ha/prepare", {"password": wrong, "ip": "10.0.0.1"}),
             ("/api/system/ha/init", {"password": wrong, "code": CODE, "vip": "10.0.0.2/24"}),
             ("/api/system/ha/action", {"password": wrong, "action": "dissolve"}))
    for path, body in cases:
        resp = await client.post(path, json=body)
        assert resp.status_code == 401 and _err(resp) == "AKTUELLES_PASSWORT_IST_FALSCH", path
    assert updater["calls"] == []


@pytest.mark.asyncio
async def test_too_many_wrong_passwords_lock_the_action_out(client, updater):
    from app import runtime_settings
    for _ in range(runtime_settings.value("login_max_attempts")):
        assert (await client.post("/api/system/ha/action", json={"password": "wrong", "action": "sync"})).status_code == 401
    resp = await client.post("/api/system/ha/action", json={"password": PW, "action": "sync"})
    assert resp.status_code == 429 and _err(resp) == "TOO_MANY_LOGIN_ATTEMPTS"
    assert updater["calls"] == []


@pytest.mark.asyncio
async def test_an_account_without_a_local_password_is_refused_with_its_own_reason():
    class Fake:
        auth_source = "oidc"
        password_hash = "x"
        id = uuid.uuid4()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as info:
        await ha_manage._reauth(Fake(), "whatever", "ha-sync")
    assert info.value.status_code == 403 and info.value.detail["code"] == "HA_LOCAL_ACCOUNT_REQUIRED"


@pytest.mark.asyncio
async def test_the_password_is_never_passed_on_or_logged(client, updater, caplog):
    caplog.set_level("DEBUG")
    await client.post("/api/system/ha/init", json={"password": PW, "code": CODE, "vip": "10.0.0.2/24"})
    await client.post("/api/system/ha/action", json={"password": PW, "action": "sync"})
    await client.post("/api/system/ha/prepare", json={"password": PW, "ip": "10.0.0.1"})
    assert PW not in repr(updater["calls"])
    assert PW not in caplog.text
    assert CODE not in caplog.text


# ── what is passed on ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_suggest_passes_the_target_and_validates_it(client, updater):
    assert (await client.post("/api/system/ha/suggest", json={"target": "192.168.2.21"})).status_code == 200
    assert (await client.post("/api/system/ha/suggest", json={})).status_code == 200
    assert [c[1] for c in updater["calls"]] == [("192.168.2.21",), (None,)]
    for bad in ("x", "999.1.1.1", "1.2.3.4;id"):
        resp = await client.post("/api/system/ha/suggest", json={"target": bad})
        assert resp.status_code == 400 and _err(resp) == "HA_INPUT_INVALID", bad


@pytest.mark.asyncio
async def test_prepare_validates_the_address_before_asking_for_the_password(client, updater):
    for bad in ("", "x", "1.2.3", "999.1.1.1", "10.0.0.1;id"):
        resp = await client.post("/api/system/ha/prepare", json={"password": "wrong", "ip": bad})
        assert resp.status_code in (400, 422), bad
        assert resp.status_code != 401, "an invalid address must not count as a password guess"
    assert updater["calls"] == []
    resp = await client.post("/api/system/ha/prepare", json={"password": PW, "ip": "192.168.2.21"})
    assert resp.status_code == 200 and updater["calls"] == [("prepare", ("192.168.2.21",), {})]


@pytest.mark.asyncio
async def test_a_check_needs_no_password_but_a_real_setup_does(client, updater):
    resp = await client.post("/api/system/ha/init", json={"code": CODE, "vip": "192.168.2.22/24", "check": True})
    assert resp.status_code == 202
    assert updater["calls"][0] == ("init", ({"code": CODE, "vip": "192.168.2.22/24", "check": True},), {})
    updater["calls"].clear()
    resp = await client.post("/api/system/ha/init", json={"code": CODE, "vip": "192.168.2.22/24"})
    assert resp.status_code == 401 and updater["calls"] == []
    resp = await client.post("/api/system/ha/init", json={"password": PW, "code": CODE, "vip": "192.168.2.22/24", "node": "a", "peer_node": "b",
                                                          "node_ip": "192.168.2.20", "vip_dev": "eth0"})
    assert resp.status_code == 202
    assert updater["calls"][0][1][0] == {"code": CODE, "vip": "192.168.2.22/24", "check": False, "node": "a", "peer_node": "b",
                                         "node_ip": "192.168.2.20", "vip_dev": "eth0"}


@pytest.mark.asyncio
async def test_only_the_known_actions_are_passed_on(client, updater):
    for action in ha_manage.ACTIONS:
        resp = await client.post("/api/system/ha/action", json={"password": PW, "action": action})
        assert resp.status_code == 202, action
    assert [c[1][0] for c in updater["calls"]] == list(ha_manage.ACTIONS)
    updater["calls"].clear()
    for bad in ("promote", "sync;id", "SYNC", "", "dissolve --local-only"):
        resp = await client.post("/api/system/ha/action", json={"password": PW, "action": bad})
        assert resp.status_code in (400, 422), bad
    assert updater["calls"] == []


# ── what the updater's answers turn into ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_updater_answers_become_error_codes(client, updater):
    cases = ((updater_client.UpdaterUnavailable("x"), 503, "UPDATER_UNAVAILABLE"), (updater_client.UpdaterRejected("busy"), 409, "UPDATER_BUSY"),
             (updater_client.UpdaterRejected("not_primary"), 409, "HA_NOT_PRIMARY"), (updater_client.UpdaterRejected("already_in_pair"), 409, "HA_ALREADY_IN_PAIR"),
             (updater_client.UpdaterRejected("bad_request"), 400, "HA_INPUT_INVALID"), (updater_client.UpdaterRejected("prepare_failed"), 502, "HA_PREPARE_FAILED"),
             (updater_client.UpdaterRejected("something_new"), 409, "UPDATER_REJECTED"))
    for raised, status, code in cases:
        updater["raise"] = raised
        resp = await client.post("/api/system/ha/action", json={"password": PW, "action": "sync"})
        assert (resp.status_code, _err(resp)) == (status, code), code


@pytest.mark.asyncio
async def test_the_job_endpoint_returns_job_and_log_and_survives_a_missing_log(client, updater):
    updater["status"].update(job={"state": "running", "verb": "init"}, log=["==> Vorprüfung …"])
    assert (await client.get("/api/system/ha/job")).json() == {"job": {"state": "running", "verb": "init"}, "log": ["==> Vorprüfung …"]}
    updater["status"].update(job=None, log=None)
    assert (await client.get("/api/system/ha/job")).json() == {"job": None, "log": []}
    updater["raise"] = updater_client.UpdaterUnavailable("x")
    assert (await client.get("/api/system/ha/job")).status_code == 503
