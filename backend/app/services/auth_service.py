"""Authentication core: password hashing, server-side sessions, brute-force
protection and the initial admin bootstrap.

Two roles ("admin", "read_only"), no orgs, no API keys — see
AdminUser/AuthMiddleware for how a role is enforced. Originally mirrored
InfraPanel's own app/services/auth_service.py (single-admin-only); the role
column and its enforcement are OPNexus-specific additions on top of that.

Design notes
------------
* Passwords are hashed with Argon2id (argon2-cffi). On every successful
  login we transparently re-hash if the cost parameters changed.
* Sessions are stored server-side so they can be revoked. The database only
  ever holds the SHA-256 of the random session token; the raw token lives
  solely in the client's httpOnly cookie. A DB leak therefore cannot be
  turned into a valid session.
* Login attempts are rate-limited per (ip, username) with a temporary
  lockout to blunt online brute-force / credential-stuffing.
* Unknown usernames still trigger a dummy verify so response timing does
  not reveal whether the account exists.
"""

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import AdminUser, Session

logger = logging.getLogger(__name__)

_ph = PasswordHasher()

# Pre-computed hash of a random string, used to equalise timing for logins
# against a non-existent username (defends against username enumeration).
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(32))

SESSION_TOKEN_BYTES = 32


# ── Password hashing ─────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _ph.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


# ── Brute-force protection (in-memory, per process) ──────────────────────

@dataclass
class _AttemptRecord:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class _LoginThrottle:
    """Failed-login limiter with two independent buckets per window:

    * ``ip::username`` — targeted guessing against the one account
    * ``ip`` — spraying across many usernames from one source

    A login is blocked while *either* bucket is locked out.
    """

    def __init__(self) -> None:
        self._records: dict[str, _AttemptRecord] = {}
        # Total failures per username since their last successful login —
        # informational only (shown to the user after login), separate from
        # the rolling lockout windows above. In-memory/per-process like the
        # rest of this throttle, so it resets on a backend restart.
        self._failures_since_success: dict[str, int] = {}

    @staticmethod
    def _user_key(ip: str, username: str) -> str:
        return f"u:{ip}::{username.lower()}"

    @staticmethod
    def _ip_key(ip: str) -> str:
        return f"i:{ip}"

    def _retry_after_key(self, key: str) -> int:
        rec = self._records.get(key)
        if not rec:
            return 0
        remaining = rec.locked_until - time.monotonic()
        return int(remaining) + 1 if remaining > 0 else 0

    def _record_key(self, key: str, max_attempts: int) -> None:
        now = time.monotonic()
        rec = self._records.setdefault(key, _AttemptRecord())
        window = settings.login_window_seconds
        rec.failures = [t for t in rec.failures if now - t < window]
        rec.failures.append(now)
        if len(rec.failures) >= max_attempts:
            rec.locked_until = now + settings.login_lockout_seconds
            rec.failures.clear()

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds the caller must wait, or 0 if not currently locked out."""
        return max(
            self._retry_after_key(self._user_key(ip, username)),
            self._retry_after_key(self._ip_key(ip)),
        )

    def record_failure(self, ip: str, username: str) -> None:
        self._record_key(self._user_key(ip, username), settings.login_max_attempts)
        self._record_key(self._ip_key(ip), settings.login_ip_max_attempts)
        key = username.lower()
        self._failures_since_success[key] = self._failures_since_success.get(key, 0) + 1

    def reset(self, ip: str, username: str) -> None:
        self._records.pop(self._user_key(ip, username), None)

    def pop_failures_since_success(self, username: str) -> int:
        """Read and clear the failure count for a username, called on a
        successful login so the count reflects only attempts since then."""
        return self._failures_since_success.pop(username.lower(), 0)


throttle = _LoginThrottle()


# ── Sessions ───────────────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_session(
    db: AsyncSession,
    user: AdminUser,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, datetime]:
    """Create a session, returning the raw token (to be set as a cookie) and its expiry."""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    expires_at = _now() + timedelta(seconds=settings.session_lifetime)
    session = Session(
        user_id=user.id,
        token_hash=_hash_token(token),
        expires_at=expires_at,
        last_used_at=_now(),
        user_agent=(user_agent or "")[:255] or None,
        ip_address=ip_address,
    )
    db.add(session)
    await db.execute(delete(Session).where(Session.user_id == user.id, Session.expires_at < _now()))
    await db.commit()
    return token, expires_at


async def resolve_session(db: AsyncSession, token: str) -> AdminUser | None:
    if not token:
        return None
    result = await db.execute(select(Session).where(Session.token_hash == _hash_token(token)))
    session = result.scalar_one_or_none()
    if session is None or session.expires_at <= _now():
        return None
    return await db.get(AdminUser, session.user_id)


async def revoke_session(db: AsyncSession, token: str) -> None:
    if not token:
        return
    await db.execute(delete(Session).where(Session.token_hash == _hash_token(token)))
    await db.commit()


# ── Authentication ────────────────────────────────────────────────────

async def authenticate(
    db: AsyncSession, username: str, password: str, ip: str, *, finalize: bool = True
) -> AdminUser | None:
    """Verify credentials. Returns the user on success, else None.

    Performs a dummy verify on an unknown username to keep timing roughly
    constant, and transparently upgrades the stored hash if Argon2
    parameters changed. On success, stashes the *previous* login
    timestamp/IP as transient (non-persisted) attributes on the returned
    user before overwriting them, so the caller can show "last login was
    ..." — the mapped columns always hold the current login after this.
    """
    result = await db.execute(select(AdminUser).where(AdminUser.username == username))
    user = result.scalar_one_or_none()

    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None

    if not verify_password(user.password_hash, password):
        return None

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    if finalize:
        await finalize_login(db, user, ip)
    else:
        await db.commit()
    return user


async def finalize_login(db: AsyncSession, user: AdminUser, ip: str) -> None:
    """Record the login (and expose the previous one). Split from authenticate()
    so a user with a second factor is only stamped once that factor passed."""
    user.previous_login_at = user.last_login_at  # type: ignore[attr-defined]
    user.previous_login_ip = user.last_login_ip  # type: ignore[attr-defined]
    user.last_login_at = _now()
    user.last_login_ip = ip
    await db.commit()


# ── Bootstrap ─────────────────────────────────────────────────────────

async def bootstrap_admin(db: AsyncSession) -> None:
    """Ensure the admin account exists."""
    existing = await db.execute(select(AdminUser.id).limit(1))
    if existing.first() is not None:
        return

    password = settings.admin_password.strip()
    generated = False
    if not password:
        password = secrets.token_urlsafe(18)
        generated = True
    elif len(password) < 12:
        logger.warning(
            "ADMIN_PASSWORD is shorter than 12 characters — please choose a "
            "stronger password."
        )

    user = AdminUser(username=settings.admin_username, password_hash=hash_password(password), role="admin")
    db.add(user)
    await db.commit()

    if generated:
        logger.warning(
            "\n%s\n  No ADMIN_PASSWORD set — created admin user '%s' with a\n"
            "  generated password:\n\n      %s\n\n"
            "  Log in and change it now. This is shown only once.\n%s",
            "=" * 64, settings.admin_username, password, "=" * 64,
        )
    else:
        logger.info("Created admin user '%s' from configuration.", settings.admin_username)
