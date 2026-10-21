"""Defence-in-depth HTTP hardening for the API.

OriginCheckMiddleware: cookie sessions are SameSite=Lax, which already blocks
most cross-site writes; this additionally rejects any state-changing request
whose Origin/Referer is neither the host it was sent to (same-origin, which is
how the proxy setup works for IP *and* name access) nor an explicitly allowed
origin. Requests without either header (curl, scripts) are not browsers, so
they cannot be riding on a victim's cookie and pass through to normal auth.

Sec-Fetch-Site (1.27): a page on another port of the same host (e.g. a
firewall GUI behind the GUI gateway) is *same-site* to OPNexus, so the
browser attaches the session cookie to its requests and SameSite=Lax does
not help. Cookie-carrying /api requests that the browser itself marks as
same-site or cross-site are therefore refused — unless the Origin is an
explicitly configured frontend origin (development setup with the frontend
on its own port). Clients that send no Sec-Fetch-Site header (curl,
scripts, API tokens) are not browsers and stay unaffected.

SecurityHeadersMiddleware: API responses must never be cached by browsers or
proxies and must not be sniffed or framed.
"""
import re
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings
from app.errors import ErrorCode
from app.routers.auth import has_session_cookie

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin_allowed(origin: str, host_header: str) -> bool:
    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        return False
    if parsed.netloc.lower() == host_header.lower():
        # Same host is not enough on an HTTPS deployment: an http:// page on that
        # very host (plain-HTTP port, downgraded first visit) is a different
        # origin. The backend only sees the proxy's plain-HTTP hop, so the
        # deployment's own COOKIE_SECURE switch says which scheme is legitimate.
        return parsed.scheme == "https" or not settings.cookie_secure
    return f"{parsed.scheme}://{parsed.netloc}".lower() in {o.lower() for o in settings.allowed_origins}


# The SSO provider sends the browser back here by a cross-site navigation;
# the endpoint authenticates by its own state cookie and creates the session.
_SEC_FETCH_EXEMPT = {"/api/auth/oidc/callback"}


def _explicit_frontend_origin(origin: str) -> bool:
    origin = origin.rstrip("/").lower()
    if not origin or origin == "null":
        return False
    if settings.allowed_origins:
        return origin in {o.lower() for o in settings.allowed_origins}
    return re.fullmatch(settings.cors_origin_regex, origin) is not None


def sec_fetch_blocked(request: Request) -> bool:
    path = request.url.path
    if not (path == "/api" or path.startswith("/api/")) or path in _SEC_FETCH_EXEMPT:
        return False
    if not has_session_cookie(request):
        return False
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if site == "cross-site":
        return True
    if site == "same-site":
        return not _explicit_frontend_origin(request.headers.get("origin", ""))
    return False  # same-origin, none (typed URL/bookmark) or no header (not a browser)


class OriginCheckMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if sec_fetch_blocked(request):
            return JSONResponse(
                status_code=403,
                content={"detail": {"code": ErrorCode.CROSS_SITE_REQUEST_BLOCKED.value, "params": {}}},
            )
        if request.method not in _SAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and source != "null" and not _origin_allowed(source, request.headers.get("host", "")):
                return JSONResponse(
                    status_code=403,
                    content={"detail": {"code": ErrorCode.CROSS_ORIGIN_BLOCKED.value, "params": {}}},
                )
            if source == "null":
                return JSONResponse(
                    status_code=403,
                    content={"detail": {"code": ErrorCode.CROSS_ORIGIN_BLOCKED.value, "params": {}}},
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response
