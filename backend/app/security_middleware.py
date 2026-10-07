"""Defence-in-depth HTTP hardening for the API.

OriginCheckMiddleware: cookie sessions are SameSite=Lax, which already blocks
most cross-site writes; this additionally rejects any state-changing request
whose Origin/Referer is neither the host it was sent to (same-origin, which is
how the proxy setup works for IP *and* name access) nor an explicitly allowed
origin. Requests without either header (curl, scripts) are not browsers, so
they cannot be riding on a victim's cookie and pass through to normal auth.

SecurityHeadersMiddleware: API responses must never be cached by browsers or
proxies and must not be sniffed or framed.
"""
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings
from app.errors import ErrorCode

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin_allowed(origin: str, host_header: str) -> bool:
    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        return False
    if parsed.netloc.lower() == host_header.lower():
        return True
    return f"{parsed.scheme}://{parsed.netloc}".lower() in {o.lower() for o in settings.allowed_origins}


class OriginCheckMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
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
