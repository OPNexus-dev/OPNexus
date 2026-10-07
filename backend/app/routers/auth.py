import logging

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.config import settings
from app.database import get_db
from app.models.user import AdminUser
from app.services import auth_service, totp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "opnexus-session"


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


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.session_lifetime,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


async def resolve_user(request: Request, db: AsyncSession) -> AdminUser:
    token = request.cookies.get(COOKIE_NAME, "")
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
        db, user, user_agent=request.headers.get("User-Agent"), ip_address=ip
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
    """Authenticator code (replay-protected) or, failing that, a one-time recovery code."""
    secret = totp_service.read_secret(user.id)
    if secret:
        step = totp_service.verify_code(secret, code, user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            await db.commit()
            return True
    remaining = totp_service.consume_recovery(user.totp_recovery_hashes, code)
    if remaining is not None:
        user.totp_recovery_hashes = remaining
        await db.commit()
        logger.warning("Recovery code used by '%s' (%d left)", user.username, len(remaining))
        return True
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
    if not auth_service.verify_password(user.password_hash, payload.password):
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.AKTUELLES_PASSWORT_IST_FALSCH)
    secret = totp_service.generate_secret()
    totp_service.save_secret(user.id, secret, pending=True)
    return {"secret": secret, "otpauth_uri": totp_service.otpauth_uri(user.username, secret)}


@router.post("/2fa/enable")
async def totp_enable(payload: TotpEnableRequest, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    if user.totp_enabled:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.TOTP_ALREADY_ENABLED)
    pending = totp_service.read_secret(user.id, pending=True)
    if not pending:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.TOTP_SETUP_MISSING)
    step = totp_service.verify_code(pending, payload.code)
    if step is None:
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.TOTP_INVALID)
    plain, hashes = totp_service.new_recovery_codes()
    totp_service.promote_pending(user.id)
    user.totp_enabled = True
    user.totp_last_step = step
    user.totp_recovery_hashes = hashes
    await db.commit()
    logger.info("Two-factor enabled for '%s'", user.username)
    return {"enabled": True, "recovery_codes": plain}


@router.post("/2fa/disable")
async def totp_disable(payload: TotpDisableRequest, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    if not user.totp_enabled:
        raise api_error(status.HTTP_409_CONFLICT, ErrorCode.TOTP_NOT_ENABLED)
    if not auth_service.verify_password(user.password_hash, payload.password):
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.AKTUELLES_PASSWORT_IST_FALSCH)
    if not await _accept_second_factor(db, user, payload.code):
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.TOTP_INVALID)
    totp_service.delete_secrets(user.id)
    user.totp_enabled = False
    user.totp_recovery_hashes = None
    user.totp_last_step = 0
    await db.commit()
    logger.info("Two-factor disabled for '%s'", user.username)
    return {"enabled": False}


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get(COOKIE_NAME, "")
    await auth_service.revoke_session(db, token)
    _clear_session_cookie(response)
    return {"authenticated": False}


@router.get("/me")
async def me(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get(COOKIE_NAME, "")
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
    if not auth_service.verify_password(user.password_hash, payload.current_password):
        raise api_error(status.HTTP_401_UNAUTHORIZED, ErrorCode.AKTUELLES_PASSWORT_IST_FALSCH)
    if payload.new_password == payload.current_password:
        raise api_error(status.HTTP_400_BAD_REQUEST, ErrorCode.DAS_NEUE_PASSWORT_MUSS_SICH_VOM_AKTUELLEN_UNTERSCHEIDEN)

    user.password_hash = auth_service.hash_password(payload.new_password)
    await db.commit()

    await auth_service.revoke_session(db, request.cookies.get(COOKIE_NAME, ""))
    token, _ = await auth_service.create_session(
        db, user, user_agent=request.headers.get("User-Agent"), ip_address=client_ip(request)
    )
    _set_session_cookie(response, token)
    return {"ok": True}
