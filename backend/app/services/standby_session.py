"""Sign-in on a standby node (docs/DESIGN-HA-UI.md section 4).

The standby's database is a streaming replica: everything the normal sign-in does (record the login, create a session row,
re-hash a password, consume a recovery code) is a write and fails there. This is the one narrow door the standby offers so that
an administrator can still reach the "take over" button when the primary is dead:

- the password is checked against the replicated hash, nothing is rehashed or stamped;
- the second factor is the TOTP file (replicated by `opnexus ha sync`, up to five minutes old) with replay protection kept in
  memory; recovery codes are refused because using one up is a write;
- only local accounts with the admin role get in;
- the result is a signed token in a cookie of its own, valid for 15 minutes, kept nowhere (a restart ends every session).
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import ErrorCode, api_error
from app.models.user import AdminUser
from app.services import auth_service, totp_service

TTL_SECONDS = 15 * 60
_KEY = secrets.token_bytes(32)          # per process: a restart (and a promote restarts the backend) ends all standby sessions
_last_step: dict[uuid.UUID, int] = {}   # replay guard for TOTP codes, in memory only (the database is read-only here)
throttle = auth_service._LoginThrottle()  # separate budget from the normal sign-in


def cookie_name() -> str:
    return "__Host-opnexus-standby" if settings.cookie_secure else "opnexus-standby"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(body: str) -> str:
    return _b64(hmac.new(_KEY, body.encode(), hashlib.sha256).digest())


def issue(username: str, now: float | None = None) -> str:
    body = _b64(json.dumps({"u": username, "e": int((time.time() if now is None else now) + TTL_SECONDS)}).encode())
    return f"{body}.{_sign(body)}"


def verify(token: str | None, now: float | None = None) -> str | None:
    """The user name in a valid, unexpired token; None otherwise."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        username, expires = str(data["u"]), int(data["e"])
    except (ValueError, KeyError, TypeError):
        return None
    return username if expires > (time.time() if now is None else now) else None


async def authenticate(db: AsyncSession, username: str, password: str, totp_code: str | None, ip: str) -> str:
    """Check the credentials without writing anything. Returns the account's user name or raises an API error."""
    wait = throttle.retry_after(ip, username)
    if wait > 0:
        raise api_error(429, ErrorCode.TOO_MANY_LOGIN_ATTEMPTS, wait=wait)
    user = (await db.execute(select(AdminUser).where(AdminUser.username == username))).scalar_one_or_none()
    if user is None:
        await auth_service.verify_password_async(auth_service._DUMMY_HASH, password)  # keep the timing alike
        throttle.record_failure(ip, username)
        raise api_error(401, ErrorCode.UNGUELTIGER_BENUTZERNAME_ODER_PASSWORT)
    if not await auth_service.verify_password_async(user.password_hash, password):
        throttle.record_failure(ip, username)
        raise api_error(401, ErrorCode.UNGUELTIGER_BENUTZERNAME_ODER_PASSWORT)
    # Only revealed after the password was right, so it tells nothing about which names exist.
    if user.auth_source != "local" or user.role != "admin":
        raise api_error(403, ErrorCode.STANDBY_ADMIN_ONLY)
    if user.totp_enabled:
        secret = totp_service.read_secret(user.id)
        if secret is None:   # the secret file has not been synced yet: never let the account in without its second factor
            raise api_error(503, ErrorCode.STANDBY_LOGIN_UNAVAILABLE)
        if not totp_code:
            raise api_error(401, ErrorCode.TOTP_REQUIRED)
        step = totp_service.verify_code(secret, totp_code, last_step=max(user.totp_last_step or 0, _last_step.get(user.id, 0)))
        if step is None:   # recovery codes land here too: spending one is a write
            throttle.record_failure(ip, username)
            raise api_error(401, ErrorCode.TOTP_INVALID)
        _last_step[user.id] = step
    throttle.reset(ip, username)
    return user.username
