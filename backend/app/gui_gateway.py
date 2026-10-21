"""The ``gui-gateway`` container: firewall web interfaces in their own browser origin (1.27).

Runs from the backend image (``python -m app.gui_gateway``) behind the proxy, which terminates TLS for the
slot ports and sets ``X-Onx-Slot`` (overwriting any client value). It never touches the database or the
secret stores; it only talks to the backend's internal API (``/internal/gui/*``, shared GUI_GATEWAY_TOKEN)
and, per gateway session, to exactly one firewall.

Security model (see the 1.27 design):
* Every slot port is its own origin. The firewall GUI therefore never runs in the OPNexus origin.
* Cookies never cross: the browser's ``Cookie`` header is not forwarded (it carries the OPNexus session),
  and the firewall's ``Set-Cookie`` never reaches the browser — a server-side jar per gateway session
  (memory only) holds them. The browser only gets ``__Host-onx-gw-<slot>``.
* Each upstream request is built from scratch: method, path and query from the browser, a small header
  allow-list, the jar's cookies; target origin, pinned certificate and overlay proxy from the backend.
  Absolute-form targets, CONNECT/TRACE, ``Upgrade`` and service-worker script requests are refused.
* ``Sec-Fetch-Site`` must be same-origin/none (same-site only for the redeem step), fail-closed;
  non-GET/HEAD requests also need the slot's own ``Origin``.
* ``Location`` pointing anywhere but the firewall is dropped; responses get CSP, X-Frame-Options DENY,
  ``Cache-Control: no-store``.
* At most 3 failed GUI logins per firewall in 5 minutes (protects OPNexus' API access from the firewall's
  login lockout, which would see all attempts coming from OPNexus).
* Idle and absolute limits; every 20 s the backend re-checks the session (OPNexus session, admin role,
  switch, host). Logs carry method, path (no query), status and duration only.

OPNsense behaviour this relies on (opnsense/core, src/www/authgui.inc, master as of 2026-10-21):
* session_auth(): a login is ``isset($_POST['login']) && !empty($_POST['usernamefld']) &&
  !empty($_POST['passwordfld'])`` posted to the current URL (login form has no action). Success sends a
  ``Location:`` header and exits (302); a failure re-renders the login form (200) with "Wrong username or
  password." and an auth_log entry.
* check_security_http_referer_enforcement() only runs when HTTP_REFERER is set, and wants the firewall's
  own host name/IP — so the gateway sends no Referer (and no Origin) upstream at all.
* check_security_dns_rebind() accepts an IP address or the configured host name as Host — the gateway
  sends the Host of the host record's base URL (httpx derives it from the URL).
"""
import asyncio
import hashlib
import logging
import os
import secrets
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import httpx

logger = logging.getLogger("gui_gateway")
# httpx/httpcore log full request URLs (query strings) at INFO/DEBUG: never in this process.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

LISTEN_PORT = 8080
COOKIE_PREFIX = "__Host-onx-gw-"
OPNEXUS_SESSION_COOKIE = "__Host-opnexus-session"
BODY_LIMIT = 64 * 1024 * 1024  # config restore uploads
LOGIN_FAILURE_LIMIT = 3
LOGIN_FAILURE_WINDOW = 300.0
UPSTREAM_TIMEOUT = 60.0
MAX_SESSIONS = 64

# Request headers copied from the browser; everything else (Cookie, Authorization, Origin, Referer,
# X-Forwarded-*, hop-by-hop, Sec-*, ...) is dropped.
_FORWARD_REQUEST_HEADERS = {
    "accept", "accept-language", "content-type", "user-agent", "x-requested-with", "x-csrftoken", "range",
}
# Response headers copied from the firewall. Set-Cookie goes to the jar, Location is rewritten.
_FORWARD_RESPONSE_HEADERS = {
    "content-type", "content-length", "content-encoding", "content-disposition", "content-language",
    "content-range", "accept-ranges", "content-security-policy",
}
_GATEWAY_CSP = ("connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'; "
                "object-src 'none'; worker-src 'none'")
_SECURITY_HEADERS = [
    (b"content-security-policy", _GATEWAY_CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"cache-control", b"no-store"),
    (b"pragma", b"no-cache"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def slot_origins_from(sites: str) -> dict[int, set[str]]:
    """OPNEXUS_GUI_SITES -> {port: {normalised origins}} (https only, explicit port)."""
    slots: dict[int, set[str]] = {}
    for raw in sites.split(","):
        raw = raw.strip().rstrip("/")
        try:
            parts = urlsplit(raw)
            port = parts.port
        except ValueError:
            continue
        if parts.scheme == "https" and parts.hostname and port:
            slots.setdefault(port, set()).add(_origin_of(raw))
    return slots


@dataclass
class GatewaySession:
    gui_session_id: str
    session_hash: str
    slot: int
    upstream_origin: str
    ca_pem: str | None
    proxy_url: str | None
    idle_timeout: float
    absolute_deadline: float
    revalidate_every: float
    last_activity: float = field(default_factory=time.monotonic)
    next_check: float = 0.0
    jar: dict[str, str] = field(default_factory=dict)
    client: httpx.AsyncClient | None = None


class BackendClient:
    """The backend's internal GUI API. Raises httpx.HTTPError when the backend is unreachable."""

    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=base_url, headers={"X-Onx-Gateway-Token": token},
                                         timeout=10.0, transport=transport)

    async def redeem(self, ticket_hash: str, session_hash: str, slot: int, client_ip: str | None) -> dict:
        resp = await self._client.post("/internal/gui/redeem", json={
            "ticket_hash": ticket_hash, "session_hash": session_hash, "slot": slot, "client_ip": client_ip})
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 403:
            return {"ok": False, "reason": (resp.json().get("detail") or {}).get("reason", "denied")}
        resp.raise_for_status()
        return {"ok": False, "reason": f"http_{resp.status_code}"}

    async def revalidate(self, gs: GatewaySession) -> dict:
        resp = await self._client.post("/internal/gui/revalidate",
                                       json={"gui_session_id": gs.gui_session_id, "session_hash": gs.session_hash})
        resp.raise_for_status()
        return resp.json()

    async def end(self, gs: GatewaySession, reason: str) -> None:
        try:
            await self._client.post("/internal/gui/end", json={
                "gui_session_id": gs.gui_session_id, "session_hash": gs.session_hash, "reason": reason})
        except httpx.HTTPError as exc:
            logger.warning("Could not report the end of a GUI session (%s)", type(exc).__name__)

    async def aclose(self) -> None:
        await self._client.aclose()


def build_upstream_client(gs: GatewaySession) -> httpx.AsyncClient:
    """Same transport rules as opnsense_client._client: pinned certificate (or system CAs), overlay proxy."""
    verify: ssl.SSLContext | bool
    if gs.ca_pem:
        from app.services.tls_pin_service import pinned_context  # no database layer behind this import
        verify = pinned_context(gs.ca_pem)
    else:
        verify = True
    return httpx.AsyncClient(verify=verify, proxy=gs.proxy_url, timeout=UPSTREAM_TIMEOUT,
                             follow_redirects=False, trust_env=False)


class LoginLimiter:
    """At most LOGIN_FAILURE_LIMIT failed GUI logins per firewall within LOGIN_FAILURE_WINDOW seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._failures: dict[str, deque] = {}
        self._clock = clock

    def _recent(self, key: str) -> deque:
        now = self._clock()
        q = self._failures.setdefault(key, deque())
        while q and now - q[0] > LOGIN_FAILURE_WINDOW:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= LOGIN_FAILURE_LIMIT

    def record_failure(self, key: str) -> None:
        self._recent(key).append(self._clock())


def is_login_attempt(method: str, content_type: str, body: bytes) -> bool:
    if method != "POST":
        return False
    if content_type.lower().startswith("multipart/form-data"):
        # PHP fills $_POST from multipart bodies too, so authgui.inc accepts a login posted this way.
        return all(f'name="{field}"'.encode() in body for field in ("login", "usernamefld", "passwordfld"))
    if not content_type.lower().startswith("application/x-www-form-urlencoded"):
        return False
    try:
        form = parse_qs(body.decode("latin-1"), keep_blank_values=True)
    except ValueError:
        return False
    return "login" in form and bool(form.get("usernamefld", [""])[0]) and bool(form.get("passwordfld", [""])[0])


def rewrite_location(location: str, upstream_origin: str) -> str | None:
    """Relative target for the browser, or None when the redirect leaves the firewall."""
    location = location.strip()
    if not location or "\\" in location or any(ord(c) < 0x20 for c in location):
        return None
    if location.startswith("//"):
        location = "https:" + location
    parts = urlsplit(location)
    if parts.scheme or parts.netloc:
        if _origin_of(location) != _origin_of(upstream_origin):
            return None
        path = parts.path or "/"
        return path + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
    return location


def _page(title: str, text_de: str, text_en: str) -> bytes:
    return (f"<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\"><title>{title}</title></head>"
            f"<body style=\"font-family:sans-serif;max-width:40rem;margin:3rem auto\"><h1>{title}</h1>"
            f"<p>{text_de}</p><p lang=\"en\">{text_en}</p></body></html>").encode()


_SESSION_GONE = _page("OPNexus", "Diese Sitzung ist abgelaufen oder ungültig. Öffne die Oberfläche erneut aus OPNexus.",
                      "This session has expired or is invalid. Open the interface again from OPNexus.")
_FORBIDDEN = _page("OPNexus", "Diese Anfrage wurde vom GUI-Gateway abgelehnt.", "This request was refused by the GUI gateway.")
_BAD_REDIRECT = _page("OPNexus", "Die Firewall wollte auf eine fremde Adresse umleiten; die Umleitung wurde verworfen.",
                      "The firewall tried to redirect to a foreign address; the redirect was dropped.")
_LOGIN_LOCKED = _page("OPNexus", "Zu viele fehlgeschlagene Anmeldungen an dieser Firewall. Bitte warte 5 Minuten.",
                      "Too many failed sign-ins at this firewall. Please wait 5 minutes.")
_UNAVAILABLE = _page("OPNexus", "Die Firewall oder OPNexus ist gerade nicht erreichbar.",
                     "The firewall or OPNexus is not reachable right now.")
_REDEEMED = (b"<!doctype html><html><head><meta charset=\"utf-8\"><meta http-equiv=\"refresh\" content=\"0;url=/\">"
             b"<title>OPNexus</title></head><body><a href=\"/\">&rarr;</a></body></html>")


class GuiGateway:
    def __init__(self, *, backend: BackendClient, slot_origins: dict[int, set[str]],
                 upstream_factory: Callable[[GatewaySession], httpx.AsyncClient] = build_upstream_client,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time):
        self.backend = backend
        self.slot_origins = slot_origins
        self.upstream_factory = upstream_factory
        self.clock = clock
        self.wall_clock = wall_clock
        self.sessions: dict[str, GatewaySession] = {}  # sha256(gateway cookie) -> session
        self.limiter = LoginLimiter(clock)
        self._loop_task: asyncio.Task | None = None

    # ── ASGI ──────────────────────────────────────────────────────────────
    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            # WebSocket and anything else: not supported (design: no WebSocket).
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        started = time.monotonic()
        status = await self._handle(scope, receive, send)
        # Method, path and status only: never the query string, headers or bodies.
        logger.info("%s %s %d %.0fms", scope.get("method", "?"), scope.get("path", "?")[:200], status,
                    (time.monotonic() - started) * 1000)

    async def _lifespan(self, receive, send):
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self._loop_task = asyncio.create_task(self._revalidate_loop())
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._loop_task:
                    self._loop_task.cancel()
                for key in list(self.sessions):
                    await self._drop(key, "shutdown")
                await self.backend.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _revalidate_loop(self):
        while True:
            await asyncio.sleep(10)
            try:
                await self.sweep()
            except Exception:  # never let the loop die
                logger.exception("GUI session sweep failed")

    async def sweep(self) -> None:
        """Idle/absolute limits and the periodic backend re-check for every gateway session."""
        for key, gs in list(self.sessions.items()):
            reason = self._expired(gs)
            if reason:
                await self._drop(key, reason)
            elif self.clock() >= gs.next_check:
                await self._recheck(key, gs)

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _headers(scope) -> dict[str, str]:
        result: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            name = k.decode("latin-1").lower()
            value = v.decode("latin-1")
            if name in result:
                value = f"{result[name]}{'; ' if name == 'cookie' else ', '}{value}"
            result[name] = value
        return result

    @staticmethod
    def _cookies(header: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for part in header.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name and name not in cookies:
                cookies[name] = value
        return cookies

    async def _respond(self, send, status: int, body: bytes = b"", headers: list | None = None,
                       content_type: bytes = b"text/html; charset=utf-8") -> int:
        out = [(b"content-type", content_type), (b"content-length", str(len(body)).encode())]
        out += list(headers or []) + _SECURITY_HEADERS
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": body})
        return status

    def _expired(self, gs: GatewaySession) -> str | None:
        if self.wall_clock() >= gs.absolute_deadline:
            return "absolute"
        if self.clock() - gs.last_activity >= gs.idle_timeout:
            return "idle"
        return None

    async def _drop(self, key: str, reason: str, *, report: bool = True) -> None:
        gs = self.sessions.pop(key, None)
        if gs is None:
            return
        gs.jar.clear()
        if gs.client is not None:
            await gs.client.aclose()
        if report:
            await self.backend.end(gs, reason)

    async def _recheck(self, key: str, gs: GatewaySession) -> bool | None:
        """True = still valid, False = ended (dropped), None = backend unreachable (fail closed, keep for retry)."""
        try:
            result = await self.backend.revalidate(gs)
        except httpx.HTTPError as exc:
            logger.warning("GUI session re-check failed (%s)", type(exc).__name__)
            return None
        if not result.get("ok"):
            await self._drop(key, str(result.get("reason", "revoked")), report=False)  # backend already ended it
            return False
        gs.absolute_deadline = min(gs.absolute_deadline, float(result.get("absolute_deadline", gs.absolute_deadline)))
        gs.idle_timeout = float(result.get("idle_timeout_seconds", gs.idle_timeout))
        gs.next_check = self.clock() + gs.revalidate_every
        return True

    # ── request handling ──────────────────────────────────────────────────
    async def _handle(self, scope, receive, send) -> int:
        method = scope.get("method", "")
        path = scope.get("path", "")
        raw_path = scope.get("raw_path") or path.encode()
        headers = self._headers(scope)

        if method in ("CONNECT", "TRACE"):
            return await self._respond(send, 501, _FORBIDDEN)
        # Origin-form only: "GET http://elsewhere/ HTTP/1.1" (absolute-form) or "//host" never reach upstream.
        if not path.startswith("/") or path.startswith("//") or not raw_path.startswith(b"/") \
                or raw_path.startswith(b"//") or "\\" in path:
            return await self._respond(send, 400, _FORBIDDEN)
        if "upgrade" in headers or "upgrade" in headers.get("connection", "").lower():
            return await self._respond(send, 400, _FORBIDDEN)
        if headers.get("service-worker", "").lower() == "script":
            return await self._respond(send, 403, _FORBIDDEN)

        try:
            slot = int(headers.get("x-onx-slot", ""))
        except ValueError:
            slot = 0
        own_origins = self.slot_origins.get(slot)
        if not own_origins:
            return await self._respond(send, 400, _FORBIDDEN)  # not coming through the proxy's slot block

        site = headers.get("sec-fetch-site", "").strip().lower()
        if path == "/__onx/redeem":
            if method != "GET" or site not in ("same-site", "same-origin", "none"):
                return await self._respond(send, 403, _FORBIDDEN)
            return await self._redeem(scope, headers, slot, send)
        if site not in ("same-origin", "none"):
            return await self._respond(send, 403, _FORBIDDEN)  # missing header = old/odd client: fail closed
        if method not in ("GET", "HEAD"):
            origin = headers.get("origin", "")
            if _origin_of(origin) not in own_origins:
                return await self._respond(send, 403, _FORBIDDEN)
        if path.startswith("/__onx/"):
            return await self._respond(send, 404, _FORBIDDEN)

        cookies = self._cookies(headers.get("cookie", ""))
        token = cookies.get(f"{COOKIE_PREFIX}{slot}", "")
        key = _sha(token) if token else ""
        gs = self.sessions.get(key) if key else None
        if gs is None or gs.slot != slot:
            return await self._respond(send, 401, _SESSION_GONE)
        reason = self._expired(gs)
        if reason:
            await self._drop(key, reason)
            return await self._respond(send, 401, _SESSION_GONE)
        if self.clock() >= gs.next_check:
            state = await self._recheck(key, gs)
            if state is False:
                return await self._respond(send, 401, _SESSION_GONE)
            if state is None:
                return await self._respond(send, 503, _UNAVAILABLE)
        gs.last_activity = self.clock()
        return await self._proxy(scope, receive, send, headers, gs)

    async def _redeem(self, scope, headers, slot: int, send) -> int:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        ticket = (query.get("t") or [""])[0]
        browser_session = self._cookies(headers.get("cookie", "")).get(OPNEXUS_SESSION_COOKIE, "")
        if not ticket or len(ticket) > 128 or not browser_session:
            return await self._respond(send, 403, _SESSION_GONE)
        client_ip = (headers.get("x-forwarded-for", "").split(",")[0].strip() or None)
        try:
            result = await self.backend.redeem(_sha(ticket), _sha(browser_session), slot, client_ip and client_ip[:45])
        except httpx.HTTPError as exc:
            logger.warning("Ticket redeem failed (%s)", type(exc).__name__)
            return await self._respond(send, 503, _UNAVAILABLE)
        if not result.get("ok"):
            return await self._respond(send, 403, _SESSION_GONE)
        # One browser session, one gateway session per slot: an older one in this slot is replaced.
        for old_key, old in list(self.sessions.items()):
            if old.slot == slot and old.session_hash == _sha(browser_session):
                await self._drop(old_key, "superseded", report=False)
        if len(self.sessions) >= MAX_SESSIONS:
            oldest = min(self.sessions, key=lambda k: self.sessions[k].last_activity)
            await self._drop(oldest, "superseded")
        value = secrets.token_urlsafe(32)
        gs = GatewaySession(
            gui_session_id=result["gui_session_id"], session_hash=_sha(browser_session), slot=slot,
            upstream_origin=result["upstream_origin"], ca_pem=result.get("ca_pem"), proxy_url=result.get("proxy_url"),
            idle_timeout=float(result["idle_timeout_seconds"]), absolute_deadline=float(result["absolute_deadline"]),
            revalidate_every=float(result.get("revalidate_seconds", 20)),
        )
        gs.last_activity = self.clock()
        gs.next_check = gs.last_activity + gs.revalidate_every
        self.sessions[_sha(value)] = gs
        cookie = f"{COOKIE_PREFIX}{slot}={value}; Path=/; Secure; HttpOnly; SameSite=Strict"
        # 200 + meta refresh instead of a 303: the follow-up request then starts in this origin
        # (Sec-Fetch-Site: same-origin) and the ticket leaves the address bar.
        return await self._respond(send, 200, _REDEEMED, headers=[
            (b"set-cookie", cookie.encode()), (b"clear-site-data", b'"cache", "storage"'),
        ])

    async def _read_body(self, receive) -> bytes | None:
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > BODY_LIMIT:
                return None
            chunks.append(chunk)
            if not message.get("more_body"):
                return b"".join(chunks)

    def _store_cookies(self, gs: GatewaySession, response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            jar = SimpleCookie()
            try:
                jar.load(header)
            except CookieError:
                continue
            for name, morsel in jar.items():
                expired = morsel["max-age"] in ("0",) or morsel.value in ("", "deleted")
                if not expired and morsel["expires"]:
                    try:
                        from email.utils import parsedate_to_datetime
                        expired = parsedate_to_datetime(morsel["expires"]).timestamp() <= self.wall_clock()
                    except (TypeError, ValueError):
                        pass
                if expired:
                    gs.jar.pop(name, None)
                else:
                    gs.jar[name] = morsel.value

    async def _proxy(self, scope, receive, send, headers: dict, gs: GatewaySession) -> int:
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > BODY_LIMIT:
            return await self._respond(send, 413, _FORBIDDEN)
        body = await self._read_body(receive)
        if body is None:
            return await self._respond(send, 413, _FORBIDDEN)
        method = scope["method"]
        login = is_login_attempt(method, headers.get("content-type", ""), body)
        if login and self.limiter.blocked(gs.upstream_origin):
            return await self._respond(send, 429, _LOGIN_LOCKED)

        out_headers = {k: v for k, v in headers.items() if k in _FORWARD_REQUEST_HEADERS}
        out_headers["accept-encoding"] = "identity"
        if gs.jar:
            out_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in gs.jar.items())
        query = scope.get("query_string", b"").decode("latin-1")
        # The path exactly as the browser sent it (still percent-encoded): the decoded scope["path"] would
        # turn %3F/%23 into query/fragment, %2F into a separator and decode %25xx a second time upstream.
        raw_path = (scope.get("raw_path") or scope["path"].encode()).split(b"?", 1)[0].decode("latin-1")
        url = gs.upstream_origin + raw_path + (f"?{query}" if query else "")
        if gs.client is None:
            gs.client = self.upstream_factory(gs)
        try:
            request = gs.client.build_request(method, url, headers=out_headers, content=body)
        except httpx.InvalidURL:
            return await self._respond(send, 400, _FORBIDDEN)  # e.g. control characters in the path
        try:
            response = await gs.client.send(request, stream=True)
        except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
            logger.warning("Upstream request failed (%s)", type(exc).__name__)
            return await self._respond(send, 502, _UNAVAILABLE)
        try:
            self._store_cookies(gs, response)
            if login and not (300 <= response.status_code < 400):
                self.limiter.record_failure(gs.upstream_origin)
            out = []
            for name, value in response.headers.multi_items():
                lname = name.lower()
                if lname in _FORWARD_RESPONSE_HEADERS:
                    out.append((lname.encode("latin-1"), value.encode("latin-1")))
            if "location" in response.headers:
                target = rewrite_location(response.headers["location"], gs.upstream_origin)
                if target is None:
                    await response.aclose()
                    return await self._respond(send, 502, _BAD_REDIRECT)
                out.append((b"location", target.encode("latin-1")))
            await send({"type": "http.response.start", "status": response.status_code, "headers": out + _SECURITY_HEADERS})
            async for chunk in response.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
            return response.status_code
        finally:
            await response.aclose()


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> GuiGateway:
    token = os.environ.get("GUI_GATEWAY_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("GUI_GATEWAY_TOKEN (at least 32 characters) is required")
    slots = slot_origins_from(os.environ.get("OPNEXUS_GUI_SITES", ""))
    if not slots:
        raise SystemExit("OPNEXUS_GUI_SITES must list the https slot origins")
    backend = BackendClient(os.environ.get("GUI_BACKEND_URL", "http://backend:8000"), token, transport)
    return GuiGateway(backend=backend, slot_origins=slots)


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # h11: an absolute-form target stays visible in scope["path"] (and is refused); access_log off because
    # uvicorn's access log would contain query strings (the redeem ticket).
    uvicorn.run(create_app(), host="0.0.0.0", port=LISTEN_PORT, http="h11", ws="none", access_log=False,
                server_header=False, date_header=False, proxy_headers=False, lifespan="on", log_config=None)


if __name__ == "__main__":
    main()
