"""Destination check for URLs OPNexus is told to call (notification channels, webhook).

Admins legitimately point these at servers on their own network (a self-hosted ntfy, Gotify or
Matrix), so private ranges are allowed. What is refused is what cannot be a notification target:
anything but http(s), credentials inside the URL, and addresses that only ever mean "this machine"
or "the cloud metadata service" (loopback, link-local such as 169.254.169.254, unspecified,
multicast). Redirects are never followed by the callers, so a public host cannot bounce a request
to such an address. The hostname is resolved at send time and every address is checked (a name that
does not resolve is let through; the request then fails on its own).

DNS rebinding (1.29): a check followed by a second, independent resolution inside the HTTP client could be
answered differently ("first public, then 127.0.0.1"). ``guarded_client`` closes that window: its transport
resolves the name itself when it opens the connection, refuses the connection if ANY answer is forbidden and
connects to exactly the address it checked. Host header, SNI and certificate verification keep using the name.
The up-front ``ensure_resolved_safe`` stays as the early, friendly error.

How: httpcore (the layer under httpx) accepts a pluggable network backend. httpx does not expose it, so
``GuardedTransport`` replaces the backend of the pool its parent class built (``_pool._network_backend``, httpx
0.28 / httpcore 1.0). If those internals change, constructing the transport raises instead of silently
unguarding; tests/test_url_guard.py opens a real socket against it, so an upgrade that breaks the hook fails the
suite. The smaller alternative (rewriting the request URL to the checked IP with an ``sni_hostname`` extension)
was rejected: it changes the URL every caller, log line and mock sees.
"""
import asyncio
import contextlib
import contextvars
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import httpcore
import httpx

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


# Cloud metadata services outside the link-local range (AWS IPv6 IMDS, Alibaba Cloud).
_METADATA_ADDRESSES = {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}


def _forbidden(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip in _METADATA_ADDRESSES


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


async def resolve_host(host: str, port: int | None = None) -> list[str]:
    """Every address the name currently resolves to (a literal answers itself). Raises OSError."""
    host = host.strip("[]").split("%")[0]
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        address = info[4][0].split("%")[0]
        if address not in seen:
            seen.append(address)
    return seen


def _any_forbidden(addresses: list[str]) -> bool:
    for address in addresses:
        try:
            if _forbidden(ipaddress.ip_address(address)):
                return True
        except ValueError:
            continue
    return False


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
        addresses = await resolve_host(host)
    except OSError:
        return
    if _any_forbidden(addresses):
        raise ValueError("Der Name zeigt auf eine nicht erlaubte Adresse (lokal, Link-Local oder Metadaten-Dienst)")


async def checked_addresses(host: str, port: int | None = None) -> list[str]:
    """Addresses a non-HTTP target (syslog) may be connected to, all of them checked. Raises ValueError for a
    forbidden target and OSError when the name does not resolve. The caller connects to these addresses, not to the
    name, so a second answer from DNS cannot change the destination."""
    bare = (host or "").strip().strip("[]")
    if not bare or bare.lower() in ("localhost", "localhost.localdomain"):
        raise ValueError("Diese Adresse (lokal, Link-Local oder Metadaten-Dienst) ist als Ziel nicht erlaubt")
    addresses = await resolve_host(bare, port)
    if not addresses or _any_forbidden(addresses):
        raise ValueError("Diese Adresse (lokal, Link-Local oder Metadaten-Dienst) ist als Ziel nicht erlaubt")
    return addresses


# ── connection-time guard (DNS rebinding) ─────────────────────────────────

BLOCKED_MESSAGE = "Zieladresse nicht erlaubt"


class BlockedAddress(httpcore.ConnectError):
    """The name resolved to a forbidden address when the connection was opened (httpx reports ConnectError)."""


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend):
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await asyncio.wait_for(resolve_host(host, port), timeout)
        except asyncio.TimeoutError:
            raise httpcore.ConnectTimeout("name resolution timed out") from None
        except OSError as exc:
            raise httpcore.ConnectError(f"name resolution failed ({type(exc).__name__})") from None
        if not addresses or _any_forbidden(addresses):
            raise BlockedAddress(BLOCKED_MESSAGE)
        last: Exception | None = None
        for address in addresses:  # the checked addresses only, in resolver order
            try:
                return await self._inner.connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last  # type: ignore[misc]

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        pool = getattr(self, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if not isinstance(backend, httpcore.AsyncNetworkBackend):
            raise RuntimeError("httpx/httpcore internals changed: the connection guard cannot be installed")
        pool._network_backend = _GuardedBackend(backend)


def guarded_client(**kwargs) -> httpx.AsyncClient:
    """An httpx client whose connections only ever go to addresses that passed the check. Environment proxies are
    ignored (a proxy would resolve the name itself, where the check cannot see it)."""
    return httpx.AsyncClient(transport=GuardedTransport(), trust_env=False, **kwargs)


def failure_reason(exc: httpx.HTTPError) -> str:
    """Short, URL-free reason for a failed request (the URL often is the credential)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc.__cause__, BlockedAddress):
        return BLOCKED_MESSAGE
    return type(exc).__name__
