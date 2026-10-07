import uuid
from app.services import audit_service

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.database import SessionLocal
from app.errors import ErrorCode
from app.routers.auth import COOKIE_NAME
from app.services import auth_service

_PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/health",
}

# /docs, /redoc and /openapi.json used to be public — fully documenting a
# firewall-fleet-management API surface to anyone unauthenticated is
# unnecessary recon for an attacker probing a public instance. Kept out
# of _PUBLIC_PATHS so a logged-in browser session (cookie sent
# automatically, same-origin) still sees them fine, anonymous requests
# now get the normal 401 like any other endpoint.

# A read_only user can call any GET, plus these specific writes — both are
# purely self-scoped (only ever touch the caller's own row, never app
# config or another account) so blocking them would serve no security
# purpose while breaking normal self-service: change-password so a
# read_only account can always get out of a leaked/weak password, and
# PUT /settings because that endpoint only ever writes the caller's own
# UserPreferences.theme (see routers/settings.py) — a read_only user must
# still be able to pick light/dark/system for themselves.
_READ_ONLY_ALLOWED_WRITES = {
    "/api/auth/change-password",
    "/api/settings",
    # Self-scoped: a user managing their own second factor.
    "/api/auth/2fa/setup",
    "/api/auth/2fa/enable",
    "/api/auth/2fa/disable",
}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME, "")
        if token:
            async with SessionLocal() as db:
                user = await auth_service.resolve_session(db, token)
            if user is not None:
                if (
                    user.role == "read_only"
                    and request.method not in ("GET", "HEAD", "OPTIONS")
                    and request.url.path not in _READ_ONLY_ALLOWED_WRITES
                ):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": {"code": ErrorCode.READ_ONLY_ACCESS.value, "params": {}}},
                    )
                request.state.user = user
                parts = request.url.path.strip("/").split("/")
                kinds = {"rules": "rule", "aliases": "alias", "port-forward": "port_forward", "one-to-one": "one_to_one", "outbound-nat": "outbound_nat", "hosts": "host", "users": "user"}
                kind = kinds.get(parts[1]) if len(parts) > 1 else None
                tracked = kind and request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.url.path.endswith(("/preview", "/carp-preview", "/revisions"))
                action = ("delete" if request.method == "DELETE" else parts[-1] if len(parts) > 3 else "edit" if request.method in ("PUT", "PATCH") else "create")
                token = audit_service.context.set({"actor": user.username, "id": str(uuid.uuid4()), "action": action})
                event = {"kind": kind, "action": action, "entity_id": parts[2] if len(parts) > 2 else None,
                         "host_id": parts[2] if kind == "host" and len(parts) > 2 else None,
                         "details": {"method": request.method, "path": request.url.path}}
                try:
                    if tracked:
                        await audit_service.persist(**event, status="requested")
                    response = await call_next(request)
                    if tracked:
                        await audit_service.persist(**event, status="accepted" if response.status_code < 400 else "rejected",
                                                    message=f"HTTP {response.status_code}")
                    return response
                except Exception:
                    if tracked:
                        await audit_service.persist(**event, status="interrupted", message="Anfrage nicht abgeschlossen; Zustand vor Wiederholung prüfen.")
                    raise
                finally:
                    audit_service.context.reset(token)

        return JSONResponse(status_code=401, content={"detail": {"code": ErrorCode.NICHT_AUTHENTIFIZIERT.value, "params": {}}})
