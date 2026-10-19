import uuid
from app.services import audit_service

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.database import SessionLocal
from app.errors import ErrorCode
from app.routers.auth import COOKIE_NAME
from app.routers.auth import client_ip
from app.services import api_token_service, auth_service

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
    # Read-only JSON-RPC tools (see routers/mcp.py).
    "/api/mcp",
    # Self-scoped: a user managing their own second factor.
    "/api/auth/2fa/setup",
    "/api/auth/2fa/enable",
    "/api/auth/2fa/disable",
}


# Resource families whose URLs are /api/<family>/<collection>/<id>/<action>
# (or /api/<family>/<action> for the account/notification endpoints). The
# older kinds below keep their original /api/<kind>/<id>/<action> parsing.
_SECURITY_KINDS = {
    "ipsec": "ipsec_tunnel", "openvpn": "openvpn_instance", "wireguard": "wireguard_tunnel",
    "certificates": "certificate", "acme": "acme", "notifications": "notification", "auth": "auth",
    "tokens": "api_token",
}
_COLLECTIONS = {"tunnels", "instances", "managed", "accounts", "certificates", "settings"}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _security_event(parts: list[str], method: str) -> dict:
    """kind/action/entity/host for VPN, certificate, notification and account-security
    requests. Only path segments are used — never query strings or bodies."""
    ids = [p for p in parts[2:] if _is_uuid(p)]
    tail = parts[-1]
    if parts[1] == "auth":
        action = "/".join(parts[2:])
    elif method == "DELETE":
        action = "delete"
    elif ids and not _is_uuid(tail):
        action = tail  # deploy / push / remove / register / sign / poll
    elif method in ("PUT", "PATCH"):
        action = "edit"
    elif not ids and tail not in _COLLECTIONS and len(parts) > 2:
        action = tail  # e.g. notifications/test
    else:
        action = "create"
    return {"kind": _SECURITY_KINDS[parts[1]], "action": action[:40], "entity_id": ids[0] if ids else None,
            "host_id": ids[1] if len(ids) > 1 else None}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        api_token = None
        bearer = api_token_service.bearer_from_header(request.headers.get("authorization", ""))
        token = request.cookies.get(COOKIE_NAME, "")
        if bearer is not None:
            # An API token wins over a cookie; a bad one never falls back to it.
            async with SessionLocal() as db:
                resolved = await api_token_service.resolve(db, bearer, client_ip(request))
            user, api_token = resolved if resolved is not None else (None, None)
            token = bearer
        elif token:
            async with SessionLocal() as db:
                user = await auth_service.resolve_session(db, token)
        if token:
            if user is not None and api_token is not None and api_token_service.denies(
                api_token.scope, request.method, request.url.path
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": {"code": ErrorCode.API_TOKEN_SCOPE_FORBIDDEN.value, "params": {"scope": api_token.scope}}},
                )
            if user is not None:
                parts = request.url.path.strip("/").split("/")
                kinds = {"rules": "rule", "aliases": "alias", "port-forward": "port_forward", "one-to-one": "one_to_one", "outbound-nat": "outbound_nat", "hosts": "host", "users": "user", "system": "system", "backups": "backup"}
                writing = request.method in ("POST", "PUT", "PATCH", "DELETE")
                if len(parts) > 1 and parts[1] in _SECURITY_KINDS:
                    event = {**_security_event(parts, request.method), "details": {"method": request.method, "path": request.url.path}}
                    kind = event["kind"]
                else:
                    kind = kinds.get(parts[1]) if len(parts) > 1 else None
                    if len(parts) > 2 and parts[1] == "settings":
                        kind = "settings"  # where OPNexus stores/sends things (own prefs at /api/settings stay unaudited)
                    action = ("delete" if request.method == "DELETE" else parts[-1] if len(parts) > 3 else "edit" if request.method in ("PUT", "PATCH") else "create")
                    event = {"kind": kind, "action": action, "entity_id": parts[2] if len(parts) > 2 else None,
                             "host_id": parts[2] if kind == "host" and len(parts) > 2 else None,
                             "details": {"method": request.method, "path": request.url.path}}
                tracked = kind and writing and not request.url.path.endswith(("/preview", "/carp-preview", "/revisions"))

                if (
                    user.role == "read_only"
                    and request.method not in ("GET", "HEAD", "OPTIONS")
                    and request.url.path not in _READ_ONLY_ALLOWED_WRITES
                    # Own API tokens (the router limits a read_only user to read scope).
                    and not request.url.path.startswith("/api/tokens")
                ):
                    if tracked:
                        # Refused attempts at security-relevant writes stay attributable.
                        ctx = audit_service.context.set({"actor": user.username if api_token is None else f"{user.username} (API: {api_token.name})"[:150], "id": str(uuid.uuid4()), "action": event["action"]})
                        try:
                            await audit_service.persist(**event, status="denied", message="Schreibzugriff für Nur-Lese-Konto verweigert")
                        finally:
                            audit_service.context.reset(ctx)
                    return JSONResponse(
                        status_code=403,
                        content={"detail": {"code": ErrorCode.READ_ONLY_ACCESS.value, "params": {}}},
                    )
                if user.role == "operator" and api_token_service.operator_denies(request.method, request.url.path):
                    if tracked:
                        ctx = audit_service.context.set({"actor": user.username if api_token is None else f"{user.username} (API: {api_token.name})"[:150], "id": str(uuid.uuid4()), "action": event["action"]})
                        try:
                            await audit_service.persist(**event, status="denied", message="Nur für Administratoren")
                        finally:
                            audit_service.context.reset(ctx)
                    return JSONResponse(status_code=403, content={"detail": {"code": ErrorCode.ADMIN_REQUIRED.value, "params": {}}})
                request.state.user = user
                request.state.api_token = api_token
                actor = user.username if api_token is None else f"{user.username} (API: {api_token.name})"[:150]
                token = audit_service.context.set({"actor": actor, "id": str(uuid.uuid4()), "action": event["action"]})
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
