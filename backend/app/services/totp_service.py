"""Optional two-factor sign-in (TOTP, RFC 6238) — no extra dependency.

Secrets live on disk (one file per user, mode 0600), never in the database,
following the project's convention for secret material. Recovery codes are
stored only as SHA-256 hashes (they are 50-bit random values, and every guess
is throttled like a password guess). A used time-step is remembered so a
captured code cannot be replayed.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from app.config import settings

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1  # accept the previous and next step to tolerate clock drift
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(value).zfill(DIGITS)


def code_at(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at if at is not None else time.time()) // STEP_SECONDS))


def verify_code(secret: str, code: str, last_step: int = 0, now: float | None = None) -> int | None:
    """Return the matched time-step, or None. Steps <= last_step are refused (replay)."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int((now if now is not None else time.time()) // STEP_SECONDS)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step > last_step and hmac.compare_digest(_hotp(secret, step), code):
            return step
    return None


def otpauth_uri(username: str, secret: str, issuer: str = "OPNexus") -> str:
    label = f"{quote(issuer)}:{quote(username)}"
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"


# ── Recovery codes ────────────────────────────────────────────────────

def _normalize_recovery(code: str) -> str:
    return code.strip().lower().replace(" ", "").replace("-", "")


def hash_recovery(code: str) -> str:
    return hashlib.sha256(_normalize_recovery(code).encode()).hexdigest()


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """Returns (plain codes to show once, hashes to store)."""
    plain = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        plain.append(f"{raw[:5]}-{raw[5:]}")
    return plain, [hash_recovery(c) for c in plain]


def consume_recovery(stored_hashes: list[str] | None, code: str) -> list[str] | None:
    """If `code` matches, return the hash list without it; else None."""
    candidate = hash_recovery(code)
    for stored in stored_hashes or []:
        if hmac.compare_digest(stored, candidate):
            return [h for h in stored_hashes if h != stored]
    return None


# ── Secret files ──────────────────────────────────────────────────────

def _path(user_id: uuid.UUID, pending: bool) -> Path:
    return Path(settings.totp_secret_dir) / f"{user_id}.{'pending' if pending else 'secret'}"


def save_secret(user_id: uuid.UUID, secret: str, pending: bool = False) -> None:
    path = _path(user_id, pending)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret)
    path.chmod(0o600)


def read_secret(user_id: uuid.UUID, pending: bool = False) -> str | None:
    path = _path(user_id, pending)
    return path.read_text().strip() if path.exists() else None


def delete_secrets(user_id: uuid.UUID) -> None:
    _path(user_id, False).unlink(missing_ok=True)
    _path(user_id, True).unlink(missing_ok=True)


def promote_pending(user_id: uuid.UUID) -> None:
    _path(user_id, True).replace(_path(user_id, False))
