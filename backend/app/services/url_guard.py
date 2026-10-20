"""Destination check for URLs OPNexus is told to call (notification channels, webhook).

Admins legitimately point these at servers on their own network (a self-hosted ntfy, Gotify or
Matrix), so private ranges are allowed. What is refused is what cannot be a notification target:
anything but http(s), credentials inside the URL, and addresses that only ever mean "this machine"
or "the cloud metadata service" (loopback, link-local such as 169.254.169.254, unspecified,
multicast). Redirects are never followed by the callers, so a public host cannot bounce a request
to such an address. The hostname is resolved at send time and every address is checked (a name that
does not resolve is let through; the request then fails on its own).
"""
import asyncio
import contextlib
import contextvars
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

# httpx logs every request line ("HTTP Request: POST https://api.telegram.org/bot<token>/sendMessage"). For
# targets whose URL is the credential that line must not reach the logs: callers wrap the request in muted().
_muted = contextvars.ContextVar("url_guard_muted", default=False)


class _MuteFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _muted.get()


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_MuteFilter())


@contextlib.contextmanager
def muted():
    token = _muted.set(True)
    try:
        yield
    finally:
        _muted.reset(token)


def _forbidden(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast


def validate_url(url: str, *, max_length: int = 500) -> str:
    """Returns the cleaned URL or raises ValueError with a short reason (shown to the admin)."""
    value = (url or "").strip()
    if not value or len(value) > max_length or any(c.isspace() for c in value):
        raise ValueError("Die Adresse ist leer, zu lang oder enthält Leerzeichen")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        raise ValueError("Die Adresse ist ungültig") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("Die Adresse muss mit http:// oder https:// beginnen")
    if parts.username or parts.password:
        raise ValueError("Zugangsdaten gehören nicht in die Adresse")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _forbidden(literal):
        raise ValueError("Diese Adresse (lokal, Link-Local oder Metadaten-Dienst) ist als Ziel nicht erlaubt")
    if literal is None and host.lower() in ("localhost", "localhost.localdomain"):
        raise ValueError("Diese Adresse (lokal, Link-Local oder Metadaten-Dienst) ist als Ziel nicht erlaubt")
    return value


async def ensure_resolved_safe(url: str) -> None:
    """Send-time check against DNS answers pointing at forbidden addresses. Raises ValueError."""
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("Die Adresse ist ungültig")
    try:
        ipaddress.ip_address(host)
        return  # a literal was already checked by validate_url
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        if _forbidden(ip):
            raise ValueError("Der Name zeigt auf eine nicht erlaubte Adresse (lokal, Link-Local oder Metadaten-Dienst)")
