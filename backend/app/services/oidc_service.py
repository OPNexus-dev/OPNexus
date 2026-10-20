"""OpenID Connect login (authorization code flow with PKCE) and role mapping.

What is checked on the ID token, in this order of suspicion: the signature (against the provider's JWKS,
asymmetric algorithms only: ``none`` and HMAC are never accepted), ``iss`` equal to the configured issuer,
``aud`` containing our client id (and ``azp`` equal to it if there are several audiences), ``exp``/``nbf``,
and the ``nonce`` we sent. The role comes from the groups claim (ID token, else userinfo) at EVERY login, so
removing someone from the group takes the access away at their next sign-in. Local accounts are never linked
to or taken over by an identity: an OIDC user is keyed by ``<issuer>|<sub>`` and a name clash with a local
account gets a suffix. Without a mapped group nobody is let in.
"""
import base64
import hashlib
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWK
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oidc import OidcLoginState, OidcSettings
from app.models.user import AdminUser
from app.services import notification_secret_store

logger = logging.getLogger(__name__)

SINGLETON_ID = 1
SECRET_NAME = "oidc_client_secret"
STATE_TTL = timedelta(minutes=10)
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
UNUSABLE_PASSWORD = "!oidc"
_ROLE_ORDER = ("admin", "operator", "read_only")


class OidcError(Exception):
    """Carries a short, user-safe code (shown on the login page), never provider internals."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


async def get_settings(db: AsyncSession) -> OidcSettings:
    row = await db.get(OidcSettings, SINGLETON_ID)
    if row is None:
        row = OidcSettings(
            id=SINGLETON_ID, enabled=False, display_name="SSO", client_secret_set=False,
            scopes="openid profile email", groups_claim="groups",
        )
        db.add(row)
        await db.flush()
    return row


def configured(cfg: OidcSettings) -> bool:
    return bool(cfg.enabled and cfg.issuer and cfg.client_id and (cfg.admin_group or cfg.operator_group or cfg.read_only_group))


# ── provider metadata (cached) ───────────────────────────────────────────

_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}
_DISCOVERY_TTL, _JWKS_TTL, _JWKS_MIN_REFETCH = 600, 3600, 60


def clear_caches() -> None:
    _discovery_cache.clear()
    _jwks_cache.clear()


async def _get_json(url: str, **kw) -> dict:
    if not url.startswith("https://"):
        raise OidcError("provider", "Nur https:// wird akzeptiert")
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(url, **kw)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        raise OidcError("provider", "Der Identitätsanbieter ist nicht erreichbar oder antwortet unverständlich") from None
    if not isinstance(data, dict):
        raise OidcError("provider", "Unerwartete Antwort des Identitätsanbieters")
    return data


async def discovery(issuer: str) -> dict:
    cached = _discovery_cache.get(issuer)
    if cached and time.monotonic() - cached[0] < _DISCOVERY_TTL:
        return cached[1]
    doc = await _get_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if doc.get("issuer") != issuer and doc.get("issuer", "").rstrip("/") != issuer.rstrip("/"):
        raise OidcError("provider", "Der Aussteller in der Discovery-Antwort passt nicht zur eingetragenen URL")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not str(doc.get(key, "")).startswith("https://"):
            raise OidcError("provider", f"Discovery enthält kein gültiges {key}")
    _discovery_cache[issuer] = (time.monotonic(), doc)
    return doc


async def _jwk_for(doc: dict, kid: str | None) -> PyJWK:
    uri = doc["jwks_uri"]
    for attempt in (0, 1):
        cached = _jwks_cache.get(uri)
        stale = cached is None or time.monotonic() - cached[0] > _JWKS_TTL
        if stale or attempt == 1:
            if attempt == 1 and cached and time.monotonic() - cached[0] < _JWKS_MIN_REFETCH:
                break  # unknown kid, but we just fetched: do not let a forged kid hammer the provider
            _jwks_cache[uri] = (time.monotonic(), await _get_json(uri))
        for key in _jwks_cache[uri][1].get("keys", []):
            if (kid is None or key.get("kid") == kid) and key.get("use", "sig") == "sig":
                try:
                    return PyJWK(key)
                except Exception:  # unsupported key type: try the next one
                    continue
    raise OidcError("token", "Kein passender Signaturschlüssel")


# ── the flow ─────────────────────────────────────────────────────────────

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def start(db: AsyncSession, cfg: OidcSettings, redirect_uri: str) -> tuple[str, str]:
    """Remember a fresh login attempt and return the provider URL to send the browser to."""
    doc = await discovery(cfg.issuer)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    await db.execute(delete(OidcLoginState).where(OidcLoginState.created_at < datetime.now(timezone.utc) - STATE_TTL))
    db.add(OidcLoginState(state_hash=_hash(state), nonce=nonce, code_verifier=verifier, redirect_uri=redirect_uri))
    await db.commit()
    query = urlencode({
        "response_type": "code", "client_id": cfg.client_id, "redirect_uri": redirect_uri, "scope": cfg.scopes,
        "state": state, "nonce": nonce, "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
    })
    return f"{doc['authorization_endpoint']}{'&' if '?' in doc['authorization_endpoint'] else '?'}{query}", state


async def _consume_state(db: AsyncSession, state: str) -> OidcLoginState:
    row = await db.scalar(select(OidcLoginState).where(OidcLoginState.state_hash == _hash(state)).with_for_update())
    if row is None:
        raise OidcError("state", "Unbekannter oder bereits benutzter Anmeldeversuch")
    created = row.created_at
    await db.delete(row)  # single use, whatever happens next
    await db.commit()
    if datetime.now(timezone.utc) - created > STATE_TTL:
        raise OidcError("state", "Der Anmeldeversuch ist abgelaufen")
    return row


async def _exchange(cfg: OidcSettings, doc: dict, code: str, state: OidcLoginState) -> dict:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": state.redirect_uri,
            "client_id": cfg.client_id, "code_verifier": state.code_verifier}
    secret = notification_secret_store.read_secret(SECRET_NAME) if cfg.client_secret_set else None
    auth = None
    if secret:
        methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if "client_secret_basic" in methods:
            auth = httpx.BasicAuth(cfg.client_id, secret)
        else:
            data["client_secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.post(doc["token_endpoint"], data=data, auth=auth)
        if response.status_code >= 400:
            raise OidcError("token", f"Token-Endpunkt antwortet mit HTTP {response.status_code}")
        tokens = response.json()
    except httpx.HTTPError:
        raise OidcError("provider", "Token-Endpunkt nicht erreichbar") from None
    except ValueError:
        raise OidcError("token", "Unverständliche Token-Antwort") from None
    if not isinstance(tokens, dict) or not tokens.get("id_token"):
        raise OidcError("token", "Die Antwort enthält kein ID-Token")
    return tokens


async def _verify_id_token(cfg: OidcSettings, doc: dict, id_token: str, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise OidcError("token", "ID-Token unlesbar") from None
    if header.get("alg") not in ALGORITHMS:
        raise OidcError("token", f"Nicht erlaubter Signaturalgorithmus {header.get('alg')!r}")
    key = await _jwk_for(doc, header.get("kid"))
    try:
        claims = jwt.decode(
            id_token, key.key, algorithms=ALGORITHMS, audience=cfg.client_id, issuer=cfg.issuer,
            options={"require": ["exp", "iss", "aud", "sub"]}, leeway=60,
        )
    except jwt.PyJWTError as exc:
        raise OidcError("token", f"ID-Token ungültig ({type(exc).__name__})") from None
    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != cfg.client_id:
        raise OidcError("token", "azp passt nicht zur Client-ID")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError("token", "Nonce stimmt nicht")
    return claims


def _as_groups(value) -> list[str]:
    if isinstance(value, str):
        # Group names may contain spaces ("Network Admins"): a string claim is split on commas and line breaks only.
        return [g.strip() for g in re.split(r"[,\n]+", value) if g.strip()]
    if isinstance(value, list):
        return [str(g) for g in value]
    return []


def role_for(cfg: OidcSettings, groups: list[str]) -> str | None:
    """The most privileged role whose group the user is in; None = not allowed in."""
    wanted = {"admin": cfg.admin_group, "operator": cfg.operator_group, "read_only": cfg.read_only_group}
    have = {g.lower() for g in groups}
    for role in _ROLE_ORDER:
        group = wanted[role]
        if group and group.strip().lower() in have:
            return role
    return None


async def _userinfo(doc: dict, claims: dict, access_token: str | None) -> dict:
    """The provider's userinfo answer for THIS identity ({} if there is none or it belongs to somebody else)."""
    if access_token and str(doc.get("userinfo_endpoint", "")).startswith("https://"):
        info = await _get_json(doc["userinfo_endpoint"], headers={"Authorization": f"Bearer {access_token}"})
        if info.get("sub") == claims.get("sub"):  # a userinfo answer for someone else is worthless
            return info
    return {}


async def _groups(cfg: OidcSettings, doc: dict, claims: dict, access_token: str | None) -> list[str]:
    if cfg.groups_claim in claims:
        return _as_groups(claims[cfg.groups_claim])
    return _as_groups((await _userinfo(doc, claims, access_token)).get(cfg.groups_claim))


async def _with_display_name(doc: dict, claims: dict, access_token: str | None) -> dict:
    """Some providers (Authelia) keep preferred_username/email out of the ID token: ask userinfo, else the user would be named
    after the opaque ``sub``. Best effort: a failing userinfo call must not block a login that already has its role."""
    if claims.get("preferred_username") or claims.get("email"):
        return claims
    try:
        info = await _userinfo(doc, claims, access_token)
    except OidcError:
        return claims
    return {**claims, **{k: info[k] for k in ("preferred_username", "email") if info.get(k)}}


def _username(claims: dict) -> str:
    raw = claims.get("preferred_username") or claims.get("email") or claims["sub"]
    return re.sub(r"[\x00-\x1f]", "", str(raw)).strip()[:140] or "sso-user"


async def provision(db: AsyncSession, cfg: OidcSettings, claims: dict, role: str) -> AdminUser:
    """The account for this identity: created on first login, role synced on every login."""
    subject = f"{cfg.issuer}|{claims['sub']}"[:600]
    user = await db.scalar(select(AdminUser).where(AdminUser.oidc_subject == subject))
    if user is None:
        base, name, n = _username(claims), None, 0
        while True:
            name = base if n == 0 else f"{base}~sso{n if n > 1 else ''}"
            if await db.scalar(select(AdminUser.id).where(AdminUser.username == name)) is None:
                break
            n += 1
        user = AdminUser(username=name, password_hash=UNUSABLE_PASSWORD, role=role, auth_source="oidc", oidc_subject=subject)
        db.add(user)
    else:
        user.role = role
    await db.commit()
    return user


async def complete(db: AsyncSession, cfg: OidcSettings, code: str, state_value: str) -> AdminUser:
    """Callback: state -> tokens -> verified identity -> role -> account. Raises OidcError."""
    state = await _consume_state(db, state_value)
    doc = await discovery(cfg.issuer)
    tokens = await _exchange(cfg, doc, code, state)
    claims = await _verify_id_token(cfg, doc, tokens["id_token"], state.nonce)
    role = role_for(cfg, await _groups(cfg, doc, claims, tokens.get("access_token")))
    if role is None:
        raise OidcError("no_role", "Kein zugeordnete Gruppe")
    return await provision(db, cfg, await _with_display_name(doc, claims, tokens.get("access_token")), role)
