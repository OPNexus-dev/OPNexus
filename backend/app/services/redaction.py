"""Scrub text that came back from a firewall before it is stored, logged or
shown to a user.

An upstream error body can echo the request that caused it — including the
password, pre-shared key or private key we just sent — or contain secret-bearing
configuration. OPNexus keeps these messages (deployment status, maintenance
results, host status detail) and read-only users can see them, so they must not
carry such values. Redaction is applied where the exception is built
(opnsense_client), so every consumer gets the safe version.
"""
import json
import re
from typing import Any

MAX_LENGTH = 300
_REDACTED = "[redacted]"

# Field names whose value is (or may be) secret material.
_SECRET_KEY = re.compile(
    r"pass|secret|psk|pre_?shared|private|prv|token|api_?key|apikey|credential|auth|crt|cert|\bkey\b|_key|key_|tls_?key",
    re.IGNORECASE,
)
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|$)", re.DOTALL)
# key=value / key: value pairs in non-JSON text
_PAIR = re.compile(r"(?i)\b([\w.\-]*(?:pass|secret|psk|token|private|api_?key|key)[\w.\-]*)\b([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)")


def _scrub_json(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _scrub_json(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json(v, key) for v in value]
    if isinstance(value, str) and key and _SECRET_KEY.search(key):
        return _REDACTED
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def _scrub_text(text: str) -> str:
    return _PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", _PEM.sub(_REDACTED, text))


def redact_text(text: str, *known_secrets: str | None) -> str:
    """Return a bounded, secret-free rendering of an upstream response body."""
    text = text or ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        text = json.dumps(_scrub_json(parsed), ensure_ascii=False)
    else:
        text = _scrub_text(text)
    for secret in known_secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    text = " ".join(text.split())
    return text if len(text) <= MAX_LENGTH else text[:MAX_LENGTH] + "…"


def redact_data(data: Any, *known_secrets: str | None) -> str:
    """Same for an already-parsed JSON structure."""
    return redact_text(json.dumps(data, ensure_ascii=False, default=str), *known_secrets)
