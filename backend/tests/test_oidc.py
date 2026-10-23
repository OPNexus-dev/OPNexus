"""OIDC single sign-on against a fake identity provider (respx): the happy path and every way a forged or
confused login could succeed."""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select, update

from app.database import SessionLocal
from app.models.oidc import OidcLoginState
from app.models.user import AdminUser
from app.services import notification_secret_store, oidc_service
from tests.test_api_tokens import _login

ISSUER = "https://idp.test/realm"
CLIENT = "opnexus-client"


def _keypair(kid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, use="sig", alg="RS256")
    return pem, jwk


KEY, JWK = _keypair("k1")
OTHER_KEY, _ = _keypair("k1")  # same kid, different key: a forger


@pytest.fixture(autouse=True)
async def _fresh():
    oidc_service.clear_caches()
    notification_secret_store.delete_secret(oidc_service.SECRET_NAME)
    async with SessionLocal() as db:
        await db.execute(OidcLoginState.__table__.delete())
        await db.execute(AdminUser.__table__.delete().where(AdminUser.auth_source == "oidc"))
        await db.commit()
    yield


async def _configure(client, **extra):
    body = {"enabled": True, "display_name": "Authentik", "issuer": ISSUER, "client_id": CLIENT, "client_secret": "s3cret",
            "admin_group": "opnexus-admins", "operator_group": "opnexus-ops", "read_only_group": "opnexus-viewers", **extra}
    resp = await client.put("/api/settings/oidc", json=body)
    assert resp.status_code == 200, resp.text


def _mock_idp(respx_mock, *, userinfo=None):
    respx_mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(return_value=httpx.Response(200, json={
        "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth", "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks", **({"userinfo_endpoint": f"{ISSUER}/userinfo"} if userinfo else {}),
        "token_endpoint_auth_methods_supported": ["client_secret_basic"]}))
    respx_mock.get(f"{ISSUER}/jwks").mock(return_value=httpx.Response(200, json={"keys": [JWK]}))
    if userinfo is not None:
        respx_mock.get(f"{ISSUER}/userinfo").mock(return_value=httpx.Response(200, json=userinfo))


def _claims(sent_nonce, **over):
    now = int(time.time())
    base = {"iss": ISSUER, "aud": CLIENT, "sub": "user-1", "exp": now + 300, "iat": now, "nonce": sent_nonce,
            "preferred_username": "alice", "groups": ["opnexus-admins"]}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def _forged_hs256(claims, secret: str, kid="k1"):
    """What an attacker sends: PyJWT >= 2.12 itself refuses to sign with a JWK-looking secret, so build the token by hand."""
    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")
    signing_input = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode()) + b"." + b64(json.dumps(claims).encode())
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + b64(signature)).decode()


def _id_token(claims, key=KEY, alg="RS256", kid="k1"):
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


async def _start(anon, respx_mock, **idp):
    _mock_idp(respx_mock, **idp)
    resp = await anon.get("/api/auth/oidc/start", params={"origin": "http://test"})
    assert resp.status_code == 302, resp.text
    location = urlsplit(resp.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/auth"
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def _callback(anon, respx_mock, query, *, claims=None, token_response=None, **token_kw):
    """Drive the callback: the fake token endpoint answers with an ID token for the nonce of this attempt."""
    seen = {}

    def token(request):
        seen["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        seen["auth"] = request.headers.get("authorization")
        if token_response is not None:
            return httpx.Response(200, json=token_response)
        return httpx.Response(200, json={"id_token": _id_token(claims if claims is not None else _claims(query["nonce"]), **token_kw), "access_token": "at"})

    respx_mock.post(f"{ISSUER}/token").mock(side_effect=token)
    resp = await anon.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
    return resp, seen


def _error(resp):
    assert resp.status_code == 302, resp.text
    loc = resp.headers["location"]
    assert loc.startswith("/login?sso_error="), loc
    return loc.split("=", 1)[1]


async def _users(prefix):
    async with SessionLocal() as db:
        return {u.username: u for u in (await db.scalars(select(AdminUser).where(AdminUser.auth_source == "oidc"))).all()}


@pytest.mark.asyncio
async def test_login_creates_the_account_with_the_role_of_the_group_and_a_working_session(client, anon_client, respx_mock):
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    assert query["code_challenge_method"] == "S256" and query["redirect_uri"] == "http://test/api/auth/oidc/callback"
    assert query["client_id"] == CLIENT and "openid" in query["scope"]
    resp, seen = await _callback(anon_client, respx_mock, query)
    assert resp.status_code == 302 and resp.headers["location"] == "/dashboard"
    # The provider got the PKCE verifier matching the challenge, the redirect URI and the client secret (HTTP basic).
    digest = base64.urlsafe_b64encode(hashlib.sha256(seen["form"]["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert digest == query["code_challenge"] and seen["form"]["redirect_uri"] == query["redirect_uri"]
    assert seen["auth"] == "Basic " + base64.b64encode(f"{CLIENT}:s3cret".encode()).decode()
    me = (await anon_client.get("/api/auth/me")).json()
    assert me == {"authenticated": True, "username": "alice", "role": "admin"}
    user = (await _users("alice"))["alice"]
    assert user.auth_source == "oidc" and user.oidc_subject == f"{ISSUER}|user-1" and user.last_login_at is not None


@pytest.mark.asyncio
async def test_role_follows_the_groups_on_every_login_and_the_most_privileged_wins(client, anon_client, respx_mock):
    await _configure(client)
    for groups, expected in ((["opnexus-viewers"], "read_only"), (["opnexus-viewers", "opnexus-ops"], "operator"),
                             (["OPNEXUS-ADMINS", "x"], "admin"), (["opnexus-ops"], "operator")):
        query = await _start(anon_client, respx_mock)
        resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], groups=groups))
        assert resp.headers["location"] == "/dashboard"
        assert (await _users("alice"))["alice"].role == expected, groups
    assert len(await _users("alice")) == 1  # same identity, same account


@pytest.mark.asyncio
async def test_without_a_mapped_group_nobody_gets_in_and_no_account_is_created(client, anon_client, respx_mock):
    await _configure(client)
    for groups in ([], ["random"], None):
        query = await _start(anon_client, respx_mock)
        resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], groups=groups))
        assert _error(resp) == "no_role"
    assert await _users("alice") == {}
    assert (await anon_client.get("/api/auth/me")).json() == {"authenticated": False}


@pytest.mark.asyncio
async def test_groups_can_come_from_userinfo_but_only_for_the_same_subject(client, anon_client, respx_mock):
    await _configure(client)
    query = await _start(anon_client, respx_mock, userinfo={"sub": "user-1", "groups": ["opnexus-ops"]})
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], groups=None))
    assert resp.headers["location"] == "/dashboard" and (await _users("alice"))["alice"].role == "operator"
    respx_mock.clear()
    query = await _start(anon_client, respx_mock, userinfo={"sub": "someone-else", "groups": ["opnexus-admins"]})
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], groups=None, sub="user-2"))
    assert _error(resp) == "no_role"


@pytest.mark.asyncio
async def test_the_username_comes_from_userinfo_if_the_id_token_has_none_and_a_failing_userinfo_does_not_block_the_login(client, anon_client, respx_mock):
    await _configure(client)
    # Authelia keeps preferred_username/email out of the ID token; without userinfo the account would be named after the opaque sub.
    query = await _start(anon_client, respx_mock, userinfo={"sub": "user-1", "preferred_username": "alice", "email": "a@example.test"})
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], preferred_username=None))
    assert resp.headers["location"] == "/dashboard" and list(await _users("alice")) == ["alice"]
    respx_mock.clear()
    query = await _start(anon_client, respx_mock, userinfo={"sub": "someone-else", "preferred_username": "mallory"})
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], preferred_username=None, sub="user-2"))
    assert resp.headers["location"] == "/dashboard" and "mallory" not in await _users("x") and "user-2" in await _users("x")


@pytest.mark.asyncio
async def test_group_claim_may_be_a_string(client, anon_client, respx_mock):
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], groups="foo, opnexus-viewers"))
    assert resp.headers["location"] == "/dashboard" and (await _users("alice"))["alice"].role == "read_only"


@pytest.mark.parametrize("label,over", [
    ("wrong audience", {"aud": "someone-elses-client"}),
    ("wrong issuer", {"iss": "https://evil.test/realm"}),
    ("expired", {"exp": int(time.time()) - 3600}),
    ("no expiry", {"exp": None}),
    ("wrong nonce", {"nonce": "not-the-one-we-sent"}),
    ("no nonce", {"nonce": None}),
    ("multi audience, wrong azp", {"aud": [CLIENT, "other"], "azp": "other"}),
])
@pytest.mark.asyncio
async def test_forged_or_confused_id_tokens_are_rejected(client, anon_client, respx_mock, label, over):
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], **over))
    assert _error(resp) == "token", label
    assert await _users("alice") == {}


@pytest.mark.asyncio
async def test_signature_attacks_are_rejected(client, anon_client, respx_mock):
    await _configure(client)
    public_pem = serialization.load_pem_public_key  # noqa: F841 (documenting the confusion attack below)
    cases = {
        "signed with another key": dict(key=OTHER_KEY),
        "unknown key id": dict(kid="nope"),
        "alg none": None,
        "hmac with the public key as secret": "hs",
    }
    for label, spec in cases.items():
        query = await _start(anon_client, respx_mock)
        claims = _claims(query["nonce"])
        if spec is None:
            token = jwt.encode(claims, None, algorithm="none", headers={"kid": "k1"})
        elif spec == "hs":
            token = _forged_hs256(claims, json.dumps(JWK))
        else:
            token = _id_token(claims, **spec)
        resp, _ = await _callback(anon_client, respx_mock, query, token_response={"id_token": token})
        assert _error(resp) == "token", label
        oidc_service.clear_caches()
        respx_mock.clear()
    assert await _users("alice") == {}


@pytest.mark.asyncio
async def test_a_state_works_exactly_once_and_unknown_or_expired_ones_fail(client, anon_client, respx_mock):
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    first, _ = await _callback(anon_client, respx_mock, query)
    assert first.headers["location"] == "/dashboard"
    replay = await anon_client.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
    assert _error(replay) == "state"
    forged = await anon_client.get("/api/auth/oidc/callback", params={"code": "abc", "state": "made-up"})
    assert _error(forged) == "state"
    query = await _start(anon_client, respx_mock)
    async with SessionLocal() as db:
        from datetime import datetime, timedelta, timezone
        await db.execute(update(OidcLoginState).values(created_at=datetime.now(timezone.utc) - timedelta(minutes=30)))
        await db.commit()
    expired, _ = await _callback(anon_client, respx_mock, query)
    assert _error(expired) == "state"


@pytest.mark.asyncio
async def test_provider_errors_and_missing_parameters(client, anon_client, respx_mock):
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    denied = await anon_client.get("/api/auth/oidc/callback", params={"error": "access_denied", "state": query["state"]})
    assert _error(denied) == "provider"
    assert _error(await anon_client.get("/api/auth/oidc/callback")) == "provider"
    query = await _start(anon_client, respx_mock)
    respx_mock.post(f"{ISSUER}/token").mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    bad = await anon_client.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
    assert _error(bad) == "token"


@pytest.mark.asyncio
async def test_an_sso_identity_never_takes_over_a_local_account(client, anon_client, respx_mock):
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], preferred_username="admin", sub="evil-1"))
    assert resp.headers["location"] == "/dashboard"
    users = await _users("admin")
    assert "admin" not in users and any(n.startswith("admin~sso") for n in users)
    async with SessionLocal() as db:
        local = (await db.scalars(select(AdminUser).where(AdminUser.username == "admin"))).one()
    assert local.auth_source == "local" and local.oidc_subject is None and local.role == "admin"
    # A second identity with the same preferred name gets yet another account.
    query = await _start(anon_client, respx_mock)
    await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], preferred_username="admin", sub="evil-2"))
    assert len([n for n in await _users("admin") if n.startswith("admin~sso")]) == 2


@pytest.mark.asyncio
async def test_sso_accounts_have_no_usable_password_and_cannot_be_managed_locally(client, anon_client, respx_mock):
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    await _callback(anon_client, respx_mock, query)
    for password in ("", "!oidc", "alice", "!"):
        resp = await anon_client.post("/api/auth/login", json={"username": "alice", "password": password or "x"})
        assert resp.status_code == 401
    listing = {u["username"]: u for u in (await client.get("/api/users")).json()}
    uid = listing["alice"]["id"]
    assert listing["alice"]["auth_source"] == "oidc"
    for path, body, method in ((f"/api/users/{uid}/role", {"role": "read_only"}, "PATCH"),
                               (f"/api/users/{uid}/reset-password", {"new_password": "a-new-password-1"}, "POST")):
        resp = await client.request(method, path, json=body)
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "OIDC_MANAGED_USER"
    assert (await client.delete(f"/api/users/{uid}")).status_code == 204  # deleting is fine; they can sign in again


@pytest.mark.asyncio
async def test_start_refuses_foreign_origins_and_disabled_sso(client, anon_client, respx_mock):
    assert _error(await anon_client.get("/api/auth/oidc/start", params={"origin": "http://test"})) == "disabled"
    await _configure(client)
    _mock_idp(respx_mock)
    for origin in ("http://evil.test", "http://test/path", "http://user@test", "javascript:alert(1)", "http://test?x=1"):
        assert _error(await anon_client.get("/api/auth/oidc/start", params={"origin": origin})) == "disabled", origin
    await client.put("/api/settings/oidc", json={"enabled": False})
    assert _error(await anon_client.get("/api/auth/oidc/start", params={"origin": "http://test"})) == "disabled"


@pytest.mark.asyncio
async def test_public_config_reveals_only_the_button_text(client, anon_client):
    assert (await anon_client.get("/api/auth/oidc/config")).json() == {"enabled": False, "name": None}
    await _configure(client)
    assert (await anon_client.get("/api/auth/oidc/config")).json() == {"enabled": True, "name": "Authentik"}
    await client.put("/api/settings/oidc", json={"admin_group": "", "operator_group": "", "read_only_group": ""})
    assert (await anon_client.get("/api/auth/oidc/config")).json()["enabled"] is False  # no group mapping = nobody could enter


@pytest.mark.asyncio
async def test_settings_are_admin_only_hide_the_secret_and_validate(client):
    await _configure(client)
    got = (await client.get("/api/settings/oidc")).json()
    assert got["client_secret_set"] is True and "s3cret" not in json.dumps(got)
    assert notification_secret_store.read_secret(oidc_service.SECRET_NAME) == "s3cret"
    assert (await client.put("/api/settings/oidc", json={"issuer": "http://idp.test"})).status_code == 422
    assert (await client.put("/api/settings/oidc", json={"clear_client_secret": True})).json()["client_secret_set"] is False
    for role in ("operator", "read_only"):
        name = f"sso-{role}"
        assert (await client.post("/api/users", json={"username": name, "password": "sso-password-123", "role": role})).status_code == 201
        user = await _login(name, "sso-password-123")
        try:
            assert (await user.get("/api/settings/oidc")).status_code == 403
            assert (await user.put("/api/settings/oidc", json={"enabled": False})).status_code == 403
            assert (await user.post("/api/settings/oidc/test")).status_code == 403
        finally:
            await user.aclose()


@pytest.mark.asyncio
async def test_connection_test(client, respx_mock):
    assert (await client.post("/api/settings/oidc/test")).status_code == 422
    await _configure(client)
    _mock_idp(respx_mock)
    assert (await client.post("/api/settings/oidc/test")).json()["authorization_endpoint"] == f"{ISSUER}/auth"
    respx_mock.clear()
    respx_mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(return_value=httpx.Response(200, json={"issuer": "https://other.test"}))
    resp = await client.post("/api/settings/oidc/test")
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "OIDC_TEST_FAILED"


@pytest.mark.asyncio
async def test_failed_callbacks_are_throttled(client, anon_client, respx_mock):
    await _configure(client)
    results = [_error(await anon_client.get("/api/auth/oidc/callback", params={"code": "x", "state": f"s{i}"})) for i in range(12)]
    assert "throttled" in results


@pytest.mark.asyncio
async def test_a_string_group_claim_keeps_names_with_spaces_together(client, anon_client, respx_mock):
    await _configure(client, admin_group="Network Admins", operator_group="Admins", read_only_group="")
    query = await _start(anon_client, respx_mock)
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], groups="Network Admins"))
    assert resp.headers["location"] == "/dashboard" and (await _users("alice"))["alice"].role == "admin"  # not "Admins" = operator, not split
    query = await _start(anon_client, respx_mock)
    resp, _ = await _callback(anon_client, respx_mock, query, claims=_claims(query["nonce"], groups="Network, Team"))
    assert _error(resp) == "no_role"


@pytest.mark.asyncio
async def test_the_callback_only_works_in_the_browser_that_started_the_login(client, anon_client, respx_mock):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    # Somebody else (another browser, no state cookie) opens the callback URL: refused, and the state is not burned for the owner.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as stranger:
        respx_mock.post(f"{ISSUER}/token").mock(return_value=httpx.Response(200, json={"id_token": _id_token(_claims(query["nonce"]))}))
        resp = await stranger.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
        assert _error(resp) == "state"
    assert await _users("alice") == {}


@pytest.mark.asyncio
async def test_sso_sessions_are_short_and_the_state_cookie_is_httponly(client, anon_client, respx_mock):
    from datetime import datetime, timedelta, timezone
    from app.models.user import Session
    await _configure(client)
    _mock_idp(respx_mock)
    start = await anon_client.get("/api/auth/oidc/start", params={"origin": "http://test"})
    cookie = start.headers["set-cookie"].lower()
    assert cookie.startswith("opnexus-oidc-state=") and "httponly" in cookie and "samesite=lax" in cookie and "path=/api/auth/oidc" in cookie
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    resp, _ = await _callback(anon_client, respx_mock, query)
    assert resp.headers["location"] == "/dashboard"
    assert "max-age=43200" in resp.headers["set-cookie"].lower()
    async with SessionLocal() as db:
        user = (await _users("alice"))["alice"]
        session = (await db.scalars(select(Session).where(Session.user_id == user.id))).one()
    assert session.expires_at - datetime.now(timezone.utc) < timedelta(hours=12, minutes=1)


@pytest.mark.asyncio
async def test_error_text_from_the_provider_cannot_forge_log_lines(client, anon_client, caplog):
    await _configure(client)
    import logging
    with caplog.at_level(logging.WARNING):
        await anon_client.get("/api/auth/oidc/callback", params={"error": "x\r\nFAKE LOG LINE\x1b[31m", "state": "s"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "FAKE LOG LINE" in text and "\r" not in text and "\x1b" not in text and "\nFAKE" not in text


# ── S2 (1.29): state cookie on HTTPS ──────────────────────────────────────

@pytest.mark.asyncio
async def test_state_cookie_is_host_prefixed_on_https_and_only_that_name_counts(client, respx_mock, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.config import settings
    from app.main import app
    await _configure(client)
    monkeypatch.setattr(settings, "cookie_secure", True)
    _mock_idp(respx_mock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as browser:
        start = await browser.get("/api/auth/oidc/start", params={"origin": "https://test"})
        assert start.status_code == 302, start.text
        raw = start.headers["set-cookie"]
        low = raw.lower()
        assert raw.startswith("__Host-opnexus-oidc-state=") and "secure" in low and "httponly" in low
        assert "path=/;" in low + ";" and "domain" not in low and "samesite=lax" in low
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        state = browser.cookies.get("__Host-opnexus-oidc-state")
        assert state == query["state"]
        respx_mock.post(f"{ISSUER}/token").mock(return_value=httpx.Response(200, json={
            "id_token": _id_token(_claims(query["nonce"])), "access_token": "at"}))
        # A plain-name cookie (planted by a sibling page) with the right state value is not accepted ...
        browser.cookies.delete("__Host-opnexus-oidc-state")
        browser.cookies.set("opnexus-oidc-state", query["state"])
        resp = await browser.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
        assert _error(resp) == "state"
    assert await _users("alice") == {}


@pytest.mark.asyncio
async def test_callback_without_a_valid_state_cookie_always_fails(client, anon_client, respx_mock):
    await _configure(client)
    query = await _start(anon_client, respx_mock)
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    respx_mock.post(f"{ISSUER}/token").mock(return_value=httpx.Response(200, json={
        "id_token": _id_token(_claims(query["nonce"])), "access_token": "at"}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as stranger:
        for cookie in (None, "wrong", query["state"] + "x"):  # (more would trip the failed-callback throttle)
            if cookie is not None:
                stranger.cookies.set("opnexus-oidc-state", cookie, domain="test", path="/api/auth/oidc")
            resp = await stranger.get("/api/auth/oidc/callback", params={"code": "abc", "state": query["state"]})
            assert _error(resp) == "state", cookie
            assert "set-cookie" not in resp.headers or "opnexus-session" not in resp.headers["set-cookie"]
    assert await _users("alice") == {}
    # The attempt itself was not burned by the strangers: the owner can still finish it.
    resp, _ = await _callback(anon_client, respx_mock, query)
    assert resp.headers["location"] == "/dashboard"


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,blocked", [
    ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}, False),  # the IdP's redirect
    ({"Sec-Fetch-Site": "cross-site"}, False),
    ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}, True),
    ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}, True),
    ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"}, True),
])
async def test_sec_fetch_exemption_of_the_callback_is_only_for_top_level_navigation(client, headers, blocked):
    # `client` carries a session cookie, so the Sec-Fetch check applies; the callback itself answers 302 (login error).
    resp = await client.get("/api/auth/oidc/callback", params={"code": "a", "state": "b"}, headers=headers)
    assert (resp.status_code == 403) is blocked, resp.text
    other = await client.get("/api/hosts", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
    assert other.status_code == 403  # no other endpoint is exempt
