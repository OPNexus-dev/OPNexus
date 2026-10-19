"""Personal API tokens: creation, lookup and scope rules.

Scopes (a token can never do more than its owner's role allows; a read_only
user's token is read-only whatever its scope says):

* ``read``   — GET/HEAD/OPTIONS only, except endpoints that return secrets (raw
  config backups, notification settings): those need ``admin``. For Prometheus,
  dashboards, MCP clients.
* ``deploy`` — additionally every write that rolls out or operates on the fleet
  (rules, aliases, NAT, VPN, maintenance, backups...). NOT the writes that
  change who can access OPNexus or where it sends data: users, settings,
  notifications, host registration/removal, log settings, OPNexus self-update.
* ``admin``  — everything the owner may do, except what is session-only.

Session-only for every scope: managing tokens themselves and the account
endpoints (/api/auth/*), so a leaked token can neither mint more tokens nor
change a password or second factor.

The enforcement lives in AuthMiddleware (one place, like the read_only role).
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.models.api_token import ApiToken
from app.models.user import AdminUser

TOKEN_PREFIX = "opx_"
SCOPES = ("read", "deploy", "admin")
MAX_TOKENS_PER_USER = 50
# last_used_at is only rewritten when older than this, so a scraper hitting
# /api/metrics every few seconds does not turn into a write per request.
_LAST_USED_GRANULARITY = timedelta(minutes=1)

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_SESSION_ONLY = re.compile(r"^/api/(tokens|auth)(/|$)")
# Reads that expose secrets (config.xml holds password hashes and VPN keys, the
# notification settings the webhook URL/SMTP data): admin scope only.
_ADMIN_SCOPE_READS = re.compile(r"^/api/hosts/[^/]+/backups/[^/]+$|^/api/notifications(/|$)|^/api/settings/")
# The MCP endpoint is POST (JSON-RPC) but its tools only ever read.
_MCP_PATH = "/api/mcp"
_ADMIN_ONLY_WRITES = re.compile(
    r"^/api/(users|settings|system|notifications)(/|$)"
    r"|^/api/logs/settings$"
    r"|^/api/hosts(/tls-probe|/[^/]+)?$"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def denies(scope: str, method: str, path: str) -> bool:
    """True if a token with this scope must not make this request."""
    if _SESSION_ONLY.match(path):
        return True
    if path == _MCP_PATH:
        return False
    if method in _SAFE_METHODS:
        return scope != "admin" and bool(_ADMIN_SCOPE_READS.match(path))
    if scope == "read":
        return True
    if scope == "deploy" and _ADMIN_ONLY_WRITES.match(path):
        return True
    return False


def operator_denies(method: str, path: str) -> bool:
    """True if an `operator` account must not make this request: the writes that
    change who may access OPNexus or where it sends data. (Their own theme at
    PUT /api/settings stays open; account endpoints are not matched here.)"""
    return method not in _SAFE_METHODS and path != "/api/settings" and bool(_ADMIN_ONLY_WRITES.match(path))


def bearer_from_header(header: str) -> str | None:
    """The token of an ``Authorization: Bearer opx_...`` header, else None."""
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() == "bearer" and value.startswith(TOKEN_PREFIX):
        return value
    return None


async def create(
    db: AsyncSession, user: AdminUser, *, name: str, scope: str, expires_in_days: int | None
) -> tuple[ApiToken, str]:
    count = len((await db.scalars(select(ApiToken.id).where(ApiToken.user_id == user.id))).all())
    if count >= MAX_TOKENS_PER_USER:
        raise api_error(409, ErrorCode.API_TOKEN_LIMIT_REACHED, limit=MAX_TOKENS_PER_USER)
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        user_id=user.id,
        name=name,
        scope=scope,
        token_hash=hash_token(raw),
        prefix=raw[: len(TOKEN_PREFIX) + 6],
        expires_at=_now() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return token, raw


async def resolve(db: AsyncSession, raw: str, ip: str | None) -> tuple[AdminUser, ApiToken] | None:
    """The owner and token for a raw bearer token, or None if unknown/expired."""
    token = await db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(raw)))
    if token is None or (token.expires_at is not None and token.expires_at <= _now()):
        return None
    user = await db.get(AdminUser, token.user_id)
    if user is None:
        return None
    if token.last_used_at is None or _now() - token.last_used_at > _LAST_USED_GRANULARITY:
        token.last_used_at = _now()
        token.last_used_ip = ip
        await db.commit()
    return user, token


async def revoke_all(db: AsyncSession, user_id, *, commit: bool = True) -> None:
    """Delete every token of an account (password reset: answer to a suspected compromise)."""
    await db.execute(delete(ApiToken).where(ApiToken.user_id == user_id))
    if commit:
        await db.commit()
