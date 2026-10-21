"""GUI gateway (1.27): every counter-measure of the design's threat table against a mock firewall,
with the real backend app behind the internal API."""
import datetime
import logging
import subprocess
import sys

import httpx
import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse

from app import gui_gateway as gw
from app.config import settings
from app.database import SessionLocal
from app.main import app as backend_app
from app.models.gui_access import GuiSession
from app.routers.auth import SECURE_COOKIE_NAME
from tests.test_gui_access import GW_TOKEN, SITES, _enable, _host, _https_login, gui_env  # noqa: F401

PASSWORD = "S3cret-Testpasswort!"
OWN = "https://opnexus.example:9441"


class MockFirewall:
    """Records what reaches the "firewall" and answers like OPNsense's authgui.inc."""

    def __init__(self):
        self.requests: list[dict] = []

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        body = await request.body()
        self.requests.append({"method": request.method, "path": request.url.path, "query": request.url.query,
                              "headers": dict(request.headers), "body": body})
        path = request.url.path
        if path == "/" and request.method == "POST":
            from urllib.parse import parse_qs
            if parse_qs(body.decode()).get("passwordfld") == ["right"]:
                response = Response(status_code=302, headers={"Location": "/ui/core/dashboard"})
            else:
                response = HTMLResponse("<form>Wrong username or password.</form>")
        elif path == "/redir":
            response = Response(status_code=302, headers={"Location": request.query_params["to"]})
        elif path == "/logout":
            response = Response(status_code=302, headers={"Location": "/"})
            response.headers.append("set-cookie", "PHPSESSID=deleted; expires=Thu, 01 Jan 1970 00:00:01 GMT; path=/")
        elif path == "/big":
            async def chunks():
                for i in range(50):
                    yield (b"%04d" % i) * 1024
            response = StreamingResponse(chunks(), media_type="application/octet-stream")
        else:
            response = HTMLResponse("<html>firewall</html>", headers={"Content-Security-Policy": "default-src 'self'"})
            response.headers.append("set-cookie", "PHPSESSID=fw-session-1; path=/; secure; HttpOnly")
            response.headers.append("x-powered-by", "PHP")
        await response(scope, receive, send)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def firewall():
    return MockFirewall()


@pytest_asyncio.fixture
async def setup(gui_env, firewall):  # noqa: F811
    admin = await _https_login(settings.admin_username, settings.admin_password)
    host_id = await _host()
    await _enable(admin)
    clock = Clock()
    gateway = gw.GuiGateway(
        backend=gw.BackendClient("http://backend:8000", GW_TOKEN, transport=ASGITransport(app=backend_app)),
        slot_origins=gw.slot_origins_from(SITES),
        upstream_factory=lambda gs: httpx.AsyncClient(transport=ASGITransport(app=firewall)),
        clock=clock,
    )
    browser = AsyncClient(transport=ASGITransport(app=gateway), base_url=OWN)
    yield {"admin": admin, "host_id": host_id, "gateway": gateway, "browser": browser, "clock": clock, "fw": firewall}
    await browser.aclose()
    await admin.aclose()


async def _ticket(s) -> tuple[str, int]:
    data = (await s["admin"].post(f"/api/hosts/{s['host_id']}/gui-ticket")).json()
    return data["url"].split("?t=", 1)[1], data["slot"]


def _opnexus_cookie(s) -> str:
    return f"{SECURE_COOKIE_NAME}={s['admin'].cookies.get(SECURE_COOKIE_NAME)}"


async def _redeem(s, ticket: str, slot: int = 9441, site: str = "same-site", cookie: str | None = None):
    return await s["browser"].get(f"/__onx/redeem?t={ticket}", headers={
        "X-Onx-Slot": str(slot), "Sec-Fetch-Site": site, "Cookie": _opnexus_cookie(s) if cookie is None else cookie})


async def _open(s) -> str:
    ticket, slot = await _ticket(s)
    resp = await _redeem(s, ticket, slot)
    assert resp.status_code == 200, resp.text
    raw = resp.headers["set-cookie"]
    return raw.split(";")[0]  # "__Host-onx-gw-9441=<value>"


async def _get(s, path: str, gw_cookie: str, slot: int = 9441, site: str | None = "same-origin", extra: dict | None = None,
               method: str = "GET", **kwargs):
    headers = {"X-Onx-Slot": str(slot), "Cookie": f"{_opnexus_cookie(s)}; {gw_cookie}; other=1", **(extra or {})}
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    return await s["browser"].request(method, path, headers=headers, **kwargs)


# ── redeem ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_redeem_sets_host_cookie_and_clears_site_data(setup):
    ticket, slot = await _ticket(setup)
    resp = await _redeem(setup, ticket, slot)
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"__Host-onx-gw-{slot}=")
    for attr in ("Path=/", "Secure", "HttpOnly", "SameSite=Strict"):
        assert attr in cookie
    assert "Domain" not in cookie and ticket not in resp.text
    assert resp.headers["clear-site-data"] == '"cache", "storage"'
    assert 'http-equiv="refresh"' in resp.text and resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_redeem_refused_without_session_cookie_cross_site_or_twice(setup):
    ticket, _ = await _ticket(setup)
    # Refused by the gateway itself, before the backend sees (and burns) the ticket:
    assert (await _redeem(setup, ticket, cookie="")).status_code == 403
    assert (await _redeem(setup, ticket, site="cross-site")).status_code == 403
    assert (await _redeem(setup, ticket, site="")).status_code == 403
    assert (await _redeem(setup, ticket)).status_code == 200
    assert (await _redeem(setup, ticket)).status_code == 403  # single use
    other = await _ticket(setup)
    assert (await _redeem(setup, other[0], slot=9442)).status_code == 403  # bound to its slot


# ── cookies never cross ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_browser_cookies_never_upstream_and_firewall_cookies_never_downstream(setup):
    cookie = await _open(setup)
    first = await _get(setup, "/ui/core/dashboard", cookie)
    assert first.status_code == 200 and "set-cookie" not in first.headers and "x-powered-by" not in first.headers
    second = await _get(setup, "/ui/x", cookie)
    assert second.status_code == 200
    up = setup["fw"].requests
    assert "cookie" not in up[0]["headers"]
    assert up[1]["headers"]["cookie"] == "PHPSESSID=fw-session-1"
    session_value = setup["admin"].cookies.get(SECURE_COOKIE_NAME)
    for r in up:
        flat = repr(r["headers"])
        assert session_value not in flat and "onx-gw" not in flat and "other=1" not in flat
    # A firewall logout deletes the cookie from the jar.
    await _get(setup, "/logout", cookie)
    await _get(setup, "/ui/y", cookie)
    assert "cookie" not in setup["fw"].requests[-1]["headers"]


@pytest.mark.asyncio
async def test_request_rebuilt_with_header_allow_list(setup):
    cookie = await _open(setup)
    await _get(setup, "/ui/a?b=1", cookie, extra={
        "Authorization": "Basic Zm9vOmJhcg==", "Referer": f"{OWN}/ui/prev", "X-Forwarded-For": "192.0.2.99",
        "X-CSRFToken": "csrf-1", "Accept-Language": "de", "Proxy-Authorization": "x",
    })
    r = setup["fw"].requests[-1]
    assert r["path"] == "/ui/a" and r["query"] == "b=1"
    h = r["headers"]
    assert h["host"] == "fw-gui.test" and h["x-csrftoken"] == "csrf-1" and h["accept-language"] == "de"
    for dropped in ("authorization", "referer", "origin", "x-forwarded-for", "proxy-authorization", "x-onx-slot",
                    "sec-fetch-site"):
        assert dropped not in h


# ── redirects ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("target,expected", [
    ("https://evil.example/x", None), ("//evil.example/x", None), ("/\\evil.example", None),
    ("https://fw-gui.test:8443/x", None), ("http://fw-gui.test/x", None),
    ("https://fw-gui.test/ui/x?y=1", "/ui/x?y=1"), ("https://FW-GUI.test:443/", "/"), ("/ui/z", "/ui/z"),
])
async def test_location_rewritten_or_dropped(setup, target, expected):
    cookie = await _open(setup)
    resp = await _get(setup, "/redir", cookie, params={"to": target})
    if expected is None:
        assert resp.status_code == 502 and "location" not in resp.headers
    else:
        assert resp.status_code == 302 and resp.headers["location"] == expected


# ── request shape ─────────────────────────────────────────────────────────

async def _raw(gateway, method="GET", path="/", raw_path=None, headers=None, scope_type="http"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)
    scope = {"type": scope_type, "method": method, "path": path, "raw_path": raw_path if raw_path is not None else path.encode(),
             "query_string": b"", "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    await gateway(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_absolute_form_connect_upgrade_websocket_service_worker_refused(setup):
    g = setup["gateway"]
    base = {"x-onx-slot": "9441", "sec-fetch-site": "same-origin"}
    assert (await _raw(g, path="http://evil.example/", headers=base))[0]["status"] == 400
    assert (await _raw(g, path="/", raw_path=b"http://evil.example/", headers=base))[0]["status"] == 400
    assert (await _raw(g, path="//evil.example/", headers=base))[0]["status"] == 400
    assert (await _raw(g, method="CONNECT", path="/", headers=base))[0]["status"] == 501
    assert (await _raw(g, method="TRACE", path="/", headers=base))[0]["status"] == 501
    assert (await _raw(g, headers={**base, "upgrade": "websocket", "connection": "Upgrade"}))[0]["status"] == 400
    assert (await _raw(g, headers={**base, "service-worker": "script"}))[0]["status"] == 403
    assert (await _raw(g, scope_type="websocket"))[0]["type"] == "websocket.close"
    assert setup["fw"].requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["", "abc", "9999"])
async def test_slot_header_required(setup, slot):
    sent = await _raw(setup["gateway"], headers={"x-onx-slot": slot, "sec-fetch-site": "same-origin"})
    assert sent[0]["status"] == 400


# ── Sec-Fetch-Site and Origin ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("site,expected", [("same-site", 403), ("cross-site", 403), (None, 403), ("none", 200), ("same-origin", 200)])
async def test_sec_fetch_site_enforced(setup, site, expected):
    cookie = await _open(setup)
    assert (await _get(setup, "/ui/x", cookie, site=site)).status_code == expected


@pytest.mark.asyncio
async def test_origin_required_for_writes(setup):
    cookie = await _open(setup)
    assert (await _get(setup, "/api/x", cookie, method="POST", content=b"{}")).status_code == 403
    other_slot = await _get(setup, "/api/x", cookie, method="POST", content=b"{}", extra={"Origin": "https://opnexus.example:9442"})
    assert other_slot.status_code == 403
    opnexus = await _get(setup, "/api/x", cookie, method="POST", content=b"{}", extra={"Origin": "https://opnexus.example"})
    assert opnexus.status_code == 403
    assert (await _get(setup, "/api/x", cookie, method="POST", content=b"{}", extra={"Origin": OWN})).status_code == 200


@pytest.mark.asyncio
async def test_foreign_slot_cookie_rejected(setup):
    cookie = await _open(setup)  # slot 9441
    value = cookie.split("=", 1)[1]
    resp = await _get(setup, "/ui/x", f"__Host-onx-gw-9442={value}", slot=9442)
    assert resp.status_code == 401
    assert (await _get(setup, "/ui/x", "", slot=9441)).status_code == 401
    assert (await _get(setup, "/__onx/anything", cookie)).status_code == 404


# ── response hardening ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_security_headers_and_streaming(setup):
    cookie = await _open(setup)
    resp = await _get(setup, "/ui/x", cookie)
    csps = resp.headers.get_list("content-security-policy")
    assert any("frame-ancestors 'none'" in c and "connect-src 'self'" in c and "form-action 'self'" in c for c in csps)
    assert "default-src 'self'" in csps  # the firewall's own CSP applies as well
    assert resp.headers["x-frame-options"] == "DENY" and resp.headers["cache-control"] == "no-store"
    big = await _get(setup, "/big", cookie)
    assert big.status_code == 200 and len(big.content) == 50 * 4 * 1024 and big.content.endswith(b"0049" * 1024)


@pytest.mark.asyncio
async def test_body_limit(setup, monkeypatch):
    cookie = await _open(setup)
    monkeypatch.setattr(gw, "BODY_LIMIT", 1000)
    resp = await _get(setup, "/api/x", cookie, method="POST", content=b"x" * 2000, extra={"Origin": OWN})
    assert resp.status_code == 413 and setup["fw"].requests == []

    async def stream():
        for _ in range(4):
            yield b"y" * 600
    chunked = await _get(setup, "/api/x", cookie, method="POST", content=stream(), extra={"Origin": OWN})
    assert chunked.status_code == 413 and setup["fw"].requests == []


# ── login protection and logging ──────────────────────────────────────────

def _login(password: str) -> dict:
    return {"content": f"usernamefld=root&passwordfld={password}&login=1".encode(),
            "extra": {"Origin": OWN, "Content-Type": "application/x-www-form-urlencoded"}}


@pytest.mark.asyncio
async def test_login_rate_limit_per_firewall(setup):
    cookie = await _open(setup)
    for _ in range(3):
        assert (await _get(setup, "/", cookie, method="POST", **_login("wrong"))).status_code == 200
    blocked = await _get(setup, "/", cookie, method="POST", **_login("right"))
    assert blocked.status_code == 429
    assert sum(1 for r in setup["fw"].requests if r["method"] == "POST") == 3
    setup["clock"].now += gw.LOGIN_FAILURE_WINDOW + 1
    await _get(setup, "/ui/x", cookie)  # (session re-check due; still valid)
    assert (await _get(setup, "/", cookie, method="POST", **_login("right"))).status_code == 302


@pytest.mark.asyncio
async def test_multipart_login_counted_too(setup):
    # PHP's $_POST is filled from multipart bodies as well: a login posted that way must not bypass the limit.
    cookie = await _open(setup)
    files = {"login": (None, "1"), "usernamefld": (None, "root"), "passwordfld": (None, "wrong")}
    codes = [(await _get(setup, "/", cookie, method="POST", files=files, extra={"Origin": OWN})).status_code
             for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert not gw.is_login_attempt("POST", "multipart/form-data; boundary=x", b'name="file"; name="login"')


@pytest.mark.asyncio
async def test_path_forwarded_as_sent_and_control_characters_refused(setup):
    cookie = await _open(setup)
    for sent, seen in (("/a%3Fb=1", "/a?b=1"), ("/api/x%2Fy", "/api/x/y"), ("/a%2541", "/a%41"), ("/a%23f", "/a#f")):
        resp = await _get(setup, sent, cookie)
        last = setup["fw"].requests[-1]
        # Reaches the firewall still encoded (or decoded exactly once): never split into a query, never
        # decoded twice (%2541 must not become "A").
        assert resp.status_code == 200 and last["path"] in (sent, seen) and last["query"] == "", (sent, last)
    before = len(setup["fw"].requests)
    resp = await _get(setup, "/a%0d%0aX-Injected:%201", cookie)
    assert resp.status_code in (200, 400)
    assert all("x-injected" not in r["headers"] for r in setup["fw"].requests[before:])


@pytest.mark.asyncio
async def test_successful_login_not_counted(setup):
    cookie = await _open(setup)
    for _ in range(5):
        assert (await _get(setup, "/", cookie, method="POST", **_login("right"))).status_code == 302


@pytest.mark.asyncio
async def test_password_and_ticket_never_logged(setup, caplog):
    caplog.set_level(logging.DEBUG)
    ticket, slot = await _ticket(setup)
    resp = await _redeem(setup, ticket, slot)
    cookie = resp.headers["set-cookie"].split(";")[0]
    await _get(setup, "/", cookie, method="POST", **_login(PASSWORD))
    await _get(setup, "/ui/x?secretquery=abc", cookie)
    text = caplog.text
    assert "POST / 200" in text and "GET /ui/x 200" in text
    for leaked in (PASSWORD, ticket, "secretquery", "usernamefld", cookie.split("=", 1)[1], "fw-session-1"):
        assert leaked not in text


def test_uvicorn_runs_without_access_log(monkeypatch):
    captured = {}
    monkeypatch.setenv("GUI_GATEWAY_TOKEN", GW_TOKEN)
    monkeypatch.setenv("OPNEXUS_GUI_SITES", SITES)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    gw.main()
    assert captured["access_log"] is False and captured["http"] == "h11" and captured["proxy_headers"] is False


def test_gateway_refuses_to_start_without_token(monkeypatch):
    monkeypatch.setenv("GUI_GATEWAY_TOKEN", "short")
    monkeypatch.setenv("OPNEXUS_GUI_SITES", SITES)
    with pytest.raises(SystemExit):
        gw.create_app()


# ── session lifetime ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revalidation_failure_ends_session(setup):
    cookie = await _open(setup)
    assert (await _get(setup, "/ui/x", cookie)).status_code == 200
    await setup["admin"].post("/api/auth/logout")
    assert (await _get(setup, "/ui/x", cookie)).status_code == 200  # not yet due (≤ 30 s)
    setup["clock"].now += 31
    assert (await _get(setup, "/ui/x", cookie)).status_code == 401
    assert setup["gateway"].sessions == {}


@pytest.mark.asyncio
async def test_sweep_ends_idle_sessions_and_reports(setup):
    cookie = await _open(setup)
    setup["clock"].now += 15 * 60
    await setup["gateway"].sweep()
    assert setup["gateway"].sessions == {}
    assert (await _get(setup, "/ui/x", cookie)).status_code == 401
    async with SessionLocal() as db:
        assert (await db.scalars(select(GuiSession.end_reason))).one() == "idle"


@pytest.mark.asyncio
async def test_absolute_deadline(setup):
    cookie = await _open(setup)
    gs = next(iter(setup["gateway"].sessions.values()))
    gs.absolute_deadline = setup["gateway"].wall_clock() - 1
    assert (await _get(setup, "/ui/x", cookie)).status_code == 401
    assert setup["gateway"].sessions == {}


@pytest.mark.asyncio
async def test_backend_unreachable_fails_closed(setup):
    cookie = await _open(setup)

    async def broken(gs):
        raise httpx.ConnectError("down")
    setup["gateway"].backend.revalidate = broken
    setup["clock"].now += 31
    assert (await _get(setup, "/ui/x", cookie)).status_code == 503
    assert len(setup["fw"].requests) == 0


# ── upstream transport and isolation ──────────────────────────────────────

def _pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OPNsense.localdomain")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_upstream_client_pins_and_uses_overlay_proxy(monkeypatch):
    captured = {}
    monkeypatch.setattr(gw.httpx, "AsyncClient", lambda **kw: captured.update(kw) or "client")
    gs = gw.GatewaySession(gui_session_id="x", session_hash="y", slot=9441, upstream_origin="https://203.0.113.5",
                           ca_pem=_pem(), proxy_url="http://wg-hub:8888", idle_timeout=900, absolute_deadline=0,
                           revalidate_every=30)
    assert gw.build_upstream_client(gs) == "client"
    ctx = captured["verify"]
    assert ctx.check_hostname is False and ctx.verify_mode.name == "CERT_REQUIRED"
    assert captured["proxy"] == "http://wg-hub:8888" and captured["follow_redirects"] is False and captured["trust_env"] is False
    gs.ca_pem, gs.proxy_url = None, None
    gw.build_upstream_client(gs)
    assert captured["verify"] is True and captured["proxy"] is None


def test_gateway_never_loads_the_database_layer():
    code = ("import sys, app.gui_gateway as g; "
            "g.build_upstream_client(g.GatewaySession('x','y',9441,'https://203.0.113.5',None,None,1,1,1)); "
            "from app.services import tls_pin_service; "
            "bad=[m for m in ('app.database','sqlalchemy','app.models') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
