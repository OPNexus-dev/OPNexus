import logging

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.config import settings
from app.database import get_db
from app.models.user import AdminUser
from app.services import api_token_service, auth_service, totp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Plain name: the session cookie of HTTP-only (lab) deployments. On HTTPS deployments it is NOT accepted any
# more (1.29): it is only still deleted (login, logout) so that a pre-1.27 cookie does not linger.
COOKIE_NAME = "opnexus-session"
# HTTPS deployments: the __Host- prefix makes the browser refuse any cookie of
# this name that is not Secure, has a Domain attribute or a Path other than
# "/". A page on another port of the same host (the GUI gateway slots, any
# other service on that machine) therefore cannot toss a narrower-path cookie
# that shadows the real session, and cannot overwrite it from script (HttpOnly).
SECURE_COOKIE_NAME = "__Host-opnexus-session"


def session_cookie_name() -> str:
    return SECURE_COOKIE_NAME if settings.cookie_secure else COOKIE_NAME


def session_token(request: Request) -> str:
    """The session token the browser sent: the __Host- cookie on HTTPS deployments, the plain one otherwise.
    Never the other name (1.29): a plain-name cookie can be planted by any plain-HTTP or sibling-subdomain page and
    must not be able to carry a session on an HTTPS deployment (the __Host- prefix does not separate PORTS, see
    BACKLOG S8)."""
    return request.cookies.get(session_cookie_name(), "")


def has_session_cookie(request: Request) -> bool:
    return bool(request.cookies.get(SECURE_COOKIE_NAME) or request.cookies.get(COOKIE_NAME))


def client_ip(request: Request) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # First entry is the original client; a trusted proxy
            # overwrites (not appends blindly to) this header, so this
            # is safe only under the condition documented on the config
            # flag itself.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, token: str, max_age: int | None = None) -> None:
    response.set_cookie(
        key=session_cookie_name(),
        value=token,
        max_age=max_age or settings.session_lifetime,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    if settings.cookie_secure:
        # A pre-1.27 plain-name cookie must not linger next to the new one.
        response.delete_cookie(key=COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
    if settings.cookie_secure:
        response.delete_cookie(key=SECURE_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")


def _check_account_budget(user: AdminUser, action: str) -> None:
    """429 once too many failed password/code attempts piled up for this account
    and action (see auth_service.account_throttle)."""
    wait = auth_service.account_throttle.retry_after(str(user.id), action)
    if wait > 0:
        raise api_error(status.HTTP_429_TOO_MANY_REQUESTS, ErrorCode.TOO_MANY_LOGIN_ATTEMPTS, wait=wait)


def _account_failure(user: AdminUser, action: str) -> None:
    auth_service.account_throttle.record_failure(str(user.id), action)


def _account_success(user: AdminUser, action: str) -> None:
    auth_service.account_throttle.reset(str(user.id), action)


async def resolve_user(request: Request, db: AsyncSession) -> AdminUser:
    # AuthMiddleware already resolved the caller (session cookie or API token), but
    # in its own, closed DB session: re-load into this request's session so that
    # changes made to the user (2FA, password...) are actually persisted.
    resolved = getattr(request.state, "user", None)
    if resolved is not None:
        user = await db.get(AdminUser, resolved.id)
        if user is not None:
            return user
    token = session_token(request)
    user = await auth_service.resolve_session(db, token)
    if user is None:
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.NICHT_AUTHENTIFIZIERT)
    return user


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> AdminUser:
    return await resolve_user(request, db)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=1024)
    # Authenticator code or one-time recovery code; only needed (and only
    # requested) once the password was right and the account has 2FA enabled.
    totp_code: str | None = Field(default=None, max_length=32)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=10, max_length=1024)


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)

    wait = auth_service.throttle.retry_after(ip, payload.username)
    if wait > 0:
        raise api_error(status.HTTP_429_TOO_MANY_REQUESTS, ErrorCode.TOO_MANY_LOGIN_ATTEMPTS, wait=wait)

    user = await auth_service.authenticate(db, payload.username, payload.password, ip, finalize=False)
    if user is None:
        auth_service.throttle.record_failure(ip, payload.username)
        logger.info("Failed login for '%s' from %s", payload.username, ip)
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.UNGUELTIGER_BENUTZERNAME_ODER_PASSWORT)

    verified_hash = user.password_hash
    if user.totp_enabled:
        # Only revealed after the password was correct, so it cannot be used
        # to find out which usernames exist or have 2FA.
        if not payload.totp_code:
            raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.TOTP_REQUIRED)
        if not await _accept_second_factor(db, user, payload.totp_code):
            auth_service.throttle.record_failure(ip, payload.username)
            logger.info("Failed second factor for '%s' from %s", payload.username, ip)
            raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.TOTP_INVALID)
    await auth_service.finalize_login(db, user, ip)

    auth_service.throttle.reset(ip, payload.username)
    failed_since_last_login = auth_service.throttle.pop_failures_since_success(payload.username)
    token, expires_at = await auth_service.create_session(
        db, user, user_agent=request.headers.get("User-Agent"), ip_address=ip, verified_hash=verified_hash
    )
    _set_session_cookie(response, token)
    logger.info("Successful login for '%s' from %s", user.username, ip)
    return {
        "authenticated": True,
        "username": user.username,
        "role": user.role,
        "previous_login_at": getattr(user, "previous_login_at", None),
        "previous_login_ip": getattr(user, "previous_login_ip", None),
        "failed_attempts_since_last_login": failed_since_last_login,
        "session_expires_at": expires_at,
    }


async def _accept_second_factor(db: AsyncSession, user: AdminUser, code: str) -> bool:
    """Authenticator code (replay-protected) or, failing that, a one-time recovery code.

    A code is single-use only if "check, then mark as used" cannot interleave
    with a second request carrying the same code. The user row is therefore
    re-read under a row lock (FOR UPDATE, fresh values) and the used step /
    consumed recovery hash is committed while holding it; a concurrent attempt
    blocks here and then sees the updated state.
    """
    locked = await db.scalar(
        select(AdminUser).where(AdminUser.id == user.id).with_for_update().execution_options(populate_existing=True)
    )
    if locked is None:
        return False
    secret = totp_service.read_secret(locked.id)
    if secret:
        step = totp_service.verify_code(secret, code, locked.totp_last_step)
        if step is not None:
            locked.totp_last_step = step
            await db.commit()
            return True
    remaining = totp_service.consume_recovery(locked.totp_recovery_hashes, code)
    if remaining is not None:
        locked.totp_recovery_hashes = remaining
        await db.commit()
        logger.warning("Recovery code used by '%s' (%d left)", locked.username, len(remaining))
        return True
    await db.commit()  # nothing changed — just release the row lock
    return False


class TotpSetupRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class TotpEnableRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class TotpDisableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=1, max_length=32)


@router.get("/2fa")
async def totp_status(user: AdminUser = Depends(current_user)):
    return {"enabled": user.totp_enabled, "recovery_codes_left": len(user.totp_recovery_hashes or [])}


@router.post("/2fa/setup")
async def totp_setup(payload: TotpSetupRequest, user: AdminUser = Depends(current_user)):
    """Step 1: needs the current password (a stolen session alone must not be able
    to add an attacker's authenticator). Returns the secret to scan; nothing is
    active until /2fa/enable proves the authenticator works."""
    if user.totp_enabled:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.TOTP_ALREADY_ENABLED)
    _check_account_budget(user, "2fa-setup")
    if not await auth_service.verify_password_async(user.password_hash, payload.password):
        _account_failure(user, "2fa-setup")
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.AKTUELLES_PASSWORT_IST_FALSCH)
    _account_success(user, "2fa-setup")
    secret = totp_service.generate_secret()
    totp_service.save_secret(user.id, secret, pending=True)
    return {"secret": secret, "otpauth_uri": totp_service.otpauth_uri(user.username, secret)}


@router.post("/2fa/enable")
async def totp_enable(
    payload: TotpEnableRequest, request: Request,
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user),
):
    if user.totp_enabled:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.TOTP_ALREADY_ENABLED)
    _check_account_budget(user, "2fa-enable")
    pending = totp_service.read_secret(user.id, pending=True)
    if not pending:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.TOTP_SETUP_MISSING)
    step = totp_service.verify_code(pending, payload.code)
    if step is None:
        # Not reset by a fresh /2fa/setup: recreating the pending enrolment
        # must not hand out a new guessing budget.
        _account_failure(user, "2fa-enable")
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.TOTP_INVALID)
    _account_success(user, "2fa-enable")
    plain, hashes = totp_service.new_recovery_codes()
    totp_service.promote_pending(user.id)
    user.totp_enabled = True
    user.totp_last_step = step
    user.totp_recovery_hashes = hashes
    # Other sessions were established without the second factor — end them.
    await auth_service.revoke_sessions(db, user.id, keep_token=session_token(request), commit=False)
    await db.commit()
    logger.info("Two-factor enabled for '%s'", user.username)
    return {"enabled": True, "recovery_codes": plain}


@router.post("/2fa/disable")
async def totp_disable(
    payload: TotpDisableRequest, request: Request,
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user),
):
    if not user.totp_enabled:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.TOTP_NOT_ENABLED)
    _check_account_budget(user, "2fa-disable")
    if not await auth_service.verify_password_async(user.password_hash, payload.password):
        _account_failure(user, "2fa-disable")
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.AKTUELLES_PASSWORT_IST_FALSCH)
    if not await _accept_second_factor(db, user, payload.code):
        _account_failure(user, "2fa-disable")
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.TOTP_INVALID)
    _account_success(user, "2fa-disable")
    totp_service.delete_secrets(user.id)
    user.totp_enabled = False
    user.totp_recovery_hashes = None
    user.totp_last_step = 0
    await auth_service.revoke_sessions(db, user.id, keep_token=session_token(request), commit=False)
    await db.commit()
    logger.info("Two-factor disabled for '%s'", user.username)
    return {"enabled": False}


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    token = session_token(request)
    await auth_service.revoke_session(db, token)
    legacy = request.cookies.get(COOKIE_NAME, "")
    if settings.cookie_secure and legacy and legacy != token:
        await auth_service.revoke_session(db, legacy)  # an old plain-name cookie ends with the logout too
    _clear_session_cookie(response)
    return {"authenticated": False}


@router.get("/me")
async def me(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    raw = api_token_service.bearer_from_header(request.headers.get("authorization", ""))
    if raw is not None:
        resolved = await api_token_service.resolve(db, raw, client_ip(request))
        if resolved is None:
            return {"authenticated": False}
        user, api_token = resolved
        return {"authenticated": True, "username": user.username, "role": user.role,
                "via": "token", "scope": api_token.scope, "token_name": api_token.name}
    token = session_token(request)
    user = await auth_service.resolve_session(db, token)
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "username": user.username, "role": user.role}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(current_user),
):
    _check_account_budget(user, "change-password")
    if not await auth_service.verify_password_async(user.password_hash, payload.current_password):
        _account_failure(user, "change-password")
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.AKTUELLES_PASSWORT_IST_FALSCH)
    _account_success(user, "change-password")
    if payload.new_password == payload.current_password:
        raise api_error(status.HTTP_400_BAD_REQUEST, ErrorCode.DAS_NEUE_PASSWORT_MUSS_SICH_VOM_AKTUELLEN_UNTERSCHEIDEN)

    # New hash, revocation of EVERY existing session (including other devices
    # an attacker may hold) and the replacement session commit as one unit.
    user.password_hash = await auth_service.hash_password_async(payload.new_password)
    await auth_service.revoke_sessions(db, user.id, commit=False)
    await api_token_service.revoke_all(db, user.id, commit=False)  # a changed password answers a suspected leak: tokens too
    token, _ = await auth_service.create_session(
        db, user, user_agent=request.headers.get("User-Agent"), ip_address=client_ip(request)
    )
    _set_session_cookie(response, token)
    return {"ok": True}
