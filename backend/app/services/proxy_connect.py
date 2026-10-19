"""HTTP CONNECT through the overlay hub's proxy, for the few places that open a raw TLS connection."""
import asyncio
from urllib.parse import urlsplit


async def open_tunnel(proxy_url: str, host: str, port: int, timeout: float = 8.0):
    """(reader, writer) connected to host:port through the proxy; TLS can be started on top with ``writer.start_tls``."""
    proxy = urlsplit(proxy_url)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(proxy.hostname, proxy.port or 8888), timeout)
    try:
        writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        status = int(head.split(b" ", 2)[1])
        if status != 200:
            raise OSError(f"Der Overlay-Proxy lehnt die Verbindung ab (HTTP {status})")
    except Exception:
        writer.close()
        raise
    return reader, writer
