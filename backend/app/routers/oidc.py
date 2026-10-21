"""OIDC single sign-on: public login endpoints and admin-only settings (see services/oidc_service.py)."""
import logging
import re
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.user import AdminUser
from app.routers.auth import _set_session_cookie, client_ip, current_user
from app.config import settings
from app.schemas.oidc import OidcSettingsRead, OidcSettingsUpdate
from app.security_middleware import _origin_allowed
from app.services import auth_service, notification_secret_store, oidc_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oidc"])
CALLBACK_PATH = "/api/auth/oidc/callback"
STATE_COOKIE = "opnexus-oidc-state"
# HTTPS deployments (1.29): __Host- (Secure, no Domain, Path=/) so that no sibling SUBDOMAIN or plain-HTTP page can
# plant a state cookie of its own (login CSRF with the attacker's own authorisation). Known limit: cookies are not
# separated by PORT, so script in a firewall GUI shown on a gateway slot port of the same host could still set it
# (BACKLOG S8, accepted: needs a compromised firewall GUI and a mapped IdP account; the real fix is one hostname
# per slot). The browser only accepts the prefix with Path=/, so the cookie travels with every request for its
# 10 minutes; plain-HTTP labs keep the narrow path.
SECURE_STATE_COOKIE = "__Host-opnexus-oidc-state"
STATE_COOKIE_PATH = "/api/auth/oidc"


def state_cookie_name() -> str:
    return SECURE_STATE_COOKIE if settings.cookie_secure else STATE_COOKIE


def _state_cookie_path() -> str:
    return "/" if settings.cookie_secure else STATE_COOKIE_PATH


def _clear_state_cookie(response) -> None:
    response.delete_cookie(state_cookie_name(), path=_state_cookie_path(), secure=settings.cookie_secure, httponly=True, samesite="lax")
    if settings.cookie_secure:  # a plain-name cookie of an earlier version
        response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH, secure=True, httponly=True, samesite="lax")
# SSO sessions are short: the provider's group membership is only re-checked at login, so a removed person
# must not keep a 30-day session.
SSO_SESSION_SECONDS = 12 * 3600


def _login_error(code: str) -> RedirectResponse:
    response = RedirectResponse(f"/login?sso_error={code}", status_code=302)
    _clear_state_cookie(response)
    return response


def _printable(value: str) -> str:
    """Query text from an unauthenticated caller, made safe for a log line (no CR/LF or other control chars)."""
    return re.sub(r"[^\x20-\x7e]", "?", value)[:60]


@router.get("/auth/oidc/config")
async def public_config(db: AsyncSession = Depends(get_db)):
    """For the login page: is there an SSO button, and what does it say. Nothing else is revealed."""
    cfg = await oidc_service.get_settings(db)
    ready = oidc_service.configured(cfg)
    return {"enabled": ready, "name": cfg.display_name if ready else None}


@router.get("/auth/oidc/start")
async def start(request: Request, origin: str, db: AsyncSession = Depends(get_db)):
    cfg = await oidc_service.get_settings(db)
    parts = urlsplit(origin)
    # The redirect URI is built from the browser's own origin (so any hostname the operator uses works), but only
    # an origin this server is actually reachable at is accepted: no path, no credentials, same host as the request.
    if (not oidc_service.configured(cfg) or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username
            or not _origin_allowed(f"{parts.scheme}://{parts.netloc}", request.headers.get("host", ""))):
        return _login_error("disabled")
    try:
        url, state = await oidc_service.start(db, cfg, f"{parts.scheme}://{parts.netloc}{CALLBACK_PATH}")
    except oidc_service.OidcError as exc:
        logger.warning("OIDC start fehlgeschlagen: %s", exc)
        return _login_error(exc.code)
    response = RedirectResponse(url, status_code=302)
    # Binds the attempt to THIS browser: a callback URL handed to somebody else (login CSRF) or a captured
    # code+state pair is useless without the cookie.
    response.set_cookie(state_cookie_name(), state, max_age=600, httponly=True, samesite="lax", secure=settings.cookie_secure,
                        path=_state_cookie_path())
    return response


@router.get("/auth/oidc/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = "", db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    if auth_service.throttle.retry_after(ip, "oidc") > 0:
        return _login_error("throttled")
    cfg = await oidc_service.get_settings(db)
    if not oidc_service.configured(cfg):
        return _login_error("disabled")
    try:
        if error or not code or not state:
            raise oidc_service.OidcError("provider", "Anbieter meldet " + (_printable(error) if error else "keinen Code"))
        # Only the cookie name of this deployment counts (on HTTPS never the plain one); no cookie never matches.
        bound = request.cookies.get(state_cookie_name(), "")
        if not bound or not secrets.compare_digest(bound.encode(), state.encode()):
            raise oidc_service.OidcError("state", "Der Anmeldeversuch gehört nicht zu diesem Browser")
        user = await oidc_service.complete(db, cfg, code, state)
    except oidc_service.OidcError as exc:
        auth_service.throttle.record_failure(ip, "oidc")
        logger.warning("OIDC-Anmeldung abgelehnt (%s): %s", exc.code, exc)
        return _login_error(exc.code)
    auth_service.throttle.reset(ip, "oidc")
    await auth_service.finalize_login(db, user, ip)
    token, _ = await auth_service.create_session(
        db, user, user_agent=request.headers.get("User-Agent"), ip_address=ip, lifetime_seconds=SSO_SESSION_SECONDS
    )
    response = RedirectResponse("/dashboard", status_code=302)
    _set_session_cookie(response, token, max_age=SSO_SESSION_SECONDS)
    _clear_state_cookie(response)
    logger.info("SSO-Anmeldung von '%s' (%s) aus %s", user.username, user.role, ip)
    return response


def _require_admin(user: AdminUser) -> None:
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)


@router.get("/settings/oidc", response_model=OidcSettingsRead)
async def read(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    return await oidc_service.get_settings(db)


@router.put("/settings/oidc", response_model=OidcSettingsRead)
async def update(payload: OidcSettingsUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    cfg = await oidc_service.get_settings(db)
    for field in ("enabled", "display_name", "issuer", "client_id", "scopes", "groups_claim", "admin_group", "operator_group", "read_only_group"):
        value = getattr(payload, field)
        if value is not None:
            setattr(cfg, field, value.strip() if isinstance(value, str) else value)
    if payload.clear_client_secret:
        notification_secret_store.delete_secret(oidc_service.SECRET_NAME)
        cfg.client_secret_set = False
    elif payload.client_secret is not None:
        notification_secret_store.save_secret(oidc_service.SECRET_NAME, payload.client_secret)
        cfg.client_secret_set = True
    await db.commit()
    oidc_service.clear_caches()
    return cfg


@router.post("/settings/oidc/test")
async def test(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    """Fetch the provider's discovery document and signing keys: proves the issuer URL, not the login."""
    _require_admin(user)
    cfg = await oidc_service.get_settings(db)
    if not cfg.issuer:
        raise api_error(422, ErrorCode.OIDC_TEST_FAILED, reason="Kein Aussteller eingetragen")
    try:
        oidc_service.clear_caches()
        doc = await oidc_service.discovery(cfg.issuer)
        await oidc_service._jwk_for(doc, None)
    except oidc_service.OidcError as exc:
        raise api_error(502, ErrorCode.OIDC_TEST_FAILED, reason=str(exc)) from exc
    return {"success": True, "authorization_endpoint": doc["authorization_endpoint"]}
