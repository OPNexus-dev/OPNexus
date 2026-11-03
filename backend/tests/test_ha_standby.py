"""U3 (docs/DESIGN-HA-UI.md section 4): the standby's own door.

A replica cannot write, so the sign-in there must not (and a test can prove it by counting statements); everything else on the node is
closed; the takeover is decided here but done by the updater of the node."""
import time
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from app import leader
from app.config import settings
from app.database import SessionLocal, engine
from app.main import app
from app.models.user import AdminUser
from app.routers import ha_standby
from app.services import auth_service, standby_session, totp_service, updater_client

PASSWORD = "standby-test-password-1"


@pytest.fixture(autouse=True)
def _standby(monkeypatch):
    """This process is the replica node `b` of a pair."""
    monkeypatch.setattr(settings, "ha_node", "b")
    monkeypatch.setattr(settings, "ha_peer_ip", "192.168.2.20")
    monkeypatch.setattr(settings, "ha_vip", "192.168.2.22/24")
    monkeypatch.setattr(leader, "current_role", lambda: leader.REPLICA)
    standby_session.throttle._records.clear()
    standby_session._last_step.clear()
    yield
    standby_session.throttle._records.clear()


@pytest_asyncio.fixture
async def door():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


_created: list[str] = []


@pytest_asyncio.fixture(autouse=True)
async def _remove_my_users():
    """admin_users survives the per-test clean-up (the bootstrap admin lives there): take away what these tests added, or they leak into
    other test files (a user called "reader" made another file's test fail in the full run)."""
    yield
    if _created:
        from sqlalchemy import delete
        async with SessionLocal() as db:
            await db.execute(delete(AdminUser).where(AdminUser.username.in_(_created)))
            await db.commit()
        _created.clear()


async def _user(name, *, role="admin", totp=None, source="local", last_step=0):
    name = f"sb-{name}"
    _created.append(name)
    async with SessionLocal() as db:
        user = AdminUser(username=name, password_hash=auth_service.hash_password(PASSWORD), role=role, auth_source=source,
                         totp_enabled=totp is not None, totp_last_step=last_step)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        if totp:
            totp_service.save_secret(user.id, totp)
        return user


def _login(door, name, password=PASSWORD, code=None):
    body = {"username": name, "password": password}
    if code is not None:
        body["totp_code"] = code
    return door.post("/api/ha/standby/login", json=body)


class Statements:
    """Every SQL statement the application sends, to prove a request wrote nothing."""

    def __init__(self):
        self.sql: list[str] = []

    def __enter__(self):
        event.listen(engine.sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(engine.sync_engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, *a):
        self.sql.append(statement.strip().split(None, 1)[0].upper())

    @property
    def writes(self):
        return [s for s in self.sql if s in ("INSERT", "UPDATE", "DELETE", "TRUNCATE")]


# ── the rest of the API is closed ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_standby_closes_every_api_route_except_health_metrics_and_its_own_door(door):
    for method, path in (("GET", "/api/hosts"), ("POST", "/api/auth/login"), ("GET", "/api/auth/me"), ("GET", "/api/auth/oidc/config"),
                         ("GET", "/api/system/ha"), ("POST", "/api/groups"), ("DELETE", "/api/hosts/x"), ("GET", "/api/dashboard/summary")):
        resp = await door.request(method, path, json={} if method != "GET" else None)
        assert resp.status_code == 503, f"{method} {path}"
        assert resp.json()["detail"]["code"] == "NODE_STANDBY", path
    assert (await door.get("/api/health")).status_code == 200
    assert (await door.get("/api/ha/standby/session")).status_code == 401   # open, but it needs its own cookie


@pytest.mark.asyncio
async def test_the_health_of_a_standby_still_names_its_role_for_the_partner_guard(door):
    body = (await door.get("/api/health")).json()
    assert body["status"] == "ok" and "role" in body


@pytest.mark.asyncio
async def test_a_primary_or_a_lone_node_is_not_affected(door, monkeypatch):
    monkeypatch.setattr(leader, "current_role", lambda: leader.LEADER)
    assert (await door.get("/api/hosts")).status_code == 401           # the ordinary sign-in wall, not NODE_STANDBY
    monkeypatch.setattr(leader, "current_role", lambda: leader.REPLICA)
    monkeypatch.setattr(settings, "ha_node", "")
    assert (await door.get("/api/hosts")).status_code == 401           # a replica that is no HA node (a cold copy) keeps today's behaviour


@pytest.mark.asyncio
async def test_the_door_answers_not_standby_on_a_primary(door, monkeypatch):
    monkeypatch.setattr(leader, "current_role", lambda: leader.LEADER)
    for resp in (await _login(door, settings.admin_username, settings.admin_password), await door.get("/api/ha/standby/status")):
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "NOT_STANDBY"


# ── sign-in without writing ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_admin_signs_in_with_a_cookie_of_its_own_and_not_a_single_write(door):
    with Statements() as sql:
        resp = await _login(door, settings.admin_username, settings.admin_password)
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == settings.admin_username and resp.json()["expires_in"] == 900
    assert sql.sql and sql.writes == [], f"a replica cannot write: {sql.writes}"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{standby_session.cookie_name()}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert (await door.get("/api/ha/standby/session")).json() == {"username": settings.admin_username}


@pytest.mark.asyncio
async def test_no_session_row_and_no_login_stamp_are_created(door):
    from sqlalchemy import select
    from app.models.user import Session as SessionRow
    async with SessionLocal() as db:
        before = (await db.execute(select(AdminUser.last_login_at).where(AdminUser.username == settings.admin_username))).scalar()
    await _login(door, settings.admin_username, settings.admin_password)
    async with SessionLocal() as db:
        assert (await db.execute(select(SessionRow))).first() is None
        assert (await db.execute(select(AdminUser.last_login_at).where(AdminUser.username == settings.admin_username))).scalar() == before


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_user_look_the_same(door):
    a = await _login(door, settings.admin_username, "wrong-password")
    b = await _login(door, "nobody-here", "wrong-password")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() and a.json()["detail"]["code"] == "UNGUELTIGER_BENUTZERNAME_ODER_PASSWORT"


@pytest.mark.asyncio
async def test_repeated_failures_lock_the_account_out_with_its_own_budget(door):
    from app import runtime_settings
    for _ in range(runtime_settings.value("login_max_attempts")):
        assert (await _login(door, settings.admin_username, "wrong")).status_code == 401
    locked = await _login(door, settings.admin_username, settings.admin_password)
    assert locked.status_code == 429 and locked.json()["detail"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"
    # the normal sign-in's budget is a different one
    assert auth_service.throttle.retry_after("127.0.0.1", settings.admin_username) == 0


@pytest.mark.asyncio
async def test_only_local_admin_accounts_get_in_and_only_after_the_right_password(door):
    await _user("reader", role="read_only")
    await _user("sso-user", source="oidc")
    wrong = await _login(door, "sb-reader", "wrong")
    assert wrong.status_code == 401                                         # no hint that this account exists
    resp = await _login(door, "sb-reader")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "STANDBY_ADMIN_ONLY"
    resp = await _login(door, "sb-sso-user")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "STANDBY_ADMIN_ONLY"
    assert (await door.get("/api/ha/standby/session")).status_code == 401


# ── second factor ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_second_factor_is_required_checked_and_replay_protected(door):
    secret = totp_service.generate_secret()
    await _user("two-factor", totp=secret)
    resp = await _login(door, "sb-two-factor")
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "TOTP_REQUIRED"
    resp = await _login(door, "sb-two-factor", code="000000")
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "TOTP_INVALID"
    code = totp_service.code_at(secret)
    with Statements() as sql:
        assert (await _login(door, "sb-two-factor", code=code)).status_code == 200
    assert sql.writes == []
    again = await _login(door, "sb-two-factor", code=code)             # the same code a second time
    assert again.status_code == 401 and again.json()["detail"]["code"] == "TOTP_INVALID"


@pytest.mark.asyncio
async def test_a_step_already_used_on_the_primary_is_refused_here_too(door):
    secret = totp_service.generate_secret()
    step = int(time.time() // 30)
    await _user("used-on-primary", totp=secret, last_step=step + 1)
    resp = await _login(door, "sb-used-on-primary", code=totp_service.code_at(secret))
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "TOTP_INVALID"


@pytest.mark.asyncio
async def test_a_recovery_code_is_not_accepted_because_using_one_up_is_a_write(door):
    plain, hashes = totp_service.new_recovery_codes()
    user = await _user("with-recovery", totp=totp_service.generate_secret())
    async with SessionLocal() as db:
        row = await db.get(AdminUser, user.id)
        row.totp_recovery_hashes = hashes
        await db.commit()
    resp = await _login(door, "sb-with-recovery", code=plain[0])
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "TOTP_INVALID"


@pytest.mark.asyncio
async def test_an_account_with_two_factor_but_no_secret_file_yet_does_not_get_in_without_it(door):
    await _user("not-synced-yet", totp="JBSWY3DPEHPK3PXP")
    totp_service.delete_secrets((await _find("sb-not-synced-yet")).id)
    resp = await _login(door, "sb-not-synced-yet", code="123456")
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "STANDBY_LOGIN_UNAVAILABLE"
    assert (await _login(door, "sb-not-synced-yet")).status_code == 503       # not even "code required": it must not be bypassable


async def _find(name):
    from sqlalchemy import select
    async with SessionLocal() as db:
        return (await db.execute(select(AdminUser).where(AdminUser.username == name))).scalar_one()


# ── the cookie ───────────────────────────────────────────────────────────────────────────────────────────

def test_a_token_is_signed_expires_and_survives_nothing_forged():
    token = standby_session.issue("admin", now=1000)
    assert standby_session.verify(token, now=1000 + 899) == "admin"
    assert standby_session.verify(token, now=1000 + 900) is None
    body, sig = token.split(".")
    other = standby_session.issue("someone-else", now=1000).split(".")[0]
    for forged in (f"{other}.{sig}", f"{body}.AAAA", token + "x", body, "", "a.b.c", ".", f"{body}."):
        assert standby_session.verify(forged, now=1001) is None, forged
    assert standby_session.verify(None) is None


@pytest.mark.asyncio
async def test_a_cookie_from_another_process_or_key_does_not_work(door, monkeypatch):
    token = standby_session.issue("admin")
    monkeypatch.setattr(standby_session, "_KEY", b"another process" * 3)
    door.cookies.set(standby_session.cookie_name(), token)
    assert (await door.get("/api/ha/standby/session")).status_code == 401


@pytest.mark.asyncio
async def test_the_ordinary_session_cookie_opens_nothing_here(door):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.cookies.set("opnexus-session", "x")
        ac.cookies.set("__Host-opnexus-session", "x")
        assert (await ac.get("/api/ha/standby/status")).status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_the_cookie(door):
    await _login(door, settings.admin_username, settings.admin_password)
    resp = await door.post("/api/ha/standby/logout")
    assert resp.status_code == 200 and f"{standby_session.cookie_name()}=" in resp.headers["set-cookie"]
    door.cookies.clear()
    assert (await door.get("/api/ha/standby/session")).status_code == 401


# ── status and takeover ──────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def pair(monkeypatch):
    """The environment around the door: the node's updater (agent) and the partner's proxy."""
    state = {"agent": {"peer_reachable": True, "job": None, "log": []}, "health": ("leader", 4), "promoted": []}

    async def local_status():
        if state["agent"] is None:
            raise updater_client.UpdaterUnavailable("not_enabled")
        return state["agent"]

    async def local_promote(force, actor):
        state["promoted"].append((force, actor))
        return {"job": {"id": "j1", "state": "running", "verb": "promote"}}

    async def missing():
        return state.get("missing", {"hosts": [], "totp": []})

    async def health():
        return state["health"]

    monkeypatch.setattr(updater_client, "ha_local_status", local_status)
    monkeypatch.setattr(updater_client, "ha_local_promote", local_promote)
    monkeypatch.setattr(ha_standby.ha_check, "missing", missing)
    monkeypatch.setattr(ha_standby, "_partner_health", health)
    return state


async def _id_of(username):
    from sqlalchemy import select
    async with SessionLocal() as db:
        return (await db.execute(select(AdminUser.id).where(AdminUser.username == username))).scalar_one()


async def _signed_in(door):
    assert (await _login(door, settings.admin_username, settings.admin_password)).status_code == 200


@pytest.mark.asyncio
async def test_the_status_tells_what_a_person_needs_to_decide(door, pair):
    await _signed_in(door)
    body = (await door.get("/api/ha/standby/status")).json()
    assert body["node"] == "b" and body["peer_ip"] == "192.168.2.20" and body["vip"] == "192.168.2.22"
    assert body["username"] == settings.admin_username
    assert body["secrets"] == {"complete": True, "hosts": [], "totp": []}
    assert body["partner"] == {"agent": True, "health": {"role": "leader", "epoch": 4}}
    assert body["promote"]["mode"] == "normal" and body["promote"]["force_sentence"] == "alter Primaer ist aus"
    for key in ("epoch", "replication", "last_replay_age_seconds", "sync_age_seconds", "primary_node"):
        assert key in body


@pytest.mark.asyncio
async def test_missing_secret_files_are_listed_by_name(door, pair):
    pair["missing"] = {"hosts": ["fw-3"], "totp": ["julian"]}
    await _signed_in(door)
    assert (await door.get("/api/ha/standby/status")).json()["secrets"] == {"complete": False, "hosts": ["fw-3"], "totp": ["julian"]}


def test_the_takeover_mode_follows_what_is_known_about_the_partner():
    d = ha_standby.decide_promote
    assert d(True, ("leader", 3)) == "normal"          # the agent answers: it can be stopped properly
    assert d(True, None) == "normal"
    assert d(False, None) == "force"                   # nothing answers: down or cut off, confirm in writing
    assert d(False, ("fenced", 3)) == "force"
    assert d(False, ("replica", 3)) == "force"
    assert d(False, ("leader", 3)) == "refused"        # alive: two primaries would result
    assert d(None, None) == "unavailable" and d(None, ("leader", 1)) == "unavailable"


@pytest.mark.asyncio
async def test_a_normal_takeover_needs_a_signed_in_admin_and_names_the_actor(door, pair):
    resp = await door.post("/api/ha/standby/promote", json={})
    assert resp.status_code == 401 and pair["promoted"] == []
    await _signed_in(door)
    resp = await door.post("/api/ha/standby/promote", json={})
    assert resp.status_code == 202 and resp.json()["job"]["id"] == "j1"
    assert pair["promoted"] == [(False, str(await _id_of(settings.admin_username)))]   # the account's id, not its free-form name


@pytest.mark.asyncio
async def test_a_client_cannot_force_a_takeover_while_the_partner_can_be_stopped(door, pair):
    await _signed_in(door)
    await door.post("/api/ha/standby/promote", json={"force": True, "confirm": "alter Primaer ist aus"})
    assert pair["promoted"] == [(False, str(await _id_of(settings.admin_username)))]       # the server decides the mode, not the request


@pytest.mark.asyncio
async def test_forcing_needs_the_sentence_typed_exactly(door, pair):
    pair["agent"] = {"peer_reachable": False}
    pair["health"] = None
    await _signed_in(door)
    for body in ({}, {"force": True}, {"force": True, "confirm": "ja"}, {"force": False, "confirm": "alter Primaer ist aus"},
                 {"force": True, "confirm": "Alter Primaer ist aus"}):
        resp = await door.post("/api/ha/standby/promote", json=body)
        assert resp.status_code == 400 and resp.json()["detail"]["code"] == "STANDBY_CONFIRMATION_REQUIRED", body
    assert pair["promoted"] == []
    resp = await door.post("/api/ha/standby/promote", json={"force": True, "confirm": "  alter Primaer ist aus "})
    assert resp.status_code == 202 and pair["promoted"] == [(True, str(await _id_of(settings.admin_username)))]


@pytest.mark.asyncio
async def test_a_partner_that_still_answers_as_leader_cannot_be_overridden(door, pair):
    pair["agent"] = {"peer_reachable": False}
    pair["health"] = ("leader", 7)                       # only the agent path is broken, the partner lives
    await _signed_in(door)
    resp = await door.post("/api/ha/standby/promote", json={"force": True, "confirm": "alter Primaer ist aus"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "PEER_IS_LEADER"
    assert pair["promoted"] == []


@pytest.mark.asyncio
async def test_without_the_updater_there_is_no_takeover(door, pair):
    pair["agent"] = None
    await _signed_in(door)
    resp = await door.post("/api/ha/standby/promote", json={})
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "UPDATER_UNAVAILABLE"
    status = (await door.get("/api/ha/standby/status")).json()
    assert status["promote"]["mode"] == "unavailable" and status["partner"]["agent"] is None


@pytest.mark.asyncio
async def test_a_busy_or_rejecting_updater_is_reported(door, pair, monkeypatch):
    async def rejects(force, actor):
        raise updater_client.UpdaterRejected("busy")

    monkeypatch.setattr(updater_client, "ha_local_promote", rejects)
    await _signed_in(door)
    resp = await door.post("/api/ha/standby/promote", json={})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "UPDATER_BUSY"


@pytest.mark.asyncio
async def test_the_job_endpoint_follows_the_takeover(door, pair):
    pair["agent"] = {"peer_reachable": True, "job": {"state": "running", "verb": "promote"}, "log": ["==> Fencing am Partner …"]}
    assert (await door.get("/api/ha/standby/promote/job")).status_code == 401
    await _signed_in(door)
    body = (await door.get("/api/ha/standby/promote/job")).json()
    assert body["job"]["state"] == "running" and body["log"] == ["==> Fencing am Partner …"]


@pytest.mark.asyncio
async def test_status_and_promote_write_nothing_either(door, pair):
    await _signed_in(door)
    with Statements() as sql:
        await door.get("/api/ha/standby/status")
        await door.post("/api/ha/standby/promote", json={})
        await door.get("/api/ha/standby/promote/job")
    assert sql.writes == []


@pytest.mark.asyncio
async def test_an_account_with_spaces_and_umlauts_can_take_over(door, pair):
    """The user name is free-form (up to 150 characters); the updater only ever sees the account's id (lab review 2026-11-03: a name like
    "Julian Boldt" was refused by the updater's pattern, so the emergency button failed for exactly that administrator)."""
    user = await _user("Jürgen Müller")
    assert (await _login(door, user.username)).status_code == 200
    resp = await door.post("/api/ha/standby/promote", json={})
    assert resp.status_code == 202
    assert pair["promoted"] == [(False, str(user.id))]
    assert " " not in pair["promoted"][0][1]


@pytest.mark.asyncio
async def test_an_account_deleted_since_the_sign_in_cannot_take_over(door, pair):
    from sqlalchemy import delete
    user = await _user("short-lived")
    assert (await _login(door, user.username)).status_code == 200
    async with SessionLocal() as db:
        await db.execute(delete(AdminUser).where(AdminUser.id == user.id))
        await db.commit()
    resp = await door.post("/api/ha/standby/promote", json={})
    assert resp.status_code == 401 and pair["promoted"] == []


@pytest.mark.asyncio
async def test_a_pair_that_still_runs_over_ssh_cannot_be_fenced_from_here(door, pair):
    """No agent pairing: the updater has no SSH keys, so it cannot stop the partner. Reporting "partner agent not reachable" would offer the
    forced takeover (no fencing) while the partner may well be stoppable over SSH from a terminal."""
    pair["agent"] = {"paired": False, "peer_reachable": None, "job": None, "log": []}
    pair["health"] = None
    await _signed_in(door)
    status = (await door.get("/api/ha/standby/status")).json()
    assert status["promote"]["mode"] == "unavailable" and status["partner"]["agent"] is None
    for body in ({}, {"force": True, "confirm": "alter Primaer ist aus"}):
        resp = await door.post("/api/ha/standby/promote", json=body)
        assert resp.status_code == 503 and resp.json()["detail"]["code"] == "UPDATER_UNAVAILABLE", body
    assert pair["promoted"] == []


@pytest.mark.asyncio
async def test_the_audit_names_the_account_by_its_id(monkeypatch):
    from sqlalchemy import select
    from app import ha_audit
    from app.models.audit import AuditEvent
    user = await _user("Zoë Beispiel")
    event_id = await ha_audit.record("promote", str(user.id), "normal")
    async with SessionLocal() as db:
        row = (await db.execute(select(AuditEvent).where(AuditEvent.id == event_id))).scalar_one()
        assert row.actor == user.username and row.action == "promote"   # (the audit trail is append-only: the rows stay, the suite's clean-up removes them)
    for bad in ("julian", "Julian Boldt", "x;id", "", str(user.id).upper(), str(user.id) + "0"):
        with pytest.raises(ValueError):
            await ha_audit.record("promote", bad, "normal")
    unknown = str(uuid.uuid4())                       # an account deleted meanwhile: still audited, by its id
    event_id = await ha_audit.record("promote", unknown, "force")
    async with SessionLocal() as db:
        row = (await db.execute(select(AuditEvent).where(AuditEvent.id == event_id))).scalar_one()
        assert unknown in row.actor
