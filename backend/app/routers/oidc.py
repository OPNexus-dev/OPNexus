"""OIDC single sign-on: public login endpoints and admin-only settings (see services/oidc_service.py)."""
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.user import AdminUser
from app.routers.auth import _set_session_cookie, client_ip, current_user
from app.schemas.oidc import OidcSettingsRead, OidcSettingsUpdate
from app.security_middleware import _origin_allowed
from app.services import auth_service, notification_secret_store, oidc_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oidc"])
CALLBACK_PATH = "/api/auth/oidc/callback"


def _login_error(code: str) -> RedirectResponse:
    return RedirectResponse(f"/login?sso_error={code}", status_code=302)


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
        url = await oidc_service.start(db, cfg, f"{parts.scheme}://{parts.netloc}{CALLBACK_PATH}")
    except oidc_service.OidcError as exc:
        logger.warning("OIDC start fehlgeschlagen: %s", exc)
        return _login_error(exc.code)
    return RedirectResponse(url, status_code=302)


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
            raise oidc_service.OidcError("provider", f"Anbieter meldet {error or 'keinen Code'}")
        user = await oidc_service.complete(db, cfg, code, state)
    except oidc_service.OidcError as exc:
        auth_service.throttle.record_failure(ip, "oidc")
        logger.warning("OIDC-Anmeldung abgelehnt (%s): %s", exc.code, exc)
        return _login_error(exc.code)
    auth_service.throttle.reset(ip, "oidc")
    await auth_service.finalize_login(db, user, ip)
    token, _ = await auth_service.create_session(db, user, user_agent=request.headers.get("User-Agent"), ip_address=ip)
    response = RedirectResponse("/dashboard", status_code=302)
    _set_session_cookie(response, token)
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
