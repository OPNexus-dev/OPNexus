"""Rule cleanup API (1.31, package 6): router, rights, locks in both directions, notification, settings.

Who may do what is enforced by TWO layers (AuthMiddleware via services/api_token_service.py, and the router itself).
The role matrix therefore runs three times: with both layers, with the middleware's rights checks switched off (the
router alone must refuse) and with the router's checks switched off (the middleware alone must refuse). A mutation
of either layer is then caught by one of the variants (tests/mutations_cleanup_1_31_api.json).
Addresses: documentation/private ranges only."""
import uuid as uuid_lib
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app import auth_middleware
from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.host import OpnsenseHost
from app.models.host_operation import HostOperation
from app.models.rule_cleanup import RuleCleanupItem, RuleCleanupOperation
from app.models.rule_deployment import RuleDeployment
from app.routers import rule_cleanup as router_mod
from app.schemas.rule_cleanup import CleanupItemRead, CleanupOperationRead
from app.services import api_token_service, notification_checks
from app.services import rule_cleanup_service as svc
from tests.rule_cleanup_fake import MGMT_SOURCE, R_ALIAS, R_BASE, R_BLOCK, R_DUP, R_MGMT, R_TAIL, R_UNUSED
from tests.test_api_tokens import _bearer, _login, _make
from tests.test_rule_cleanup import (  # noqa: F401  (fixtures are used by name)
    FW1, FW2, _backdate, _confirmed_disable, _fresh_cache, _items, _now, _op, _settings, cluster, env, refused,
)

PW = "cleanup-api-test-pw-1"
NOHOST = "00000000-0000-0000-0000-0000000000aa"
NOID = "00000000-0000-0000-0000-0000000000bb"


# ── Principals ────────────────────────────────────────────────────────────

async def _user(client, name: str, role: str):
    resp = await client.post("/api/users", json={"username": name, "password": PW, "role": role})
    assert resp.status_code in (201, 409), resp.text  # the account survives between tests
    return await _login(name, PW)


async def _token_of(session, name: str, scope: str):
    resp = await session.post("/api/tokens", json={"name": name, "scope": scope, "current_password": PW})
    assert resp.status_code == 201, resp.text
    return _bearer(resp.json()["token"])


@pytest_asyncio.fixture
async def who(client):
    """Every kind of caller: sessions of an admin, an operator and a read-only account, and tokens of each scope
    (an admin's read/deploy/admin, an operator's deploy, a read-only account's read)."""
    operator = await _user(client, "cl-operator", "operator")
    read_only = await _user(client, "cl-readonly", "read_only")
    callers = {
        "admin": client, "operator": operator, "read_only": read_only,
        "tok_read": _bearer((await _make(client, "read", name="cl-read"))["token"]),
        "tok_deploy": _bearer((await _make(client, "deploy", name="cl-deploy"))["token"]),
        "tok_admin": _bearer((await _make(client, "admin", name="cl-admin"))["token"]),
        "tok_op_deploy": await _token_of(operator, "cl-op-deploy", "deploy"),
        "tok_ro_read": await _token_of(read_only, "cl-ro-read", "read"),
    }
    yield callers
    for name, session in callers.items():
        if name != "admin":
            await session.aclose()


async def _governance(client, may_deploy: bool):
    assert (await client.put("/api/settings/governance", json={"operators_may_deploy": may_deploy})).status_code == 200


# ── The role matrix ───────────────────────────────────────────────────────

ROUTES = {
    "list": ("GET", "", None),
    "get": ("GET", "/{op}", None),
    "preview": ("POST", "/preview", {"kind": "disable", "uuids": ["a"]}),
    "cancel": ("POST", "/{op}/cancel", None),
    "deploy": ("POST", "/{op}/deploy", {"confirm_name": "x"}),
    "confirm": ("POST", "/{op}/confirm", None),
    "revert": ("POST", "/{op}/revert", None),
    "retry": ("POST", "/{op}/retry", None),
    "restore": ("POST", "/items/{op}/restore", {"confirm_name": "x"}),
}
READ_ONLY_CODES = {"READ_ONLY_ACCESS", "API_TOKEN_SCOPE_FORBIDDEN"}


def verdict(route: str, caller: str, may_deploy: bool):
    """None: the caller passes the gate (the unknown host then answers 404). Otherwise the acceptable 403 codes."""
    if route in ("list", "get"):
        return None
    if caller in ("read_only", "tok_ro_read"):
        return READ_ONLY_CODES
    if caller == "tok_read":
        return {"API_TOKEN_SCOPE_FORBIDDEN"}
    if route in ("preview", "cancel"):
        return None
    if route == "confirm":  # a human in a browser session; no token, whatever its scope
        return None if caller in ("admin", "operator") else {"API_TOKEN_SCOPE_FORBIDDEN"}
    if route in ("deploy", "retry", "restore"):  # admin only, even though operators_may_deploy is on
        if caller in ("admin", "tok_admin"):
            return None
        return {"admin": None, "operator": {"ADMIN_REQUIRED"}, "tok_deploy": {"API_TOKEN_SCOPE_FORBIDDEN"},
                "tok_op_deploy": {"API_TOKEN_SCOPE_FORBIDDEN", "ADMIN_REQUIRED"}}[caller]
    assert route == "revert"  # an admin, or an operator while operators_may_deploy is on
    if caller in ("admin", "tok_deploy", "tok_admin"):
        return None
    return None if may_deploy else {"DEPLOY_NEEDS_ADMIN"}


async def _gate(session, route: str):
    method, path, body = ROUTES[route]
    url = f"/api/hosts/{NOHOST}/rule-cleanup" + path.format(op=NOID)
    resp = await session.request(method, url, **({"json": body} if body is not None else {}))
    code = (resp.json().get("detail") or {}).get("code") if resp.headers.get("content-type", "").startswith("application/json") else None
    return resp.status_code, code


@pytest.mark.asyncio
@pytest.mark.parametrize("may_deploy", [True, False])
@pytest.mark.parametrize("variant", ["both", "router_only", "middleware_only"])
async def test_every_route_for_every_caller(client, who, monkeypatch, variant, may_deploy):
    await _governance(client, may_deploy)
    try:
        if variant == "router_only":  # switch the middleware's rights checks off: the router must still refuse
            monkeypatch.setattr(api_token_service, "denies", lambda *a, **k: False)
            monkeypatch.setattr(api_token_service, "operator_denies", lambda *a, **k: False)
            monkeypatch.setattr(api_token_service, "is_rollout", lambda *a, **k: False)

            class _Everything:  # the read-only gate of the middleware lets this request through
                def __contains__(self, _):
                    return True
            monkeypatch.setattr(auth_middleware, "_READ_ONLY_ALLOWED_WRITES", _Everything())
        if variant == "middleware_only":  # switch the router's checks off: the middleware must still refuse
            async def _nothing_async(*a, **k):
                return None
            for name in ("_writer", "_admin_only", "_session_only"):
                monkeypatch.setattr(router_mod, name, lambda *a, **k: None)
            monkeypatch.setattr(router_mod, "_deployer", _nothing_async)
        problems = []
        for route in ROUTES:
            for caller, session in who.items():
                want = verdict(route, caller, may_deploy)
                got = await _gate(session, route)
                ok = got == (404, "HOST_NICHT_GEFUNDEN") if want is None else (got[0] == 403 and got[1] in want)
                if not ok:
                    problems.append((route, caller, got, want))
        assert not problems, problems
    finally:
        await _governance(client, True)


@pytest.mark.parametrize("method,path,session_only,admin_only,rollout", [
    ("POST", "/api/hosts/h/rule-cleanup/preview", False, False, False),
    ("POST", "/api/hosts/h/rule-cleanup/o/cancel", False, False, False),
    ("POST", "/api/hosts/h/rule-cleanup/o/confirm", True, False, False),
    ("POST", "/api/hosts/h/rule-cleanup/o/confirm/", True, False, False),
    ("POST", "/api/hosts/h/rule-cleanup/o/deploy", False, True, True),
    ("POST", "/api/hosts/h/rule-cleanup/o/deploy/", False, True, False),
    ("POST", "/api/hosts/h/rule-cleanup/o/revert", False, False, True),
    ("POST", "/api/hosts/h/rule-cleanup/o/retry", False, True, True),
    ("POST", "/api/hosts/h/rule-cleanup/items/i/restore", False, True, False),
    ("GET", "/api/hosts/h/rule-cleanup", False, False, False),
    ("GET", "/api/hosts/h/rule-cleanup/o", False, False, False),
])
def test_pattern_table(method, path, session_only, admin_only, rollout):
    denied_for_read = method == "POST"
    assert api_token_service.denies("read", method, path) is denied_for_read
    # a token below scope admin: admin-only routes and confirm are closed
    assert api_token_service.denies("deploy", method, path) is (session_only or admin_only)
    # an admin-scope token reaches everything except what only a session may do
    assert api_token_service.denies("admin", method, path) is session_only
    assert api_token_service.operator_denies(method, path) is admin_only
    assert api_token_service.is_rollout(method, path) is rollout


def test_governance_settings_are_admin_writes_for_operators_and_deploy_tokens():
    assert api_token_service.operator_denies("PUT", "/api/settings/governance") is True
    assert api_token_service.denies("deploy", "PUT", "/api/settings/governance") is True
    assert api_token_service.denies("admin", "PUT", "/api/settings/governance") is False


# ── A full run over the API: shapes, audit, input forms ──────────────────

def _base(env_or_id) -> str:
    return f"/api/hosts/{getattr(env_or_id, 'host_id', env_or_id)}/rule-cleanup"


async def _audit(**filters):
    async with SessionLocal() as db:
        rows = (await db.scalars(select(AuditEvent).where(*[getattr(AuditEvent, k) == v for k, v in filters.items()])
                                 .order_by(AuditEvent.created_at))).all()
    return rows


async def _open(session, env, uuids=(R_UNUSED, R_DUP), name="fw-a"):
    """Preview + deploy over the API; the run waits for its confirmation."""
    pv = await session.post(_base(env) + "/preview", json={"kind": "disable", "uuids": list(uuids)})
    assert pv.status_code == 200, pv.text
    op = await session.post(f"{_base(env)}/{pv.json()['preview_id']}/deploy", json={"confirm_name": name})
    assert op.status_code == 200, op.text
    return op.json()


@pytest.mark.asyncio
async def test_preview_deploy_confirm_over_the_api_with_shapes_and_audit(client, env):
    pv = await client.post(_base(env) + "/preview", json={"kind": "disable", "uuids": [R_UNUSED, R_DUP]})
    assert pv.status_code == 200, pv.text
    body = pv.json()
    assert {"preview_id", "expires_at", "kind", "plan", "max_rules", "window_seconds", "operation"} <= set(body)
    assert body["max_rules"] == 10 and body["kind"] == "disable" and body["window_seconds"] > 0
    assert {i["uuid"] for i in body["plan"]["nodes"][0]["items"]} == {R_UNUSED, R_DUP}
    operation = body["operation"]
    assert set(operation) == set(CleanupOperationRead.model_fields)  # the UI gets exactly the documented fields
    assert operation["status"] == "preview" and operation["plan"] and operation["items"] == []
    assert operation["actor"] == "admin" and operation["host_name"] == "fw-a"
    # The middleware does not log /preview; the service writes the one entry (and it is no history change).
    assert [(a.action, a.status) for a in await _audit(kind="rule_cleanup")] == [("preview", "requested")]
    assert await _audit(kind="host", action="preview") == []
    assert env.fw.writes() == []

    pid = body["preview_id"]
    wrong = await client.post(f"{_base(env)}/{pid}/deploy", json={"confirm_name": "fw-x"})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "HOSTNAMEN_EXAKT_EINGEBEN"
    deployed = await client.post(f"{_base(env)}/{pid}/deploy", json={"confirm_name": "fw-a"})
    assert deployed.status_code == 200, deployed.text
    op = deployed.json()
    assert set(op) == set(CleanupOperationRead.model_fields) and all(set(i) == set(CleanupItemRead.model_fields) for i in op["items"])
    assert op["status"] == "awaiting_confirm" and op["revert_deadline"] and op["plan"] is None
    assert (op["can_confirm"], op["can_revert"], op["can_retry"]) == (True, True, False)
    assert {(i["rule_uuid"], i["state"], i["host_name"]) for i in op["items"]} == {(R_UNUSED, "disabled", "fw-a"), (R_DUP, "disabled", "fw-a")}
    assert env.fw.get(R_UNUSED)["enabled"] == "0"
    # Audit: one request pair from the middleware (kind host), one entry per rule from the service - no duplicates.
    # the wrong-name attempt and the real one: a request entry and an outcome each, from the middleware only
    assert sorted(a.status for a in await _audit(kind="host", action="deploy")) == ["accepted", "rejected", "requested", "requested"]
    assert [a.status for a in await _audit(kind="rule_cleanup", action="disable")] == ["accepted", "accepted"]
    assert await _audit(kind="rule_cleanup", action="deploy") == [] and await _audit(kind="host", action="disable") == []

    listing = (await client.get(_base(env))).json()
    assert [o["id"] for o in listing] == [pid] and listing[0]["status"] == "awaiting_confirm"
    one = (await client.get(f"{_base(env)}/{pid}")).json()
    assert one == listing[0]
    confirmed = await client.post(f"{_base(env)}/{pid}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    c = confirmed.json()
    assert c["status"] == "confirmed" and c["revert_deadline"] is None and {i["state"] for i in c["items"]} == {"confirmed"}
    assert (c["can_confirm"], c["can_revert"], c["can_retry"]) == (False, False, False)
    assert [a.status for a in await _audit(kind="rule_cleanup", action="confirm")] == ["confirmed", "confirmed"]
    assert sorted(a.status for a in await _audit(kind="host", action="confirm")) == ["accepted", "requested"]
    again = await client.post(f"{_base(env)}/{pid}/confirm")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "BEREINIGUNG_FRIST_ABGELAUFEN"
    missing = await client.get(f"{_base(env)}/{NOID}")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "BEREINIGUNG_NICHT_GEFUNDEN"
    assert (await client.get(f"/api/hosts/{NOHOST}/rule-cleanup")).status_code == 404


@pytest.mark.asyncio
async def test_preview_input_forms_and_validation(client, env):
    url = _base(env) + "/preview"
    # items[] with per-rule kind/mode and acknowledgement; a "probable" rule needs the acknowledgement
    plain = {"items": [{"uuid": R_UNUSED, "mode": "disable"}, {"uuid": R_BLOCK, "kind": "disable"}]}
    got = await client.post(url, json=plain)
    assert got.status_code == 409 and got.json()["detail"]["code"] == "BEREINIGUNG_BESTAETIGUNG_FEHLT"
    assert R_BLOCK in got.json()["detail"]["params"]["uuids"]
    ack = {"items": [{"uuid": R_UNUSED, "mode": "disable"}, {"uuid": R_BLOCK, "kind": "disable", "ack": True}]}
    done = await client.post(url, json=ack)
    assert done.status_code == 200, done.text
    assert {i["uuid"] for i in done.json()["plan"]["nodes"][0]["items"]} == {R_UNUSED, R_BLOCK}
    assert done.json()["plan"]["acknowledged"] == [R_BLOCK]
    # the old form: kind + uuids + acknowledge
    old = await client.post(url, json={"kind": "disable", "uuids": [R_UNUSED, R_BLOCK], "acknowledge": [R_BLOCK]})
    assert old.status_code == 200 and (await _op(done.json()["preview_id"])).status == "cancelled"  # one preview per actor
    # bad input
    for bad in ({"uuids": [R_UNUSED]}, {"items": []}):
        r = await client.post(url, json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "BEREINIGUNG_AUSWAHL_UNGUELTIG", bad
    for bad in ({"kind": "disable", "uuids": []}, {"kind": "disable", "uuids": ["../x"]}):
        r = await client.post(url, json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "BEREINIGUNG_AUSWAHL_UNGUELTIG", bad
    mixed = {"items": [{"uuid": R_UNUSED, "kind": "disable"}, {"uuid": R_DUP, "kind": "delete"}]}
    assert (await client.post(url, json=mixed)).status_code == 422
    assert (await client.post(url, json={"kind": "restore", "uuids": [R_UNUSED]})).status_code == 422
    assert (await client.post(url, json={"kind": "disable", "uuids": [R_UNUSED], "unused_days": 0})).status_code == 422
    assert (await client.post(f"/api/hosts/{NOHOST}/rule-cleanup/preview", json={"kind": "disable", "uuids": [R_UNUSED]})).status_code == 404
    # the settings reach the service: the limit and the fail-closed lock without management sources
    assert (await client.put("/api/settings/governance", json={"cleanup_max_rules": 1})).status_code == 200
    many = await client.post(url, json={"kind": "disable", "uuids": [R_UNUSED, R_DUP]})
    assert many.status_code == 422 and many.json()["detail"]["code"] == "BEREINIGUNG_ZU_VIELE_REGELN" and many.json()["detail"]["params"]["max"] == 1
    assert (await client.put("/api/settings/governance", json={"cleanup_management_sources": []})).status_code == 200
    locked = await client.post(url, json={"kind": "disable", "uuids": [R_UNUSED]})
    assert locked.status_code == 409 and locked.json()["detail"]["code"] == "BEREINIGUNG_VERWALTUNGSQUELLEN_FEHLEN"


@pytest.mark.asyncio
async def test_a_preview_belongs_to_its_actor_and_can_be_cancelled(client, env, who):
    operator = who["operator"]
    mine = (await operator.post(_base(env) + "/preview", json={"kind": "disable", "uuids": [R_UNUSED]})).json()
    assert mine["operation"]["actor"] == "cl-operator"
    # the admin neither sees it as his own nor deploys it; the operator cannot deploy at all
    stranger = await client.post(f"{_base(env)}/{mine['preview_id']}/deploy", json={"confirm_name": "fw-a"})
    assert stranger.status_code == 404 and stranger.json()["detail"]["code"] == "BEREINIGUNG_NICHT_GEFUNDEN"
    refused_op = await operator.post(f"{_base(env)}/{mine['preview_id']}/deploy", json={"confirm_name": "fw-a"})
    assert refused_op.status_code == 403 and refused_op.json()["detail"]["code"] == "ADMIN_REQUIRED"
    assert (await client.post(f"{_base(env)}/{mine['preview_id']}/cancel")).status_code == 404  # not the admin's
    cancelled = await operator.post(f"{_base(env)}/{mine['preview_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    # the admin's refused attempt (not his preview) and the operator's real cancel: a request entry and an outcome each
    assert sorted(a.status for a in await _audit(kind="host", action="cancel")) == ["accepted", "rejected", "requested", "requested"]
    again = await operator.post(f"{_base(env)}/{mine['preview_id']}/cancel")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "BEREINIGUNG_AKTION_NICHT_MOEGLICH"
    gone = await client.post(f"{_base(env)}/{mine['preview_id']}/deploy", json={"confirm_name": "fw-a"})
    assert gone.status_code == 404  # (not the admin's preview)
    assert env.fw.writes() == []
    # an operator prepares, the admin prepares his own and executes; the operator may confirm afterwards
    own = (await client.post(_base(env) + "/preview", json={"kind": "disable", "uuids": [R_UNUSED]})).json()
    deployed = await client.post(f"{_base(env)}/{own['preview_id']}/deploy", json={"confirm_name": "fw-a"})
    assert deployed.status_code == 200
    confirmed = await operator.post(f"{_base(env)}/{own['preview_id']}/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_confirm_never_works_with_a_token_and_changes_nothing(client, env, who):
    op = await _open(client, env)
    for name in ("tok_admin", "tok_deploy", "tok_op_deploy"):
        r = await who[name].post(f"{_base(env)}/{op['id']}/confirm")
        assert r.status_code == 403 and r.json()["detail"]["code"] == "API_TOKEN_SCOPE_FORBIDDEN", name
    state = await _op(op["id"])
    assert state.status == "awaiting_confirm" and state.revert_deadline is not None
    assert {i.state for i in (await _items(op["id"])).values()} == {"disabled"}
    # the same token may read the run, and the browser session confirms
    assert (await who["tok_read"].get(f"{_base(env)}/{op['id']}")).json()["can_confirm"] is True
    assert (await client.post(f"{_base(env)}/{op['id']}/confirm")).json()["status"] == "confirmed"
    denied = await _audit(kind="host", action="confirm", status="rejected")
    assert not denied  # the middleware refused before the router: no request entry for a refused token confirm


@pytest.mark.asyncio
async def test_revert_is_for_admins_and_for_operators_only_while_they_may_deploy(client, env, who):
    op = await _open(client, env)
    url = f"{_base(env)}/{op['id']}/revert"
    await _governance(client, False)
    try:
        assert (await who["operator"].post(url)).status_code == 403
        assert (await who["tok_op_deploy"].post(url)).status_code == 403
        assert (await who["read_only"].post(url)).status_code == 403
        assert (await _op(op["id"])).status == "awaiting_confirm" and env.fw.get(R_UNUSED)["enabled"] == "0"
    finally:
        await _governance(client, True)
    reverted = await who["operator"].post(url)
    assert reverted.status_code == 200, reverted.text
    r = reverted.json()
    assert r["status"] == "reverted" and {i["state"] for i in r["items"]} == {"reverted"} and not r["can_revert"]
    assert env.fw.get(R_UNUSED)["enabled"] == "1" and env.fw.get(R_DUP)["enabled"] == "1"
    again = await client.post(url)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "BEREINIGUNG_AKTION_NICHT_MOEGLICH"


@pytest.mark.asyncio
async def test_retry_after_a_failed_revert_is_admin_only(client, env, who):
    op = await _open(client, env)
    env.fw.fail[("toggle", R_DUP)] = httpx.Response(200, json={"result": "failed"})
    failed = await client.post(f"{_base(env)}/{op['id']}/revert")
    assert failed.status_code == 200 and failed.json()["status"] == "revert_failed" and failed.json()["can_retry"] is True
    del env.fw.fail[("toggle", R_DUP)]
    url = f"{_base(env)}/{op['id']}/retry"
    for name, code in (("operator", "ADMIN_REQUIRED"), ("tok_deploy", "API_TOKEN_SCOPE_FORBIDDEN"), ("read_only", "READ_ONLY_ACCESS")):
        r = await who[name].post(url)
        assert r.status_code == 403 and r.json()["detail"]["code"] == code, name
    assert (await _op(op["id"])).status == "revert_failed"
    done = await who["tok_admin"].post(url)
    assert done.status_code == 200 and done.json()["status"] == "reverted" and env.fw.get(R_DUP)["enabled"] == "1"
    nothing = await client.post(url)
    assert nothing.status_code == 409 and nothing.json()["detail"]["code"] == "BEREINIGUNG_AKTION_NICHT_MOEGLICH"


@pytest.mark.asyncio
async def test_delete_and_restore_over_the_api(client, env, who):
    await _confirmed_disable(env.host_id, [R_UNUSED])
    pv = (await client.post(_base(env) + "/preview", json={"kind": "delete", "uuids": [R_UNUSED]})).json()
    assert pv["operation"]["kind"] == "delete"
    done = await client.post(f"{_base(env)}/{pv['preview_id']}/deploy", json={"confirm_name": "fw-a"})
    assert done.status_code == 200, done.text
    op = done.json()
    assert op["status"] == "done" and op["kind"] == "delete" and (op["can_confirm"], op["can_revert"], op["can_retry"]) == (False,) * 3
    item = op["items"][0]
    assert item["state"] == "deleted" and item["restorable"] is True and item["has_snapshot"] is True
    assert env.fw.get(R_UNUSED) is None
    assert [a.status for a in await _audit(kind="rule_cleanup", action="delete")] == ["accepted"]
    url = f"{_base(env)}/items/{item['id']}/restore"
    for name, code in (("operator", "ADMIN_REQUIRED"), ("tok_deploy", "API_TOKEN_SCOPE_FORBIDDEN"), ("read_only", "READ_ONLY_ACCESS")):
        r = await who[name].post(url, json={"confirm_name": "fw-a"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == code, name
    wrong = await client.post(url, json={"confirm_name": "nope"})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "HOSTNAMEN_EXAKT_EINGEBEN"
    assert env.fw.get(R_UNUSED) is None
    restored = await who["tok_admin"].post(url, json={"confirm_name": "fw-a"})
    assert restored.status_code == 200, restored.text
    r = restored.json()
    assert r["kind"] == "restore" and r["status"] == "done" and r["warnings"] == []
    new = r["items"][0]["restored_uuid"]
    assert new and r["items"][0]["state"] == "restored" and env.fw.get(new)["enabled"] == "0"
    listed = {o["id"]: o for o in (await client.get(_base(env))).json()}
    source = next(i for i in listed[op["id"]]["items"] if i["id"] == item["id"])
    assert source["restorable"] is False and source["restored_uuid"] == new
    assert [a.status for a in await _audit(kind="rule_cleanup", action="restore")] == ["accepted"]
    again = await client.post(url, json={"confirm_name": "fw-a"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "BEREINIGUNG_AKTION_NICHT_MOEGLICH"
    nothing = await client.post(f"{_base(env)}/{op['id']}/retry")
    assert nothing.status_code == 409  # nothing unclear in a finished delete


@pytest.mark.asyncio
async def test_restore_reports_a_lost_position_as_a_warning(client, env):
    await _confirmed_disable(env.host_id, [R_UNUSED])
    pv = (await client.post(_base(env) + "/preview", json={"kind": "delete", "uuids": [R_UNUSED]})).json()
    op = (await client.post(f"{_base(env)}/{pv['preview_id']}/deploy", json={"confirm_name": "fw-a"})).json()
    env.fw.rules = [r for r in env.fw.rules if r["uuid"] != R_BLOCK]  # the anchor is gone
    r = await client.post(f"{_base(env)}/items/{op['items'][0]['id']}/restore", json={"confirm_name": "fw-a"})
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == ["BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR"]
    assert r.json()["items"][0]["message"] == "BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR"
    missing = await client.post(f"{_base(env)}/items/{NOID}/restore", json={"confirm_name": "fw-a"})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "BEREINIGUNG_NICHT_GEFUNDEN"


# ── Settings ──────────────────────────────────────────────────────────────

GOV = "/api/settings/governance"


@pytest.mark.asyncio
async def test_management_sources_are_validated_normalised_and_audited(client):
    put = await client.put(GOV, json={"cleanup_management_sources": [" 203.0.113.10 ", "10.20.30.5/24", "203.0.113.10", "2001:db8::1"],
                                      "cleanup_max_rules": 25, "cleanup_delete_min_days": 30})
    assert put.status_code == 200, put.text
    got = put.json()
    assert got["cleanup_management_sources"] == ["203.0.113.10", "10.20.30.0/24", "2001:db8::1"]
    assert got["cleanup_management_sources_configured"] is True and got["cleanup_max_rules"] == 25 and got["cleanup_delete_min_days"] == 30
    assert (await client.get(GOV)).json() == got
    audit = await _audit(kind="settings", action="cleanup_settings")
    assert len(audit) == 1 and audit[0].actor == "admin"
    changes = audit[0].details["changes"]
    assert changes["cleanup_management_sources"]["new"] == got["cleanup_management_sources"]
    assert changes["cleanup_max_rules"] == {"old": 10, "new": 25}
    # an unchanged value writes no further entry; a partial update leaves the rest alone
    assert (await client.put(GOV, json={"cleanup_max_rules": 25, "operators_may_deploy": True})).json()["cleanup_delete_min_days"] == 30
    assert len(await _audit(kind="settings", action="cleanup_settings")) == 1
    bad_sources = [["fw.example.test"], ["203.0.113.999"], ["0.0.0.0/0"], ["::/0"], ["203.0.113.1", ""], [f"10.20.30.{i}" for i in range(21)], ["10.20.30.0/33"]]
    for sources in bad_sources:
        r = await client.put(GOV, json={"cleanup_management_sources": sources})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "BEREINIGUNG_EINSTELLUNG_UNGUELTIG", sources
        assert r.json()["detail"]["params"]["field"] == "cleanup_management_sources"
    for field, value in (("cleanup_max_rules", 0), ("cleanup_max_rules", 26), ("cleanup_delete_min_days", 0), ("cleanup_delete_min_days", 366)):
        r = await client.put(GOV, json={field: value})
        assert r.status_code == 422 and r.json()["detail"]["params"]["field"] == field, (field, value)
    assert (await client.get(GOV)).json() == got  # nothing of the refused requests was stored
    assert len(await _audit(kind="settings", action="cleanup_settings")) == 1
    assert (await client.put(GOV, json={"cleanup_management_sources": [f"10.20.30.{i}" for i in range(20)]})).status_code == 200


@pytest.mark.asyncio
async def test_management_ports_are_validated_normalised_audited_and_hidden(client, who):  # review finding 3
    put = await client.put(GOV, json={"cleanup_management_ports": [8443, "2222", 8443]})
    assert put.status_code == 200, put.text
    assert put.json()["cleanup_management_ports"] == [2222, 8443] and put.json()["cleanup_management_ports_configured"] is True
    audit = await _audit(kind="settings", action="cleanup_settings")
    assert audit[-1].details["changes"]["cleanup_management_ports"] == {"old": [], "new": [2222, 8443]}
    for ports in ([0], [65536], ["ssh"], [True], [-1], list(range(1, 22))):
        r = await client.put(GOV, json={"cleanup_management_ports": ports})
        assert r.status_code == 422 and r.json()["detail"]["params"]["field"] == "cleanup_management_ports", ports
    assert (await client.get(GOV)).json()["cleanup_management_ports"] == [2222, 8443]
    for name in ("operator", "read_only"):
        seen = (await who[name].get(GOV)).json()
        assert seen["cleanup_management_ports"] == [] and seen["cleanup_management_ports_configured"] is True, name
        r = await who[name].put(GOV, json={"cleanup_management_ports": [1]})
        assert r.status_code == 403, name
    assert (await client.put(GOV, json={"cleanup_management_ports": []})).json()["cleanup_management_ports_configured"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["both", "router_only"])
async def test_only_an_admin_writes_the_cleanup_settings(client, who, monkeypatch, variant):
    await client.put(GOV, json={"cleanup_management_sources": ["203.0.113.10"]})
    if variant == "router_only":
        monkeypatch.setattr(api_token_service, "denies", lambda *a, **k: False)
        monkeypatch.setattr(api_token_service, "operator_denies", lambda *a, **k: False)
    # router_only: an admin-owned deploy/read token IS an admin account: only the token layer (switched off) stops it
    attempts = {"operator": {"ADMIN_REQUIRED"}, "tok_op_deploy": {"API_TOKEN_SCOPE_FORBIDDEN", "ADMIN_REQUIRED"},
                "read_only": {"READ_ONLY_ACCESS", "ADMIN_REQUIRED"}}
    if variant == "both":
        attempts.update({"tok_deploy": {"API_TOKEN_SCOPE_FORBIDDEN"}, "tok_read": {"API_TOKEN_SCOPE_FORBIDDEN"}})
    for name, codes in attempts.items():
        r = await who[name].put(GOV, json={"cleanup_management_sources": ["198.51.100.7"], "cleanup_max_rules": 3})
        assert r.status_code == 403 and r.json()["detail"]["code"] in codes, (name, r.text)
    assert (await client.get(GOV)).json()["cleanup_management_sources"] == ["203.0.113.10"]
    assert (await client.get(GOV)).json()["cleanup_max_rules"] == 10
    # an admin-scope token acts as the admin and the audit names the token
    ok = await who["tok_admin"].put(GOV, json={"cleanup_management_sources": ["198.51.100.7"]})
    assert ok.status_code == 200, ok.text
    assert [a.actor for a in await _audit(kind="settings", action="cleanup_settings")][-1] == "admin (API: cl-admin)"


@pytest.mark.asyncio
async def test_others_only_see_whether_management_sources_are_set(client, who):
    await client.put(GOV, json={"cleanup_management_sources": ["203.0.113.10"]})
    assert (await client.get(GOV)).json()["cleanup_management_sources"] == ["203.0.113.10"]
    for name in ("operator", "read_only"):
        seen = (await who[name].get(GOV)).json()
        assert seen["cleanup_management_sources"] == [] and seen["cleanup_management_sources_configured"] is True, name
        assert seen["cleanup_max_rules"] == 10 and seen["operators_may_deploy"] is True


# ── Locks in both directions ──────────────────────────────────────────────

async def _central_rule(client, env, **extra):
    resp = await client.post("/api/rules", json={
        "description": "managed ssh", "interface": "lan", "direction": "in", "protocol": "tcp", "action": "pass",
        "source_net": "any", "destination_net": "lan", "destination_port": "22",
        "target_host_ids": [str(env.host_id)], **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _deployed_row(rule_id, host_id, firewall_uuid, status="success"):
    async with SessionLocal() as db:
        dep = RuleDeployment(rule_id=uuid_lib.UUID(rule_id), host_id=host_id, status=status, opnsense_rule_uuid=firewall_uuid)
        db.add(dep)
        await db.commit()


@pytest.mark.asyncio
async def test_an_open_cleanup_blocks_other_writes_to_the_firewall(client, env):
    rule = await _central_rule(client, env)
    alias = await client.post("/api/aliases", json={"name": "cl_alias", "type": "host", "content": "203.0.113.5",
                                                    "target_host_ids": [str(env.host_id)]})
    assert alias.status_code == 201, alias.text
    hid = env.host_id
    await _open(client, env)
    writes = list(env.fw.writes())
    blocked = [
        ("POST", f"/api/rules/{rule['id']}/deploy", None),
        ("POST", f"/api/aliases/{alias.json()['id']}/deploy", None),
        ("POST", f"/api/hosts/{hid}/snapshots", {"name": "x"}),
        ("POST", f"/api/hosts/{hid}/services/dhcpd/restart", None),
        ("POST", f"/api/hosts/{hid}/maintenance/check-updates", None),
        ("POST", f"/api/hosts/{hid}/network/changes", {"kind": "vlan", "action": "add", "fields": {}, "preview_token": "0" * 64}),
        ("DELETE", f"/api/hosts/{hid}/snapshots/abc", None),
    ]
    for method, path, body in blocked:
        r = await client.request(method, path, **({"json": body} if body is not None else {}))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", (path, r.text)
    assert env.fw.writes() == writes  # nothing reached the firewall
    # the cleanup's own follow-up steps are not blocked by itself, and a second cleanup is refused with its own code
    second = await client.post(_base(env) + "/preview", json={"kind": "disable", "uuids": [R_BLOCK], "acknowledge": [R_BLOCK]})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "BEREINIGUNG_OFFENE_OPERATION"
    assert second.json()["detail"]["params"]["reason"] == "cleanup"
    op = (await client.get(_base(env))).json()[0]
    assert (await client.post(f"{_base(env)}/{op['id']}/revert")).status_code == 200
    again = await client.post(f"/api/hosts/{hid}/maintenance/check-updates")
    assert again.status_code != 409 or again.json()["detail"]["code"] != "OPEN_CLEANUP_BLOCKS_DEPLOY"


@pytest.mark.asyncio
async def test_the_block_covers_the_cluster_and_the_maintenance_inspection(cluster, client):
    from app.services.host_maintenance_service import _shared_change_blockers, ensure_no_maintenance, ensure_no_open_cleanup, peers
    pv = await client.post(_base(cluster.a) + "/preview", json={"kind": "disable", "uuids": [R_UNUSED]})
    assert pv.status_code == 200, pv.text
    assert (await client.post(f"{_base(cluster.a)}/{pv.json()['preview_id']}/deploy", json={"confirm_name": "fw-a"})).status_code == 200
    async with SessionLocal() as db:
        a, b = await db.get(OpnsenseHost, cluster.a), await db.get(OpnsenseHost, cluster.b)
        for host in (a, b):  # the partner node is just as busy
            for call in (ensure_no_maintenance, ensure_no_open_cleanup):
                with pytest.raises(Exception) as info:
                    await call(db, [host])
                assert info.value.detail["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY"
        blockers = await _shared_change_blockers(db, await peers(db, b))
        assert any("Bereinigung" in text for text in blockers)
        await ensure_no_maintenance(db, [a], cleanup=False)  # the cleanup service's own check does not trip over itself
    # deleting or re-addressing either node is refused while it is open
    for host_id in (cluster.a, cluster.b):
        r = await client.delete(f"/api/hosts/{host_id}")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", host_id
    op = (await client.get(_base(cluster.a))).json()[0]
    assert (await client.post(f"{_base(cluster.a)}/{op['id']}/revert")).status_code == 200
    async with SessionLocal() as db:
        assert await _shared_change_blockers(db, await peers(db, await db.get(OpnsenseHost, cluster.b))) == []
        await ensure_no_maintenance(db, [await db.get(OpnsenseHost, cluster.b)])


@pytest.mark.asyncio
async def test_host_delete_and_readdressing_are_refused_while_a_cleanup_is_open(client, env):
    hid = env.host_id
    await _open(client, env)
    gone = await client.delete(f"/api/hosts/{hid}")
    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY"
    for body in ({"base_url": "https://203.0.113.99"}, {"enabled": False}, {"cluster_name": "ha-x"}, {"clear_cluster_name": True}):
        r = await client.patch(f"/api/hosts/{hid}", json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", body
    assert (await client.patch(f"/api/hosts/{hid}", json={"name": "fw-a2"})).status_code == 200  # a rename is harmless
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, hid)
        assert host.base_url == FW1 and host.enabled and host.name == "fw-a2"
    op = (await client.get(_base(env))).json()[0]
    assert (await client.post(f"{_base(env)}/{op['id']}/revert")).status_code == 200
    assert (await client.delete(f"/api/hosts/{hid}")).status_code == 204  # settled: the host may go (its runs go with it)
    async with SessionLocal() as db:
        assert (await db.scalars(select(RuleCleanupOperation))).all() == []


@pytest.mark.asyncio
async def test_a_confirmed_cleanup_blocks_nothing(client, env):
    op = await _open(client, env)
    assert (await client.post(f"{_base(env)}/{op['id']}/confirm")).status_code == 200
    async with SessionLocal() as db:
        assert not await svc.has_open_cleanup(db, [env.host_id])
    r = await client.post(f"/api/hosts/{env.host_id}/maintenance/check-updates")
    assert r.status_code != 409 or r.json()["detail"]["code"] != "OPEN_CLEANUP_BLOCKS_DEPLOY"


@pytest.mark.asyncio
async def test_rule_deletion_and_removal_of_targets_wait_for_the_cleanup(client, env):
    rule = await _central_rule(client, env, target_host_ids=[])
    await _deployed_row(rule["id"], env.host_id, R_TAIL)
    token = (await client.get(f"/api/rules/{rule['id']}/preview")).json()["token"]
    await _open(client, env)
    env.fw.calls.clear()
    deleted = await client.delete(f"/api/rules/{rule['id']}")
    assert deleted.status_code == 409 and deleted.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY"
    removed = await client.post(f"/api/rules/{rule['id']}/deployments/{env.host_id}/remove", json={"preview_token": token})
    assert removed.status_code == 409 and removed.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", removed.text
    assert env.fw.writes() == [] and env.fw.get(R_TAIL) is not None
    op = (await client.get(_base(env))).json()[0]
    await client.post(f"{_base(env)}/{op['id']}/revert")
    assert (await client.delete(f"/api/rules/{rule['id']}")).status_code == 204  # the same call works afterwards
    assert env.fw.get(R_TAIL) is None


@pytest.mark.asyncio
async def test_hierarchy_rule_deletion_and_reordering_wait_for_the_cleanup(client, env):
    from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
    from tests.test_policy_hierarchy import _create_group, _hrule_payload
    group = await _create_group(client, "Cleanup-Root")
    assert (await client.patch(f"/api/hosts/{env.host_id}", json={"policy_hierarchy_group_id": group["id"]})).status_code == 200
    rule = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"], description="guard"))).json()
    async with SessionLocal() as db:
        db.add(PolicyHierarchyRuleDeployment(policy_hierarchy_id=uuid_lib.UUID(rule["id"]), host_id=env.host_id, status="success",
                                             opnsense_policy_hierarchy_uuid=R_TAIL))
        await db.commit()
    await _open(client, env)
    env.fw.calls.clear()
    deleted = await client.delete(f"/api/policy-hierarchy/rules/{rule['id']}")
    assert deleted.status_code == 409 and deleted.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", deleted.text
    reorder = await client.post(f"/api/policy-hierarchy/groups/{group['id']}/reassert-ordering")
    assert reorder.status_code == 409 and reorder.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", reorder.text
    assert env.fw.writes() == []


@pytest.mark.asyncio
async def test_removing_a_former_hierarchy_target_waits_for_the_cleanup(client, env):
    from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
    from tests.test_policy_hierarchy import _create_group, _hrule_payload
    group = await _create_group(client, "Cleanup-Former")
    rule = (await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"], description="former"))).json()
    async with SessionLocal() as db:  # deployed on the host, which is not in the group (any more): "retained"
        db.add(PolicyHierarchyRuleDeployment(policy_hierarchy_id=uuid_lib.UUID(rule["id"]), host_id=env.host_id, status="success",
                                             opnsense_policy_hierarchy_uuid=R_TAIL))
        await db.commit()
    plan = (await client.get(f"/api/policy-hierarchy/rules/{rule['id']}/preview")).json()
    assert [h["can_remove"] for h in plan["hosts"]] == [True]
    await _open(client, env)
    env.fw.calls.clear()
    r = await client.post(f"/api/policy-hierarchy/rules/{rule['id']}/deployments/{env.host_id}/remove", json={"preview_token": plan["token"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", r.text
    assert env.fw.writes() == [] and env.fw.get(R_TAIL) is not None


@pytest.mark.asyncio
async def test_adopting_a_rule_waits_for_the_cleanup(client, env):
    await _open(client, env)
    r = await client.post("/api/rules/adopt", json={"host_id": str(env.host_id), "rule_uuid": R_BASE})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", r.text


@pytest.mark.asyncio
async def test_other_open_work_blocks_a_new_cleanup_over_the_api(client, env):
    url = _base(env) + "/preview"
    body = {"kind": "disable", "uuids": [R_UNUSED]}
    async with SessionLocal() as db:
        db.add(HostOperation(host_id=env.host_id, host_name="fw-a", actor="admin", action="reboot", status="submitted",
                             expires_at=_now(), fingerprint="x", snapshot={}))
        await db.commit()
    r = await client.post(url, json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OPEN_HOST_MAINTENANCE_BLOCKS_DEPLOY"
    async with SessionLocal() as db:
        await db.execute(update(HostOperation).values(status="done"))
        await db.commit()
    rule = await _central_rule(client, env)
    await _deployed_row(rule["id"], env.host_id, R_TAIL, status="pending")
    r = await client.post(url, json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BEREINIGUNG_OFFENE_OPERATION" and r.json()["detail"]["params"]["reason"] == "deployment"
    async with SessionLocal() as db:
        await db.execute(update(RuleDeployment).values(status="success"))
        await db.commit()
    assert (await client.post(url, json=body)).status_code == 200
    assert env.fw.writes() == []


# ── Notification ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_reverts_and_unclear_runs_are_reported_with_stable_keys(client, env):
    op = await _open(client, env)
    async with SessionLocal() as db:
        assert not [k for k in await notification_checks.check_revert_failed(db) if k.startswith("rule_cleanup")]
    # a run that ended half-way (decision needed): one problem for the run
    async with SessionLocal() as db:
        await db.execute(update(RuleCleanupOperation).where(RuleCleanupOperation.id == uuid_lib.UUID(op["id"])).values(status="partial"))
        await db.commit()
        problems = await notification_checks.check_revert_failed(db)
    key = f"rule_cleanup_op:{op['id']}"
    assert set(problems) == {key} and problems[key]["deployment_type"] == "rule_cleanup"
    assert problems[key]["host_name"] == "fw-a" and "teilweise" in problems[key]["hinweis"]
    # the automatic revert failed: one problem per rule that is still changed (the key is the item, not the run)
    env.fw.fail[("toggle", R_DUP)] = httpx.Response(200, json={"result": "failed"})
    await client.post(f"{_base(env)}/{op['id']}/revert")
    items = await _items(op["id"])
    assert (await _op(op["id"])).status == "revert_failed"
    async with SessionLocal() as db:
        first = await notification_checks.check_revert_failed(db)
        second = await notification_checks.check_revert_failed(db)
    stuck = f"rule_cleanup:{items[R_DUP].id}"
    assert set(first) == {stuck} and set(second) == set(first)  # stable: the same problem is not reported twice
    assert first[stuck]["entity_id"] == R_DUP and first[stuck]["host_name"] == "fw-a" and "Erneut zurücknehmen" in first[stuck]["hinweis"]
    assert first[stuck]["operation_status"] == "revert_failed" and first[stuck]["item_state"] == "revert_failed"
    # solved: gone
    del env.fw.fail[("toggle", R_DUP)]
    assert (await client.post(f"{_base(env)}/{op['id']}/retry")).json()["status"] == "reverted"
    async with SessionLocal() as db:
        assert not [k for k in await notification_checks.check_revert_failed(db) if k.startswith("rule_cleanup")]


@pytest.mark.asyncio
async def test_an_unclear_delete_is_reported_until_it_is_read_again(client, env):
    await _confirmed_disable(env.host_id, [R_UNUSED])
    pv = (await client.post(_base(env) + "/preview", json={"kind": "delete", "uuids": [R_UNUSED]})).json()
    env.fw.fail[("delete", R_UNUSED)] = httpx.ReadTimeout

    def _then_unreadable(kind, rule_uuid):  # the firewall stops answering once delRule went out
        if kind == "delete":
            env.fw.fail[("get", R_UNUSED)] = httpx.ReadTimeout
    env.fw.on_call = _then_unreadable
    op = (await client.post(f"{_base(env)}/{pv['preview_id']}/deploy", json={"confirm_name": "fw-a"})).json()
    assert op["status"] == "failed" and op["items"][0]["state"] == "unknown" and op["can_retry"] is True
    async with SessionLocal() as db:
        problems = await notification_checks.check_revert_failed(db)
    key = f"rule_cleanup:{op['items'][0]['id']}"
    assert set(problems) == {key} and problems[key]["operation"] == "delete" and "unklar" in problems[key]["hinweis"]
    env.fw.on_call = None
    del env.fw.fail[("get", R_UNUSED)]
    del env.fw.fail[("delete", R_UNUSED)]
    assert (await client.post(f"{_base(env)}/{op['id']}/retry")).status_code == 200
    async with SessionLocal() as db:
        assert not [k for k in await notification_checks.check_revert_failed(db) if k.startswith("rule_cleanup")]


# ── MCP and the schema ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_stays_read_only_for_the_cleanup(client, env, who):
    from app.routers.mcp import TOOLS
    names = {t["name"] for t in TOOLS}
    assert not [n for n in names if "cleanup" in n and n != "rule_cleanup_candidates"]
    assert all(t.get("path", "").startswith("/api/") for t in TOOLS if "path" in t)
    op = await _open(client, env)

    def call(path):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "api_get", "arguments": {"path": path}}}

    async with _bearer((await _make(client, "admin", name="mcp-admin"))["token"]) as api:
        for tail in ("/preview", f"/{op['id']}/confirm", f"/{op['id']}/revert", f"/{op['id']}/deploy", f"/{op['id']}/retry"):
            result = (await api.post("/api/mcp", json=call(f"/api/hosts/{env.host_id}/rule-cleanup{tail}"))).json()["result"]
            assert result["isError"] is True, tail  # a GET on a POST route: refused, nothing runs
        listing = (await api.post("/api/mcp", json=call(f"/api/hosts/{env.host_id}/rule-cleanup"))).json()["result"]
        assert listing["isError"] is False and op["id"] in listing["content"][0]["text"]
    assert (await _op(op["id"])).status == "awaiting_confirm"
    # a plain POST of a tool name that would write does not exist
    async with _bearer((await _make(client, "admin", name="mcp-admin2"))["token"]) as api:
        r = (await api.post("/api/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                              "params": {"name": "rule_cleanup_deploy", "arguments": {}}})).json()
        assert "error" in r or r["result"]["isError"] is True


@pytest.mark.asyncio
async def test_openapi_knows_the_routes_and_their_models(client):
    schema = (await client.get("/api/openapi.json")).json()
    base = "/api/hosts/{host_id}/rule-cleanup"
    paths = schema["paths"]
    assert set(paths[base]) == {"get"} and set(paths[base + "/preview"]) == {"post"} and set(paths[base + "/{op_id}"]) == {"get"}
    for tail in ("cancel", "deploy", "confirm", "revert", "retry"):
        assert set(paths[f"{base}/{{op_id}}/{tail}"]) == {"post"}, tail
    assert set(paths[base + "/items/{item_id}/restore"]) == {"post"}
    assert {"CleanupOperationRead", "CleanupItemRead", "CleanupPreviewRead", "PreviewRequest", "ConfirmName"} <= set(schema["components"]["schemas"])
    ops = schema["components"]["schemas"]["CleanupOperationRead"]["properties"]
    assert {"can_confirm", "can_revert", "can_retry", "plan", "warnings", "items", "revert_deadline", "message"} <= set(ops)
    assert {"restorable", "state", "host_name", "has_snapshot"} <= set(schema["components"]["schemas"]["CleanupItemRead"]["properties"])


def test_every_new_error_code_has_a_text_in_both_languages():
    import json
    import pathlib
    from app.errors import ErrorCode
    root = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "lib" / "i18n"
    if not root.is_dir():
        pytest.skip("the frontend is not part of this image (scripts/check_i18n.py checks the same from the repo root)")
    for lang in ("de", "en"):
        errors = json.loads((root / f"{lang}.json").read_text(encoding="utf-8"))["errors"]
        for code in ErrorCode:
            if code.name.startswith("BEREINIGUNG_") or code.name == "OPEN_CLEANUP_BLOCKS_DEPLOY":
                assert errors.get(code.value), (lang, code.value)


@pytest.mark.asyncio
async def test_a_refused_operator_attempt_is_audited_and_writes_nothing(client, env, who):
    op = await _open(client, env)
    r = await who["operator"].post(f"{_base(env)}/{op['id']}/retry")
    assert r.status_code == 403
    denied = await _audit(kind="host", action="retry", status="denied")
    assert [a.actor for a in denied] == ["cl-operator"] and denied[0].host_id == str(env.host_id)
    assert (await _op(op["id"])).status == "awaiting_confirm"


def _leaks(value) -> bool:
    """Anything that names a management source or a firewall's base URL."""
    import json as _json
    text = _json.dumps(value)
    return MGMT_SOURCE in text or "203.0.113.57" in text


@pytest.mark.asyncio
async def test_sources_and_firewall_addresses_are_for_admins_only(client, env, who):  # review finding 8
    mine = await client.post(_base(env) + "/preview", json={"kind": "disable", "uuids": [R_UNUSED]})
    assert mine.json()["plan"]["sources"] == [f"{MGMT_SOURCE}/32"] and mine.json()["plan"]["nodes"][0]["base_url"]
    theirs = await who["operator"].post(_base(env) + "/preview", json={"kind": "disable", "uuids": [R_DUP]})
    assert theirs.status_code == 200, theirs.text
    body = theirs.json()
    assert "sources" not in body["plan"] and body["plan"]["sources_configured"] is True and not _leaks(body)
    assert body["plan"]["nodes"][0]["items"][0]["protection"]["result"] == "false"
    for name in ("operator", "read_only", "tok_read", "tok_deploy", "tok_ro_read"):
        listed = await who[name].get(_base(env))
        assert listed.status_code == 200 and listed.json() and not _leaks(listed.json()), name
        one = await who[name].get(f"{_base(env)}/{mine.json()['preview_id']}")
        assert one.status_code == 200 and not _leaks(one.json()), name
        audit = await who[name].get("/api/audit", params={"kind": "rule_cleanup"})
        assert audit.status_code == 200 and audit.json()["items"] and not _leaks(audit.json()), name
        assert {i["details"]["admin_only"] for i in audit.json()["items"]} == {"redacted"}, name
    for name in ("admin", "tok_admin"):
        assert _leaks((await who[name].get(_base(env))).json()), name
        audit = (await who[name].get("/api/audit", params={"kind": "rule_cleanup"})).json()
        assert all(isinstance(i["details"]["admin_only"], dict) for i in audit["items"]), name


# Review finding 9: undeploying a central object and removing a former target write to the firewall too.
_UNDEPLOY_SITES = [
    ("app.services.alias_deploy_service", "undeploy_alias"), ("app.services.openvpn_deploy_service", "undeploy_instance"),
    ("app.services.wireguard_deploy_service", "undeploy_tunnel"), ("app.services.port_forward_deploy_service", "undeploy_port_forward"),
    ("app.services.outbound_nat_deploy_service", "undeploy_outbound_nat"), ("app.services.one_to_one_deploy_service", "undeploy_one_to_one"),
    ("app.services.ipsec_deploy_service", "undeploy_tunnel"),
] + [(f"app.routers.{r}", "remove_previous_target") for r in
     ("aliases", "port_forward", "one_to_one", "outbound_nat", "wireguard", "openvpn", "ipsec")]


@pytest.mark.parametrize("module,name", _UNDEPLOY_SITES)
def test_every_undeploy_and_target_removal_checks_for_an_open_cleanup_first(module, name):
    import importlib
    import inspect
    source = inspect.getsource(getattr(importlib.import_module(module), name))
    lock = min(i for i in (source.find("ensure_no_open_cleanup_on("), source.find("ensure_no_open_cleanup(")) if i >= 0)
    first_write = source.find("opnsense_client.")
    first_write = min(i for i in (first_write, source.find("client.del_"), source.find("_client.del")) if i >= 0)
    assert 0 <= lock < first_write, (module, name)


@pytest.mark.asyncio
async def test_alias_delete_and_target_removal_wait_for_the_cleanup(client, env):
    from app.models.alias_deployment import AliasDeployment
    deployed = await client.post("/api/aliases", json={"name": "cl_gone", "type": "host", "content": "203.0.113.5",
                                                      "target_host_ids": [str(env.host_id)]})
    former = await client.post("/api/aliases", json={"name": "cl_former", "type": "host", "content": "203.0.113.6",
                                                    "target_host_ids": []})
    assert deployed.status_code == 201 and former.status_code == 201, (deployed.text, former.text)
    async with SessionLocal() as db:
        for alias in (deployed.json(), former.json()):
            db.add(AliasDeployment(alias_id=uuid_lib.UUID(alias["id"]), host_id=env.host_id, status="success",
                                   opnsense_alias_uuid="aaaaaaaa-1111-4111-8111-111111111111", last_payload={"name": alias["name"]}))
        await db.commit()
    preview = (await client.get(f"/api/aliases/{former.json()['id']}/preview")).json()
    assert next(h for h in preview["hosts"] if h["host_id"] == str(env.host_id))["can_remove"], preview
    await _open(client, env)
    writes = list(env.fw.writes())
    r = await client.delete(f"/api/aliases/{deployed.json()['id']}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", r.text
    preview = (await client.get(f"/api/aliases/{former.json()['id']}/preview")).json()
    r = await client.post(f"/api/aliases/{former.json()['id']}/deployments/{env.host_id}/remove", json={"preview_token": preview["token"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OPEN_CLEANUP_BLOCKS_DEPLOY", r.text
    assert env.fw.writes() == writes
