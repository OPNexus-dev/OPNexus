"""Policy for the address under which a firewall's API is reached.

The API key/secret travel as HTTP Basic credentials, so a plain ``http://``
address would hand them to anyone on the path. ``verify_tls`` is a different
control (does the certificate get checked) and does not upgrade or reject an
``http://`` URL, hence the scheme is enforced here — when a host is created
and again, for rows that already exist, right before a request is built.
``ALLOW_INSECURE_FIREWALL_URLS=true`` is the explicit, deployment-level
escape hatch for lab setups.
"""
from urllib.parse import urlsplit

from app.config import settings


def check_base_url(value: str) -> str | None:
    """Return a reason (German, user-facing) if the URL must not be used, else None."""
    try:
        parts = urlsplit(value.strip())
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return "Ungültige Adresse"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "Die Adresse muss mit https:// beginnen und einen Host enthalten"
    if parts.scheme == "http" and not settings.allow_insecure_firewall_urls:
        return (
            "Unverschlüsseltes http:// ist nicht erlaubt (API-Zugangsdaten würden im Klartext "
            "übertragen) – bitte https:// verwenden"
        )
    if parts.username is not None or parts.password is not None:
        return "Zugangsdaten gehören nicht in die Adresse"
    if parts.query or parts.fragment:
        return "Die Adresse darf keine Parameter oder Fragmente enthalten"
    return None


def validate_base_url(value: str) -> str:
    reason = check_base_url(value)
    if reason:
        raise ValueError(reason)
    return value
