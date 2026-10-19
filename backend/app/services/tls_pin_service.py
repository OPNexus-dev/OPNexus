"""Certificate pinning for the connection to each OPNsense firewall.

Firewalls usually carry a self-signed certificate whose name does not match
the address OPNexus uses (an IP, or a name that only exists in someone's
head), so ordinary hostname verification can never pass. Pinning replaces it
with something stricter: the operator confirms the certificate's SHA-256
fingerprint once, OPNexus stores that certificate (or the CA that signed it)
and from then on trusts *only* it. The connection works the same whether the
firewall is addressed by IP or by name, and a man-in-the-middle presenting any
other certificate is rejected before credentials are sent.
"""
import asyncio
import ssl
from datetime import datetime, timezone
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtensionOID, NameOID

from app.services import overlay_service, proxy_connect

MAX_PEM_BYTES = 32 * 1024


class TlsPinError(Exception):
    pass


def _describe(cert: x509.Certificate) -> dict:
    def common_name(name: x509.Name) -> str:
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return attrs[0].value if attrs else name.rfc4514_string()

    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        names = [str(v.value) for v in san]
    except x509.ExtensionNotFound:
        names = []
    return {
        "sha256": ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256())),
        "subject": common_name(cert.subject),
        "issuer": common_name(cert.issuer),
        "self_signed": cert.subject == cert.issuer,
        "not_before": cert.not_valid_before_utc,
        "not_after": cert.not_valid_after_utc,
        "san": names,
    }


def parse_pem(pem: str) -> list[dict]:
    """Validate a PEM bundle and describe every certificate in it."""
    data = pem.encode()
    if len(data) > MAX_PEM_BYTES:
        raise TlsPinError("PEM zu groß")
    try:
        certs = x509.load_pem_x509_certificates(data)
    except ValueError as exc:
        raise TlsPinError("Kein gültiges PEM-Zertifikat") from exc
    if not certs:
        raise TlsPinError("Kein Zertifikat gefunden")
    return [_describe(c) for c in certs]


def fingerprint_of(pem: str | None) -> str | None:
    if not pem:
        return None
    try:
        return parse_pem(pem)[0]["sha256"]
    except TlsPinError:
        return None


def pinned_context(pem: str) -> ssl.SSLContext:
    """Trust exactly the pinned certificate(s). The hostname is deliberately not
    checked: the pin itself identifies the peer, and the same firewall must be
    reachable by IP and by name."""
    ctx = ssl.create_default_context(cadata=pem)
    ctx.check_hostname = False
    # OPNsense's self-signed certs often lack the extensions strict mode wants.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return ctx


def verify_argument(verify_tls: bool, tls_ca_pem: str | None):
    """Value for httpx's `verify=`: False (unchecked), True (system CAs) or a pinned context."""
    if not verify_tls:
        return False
    if tls_ca_pem:
        return pinned_context(tls_ca_pem)
    return True


async def _open_tls(base_url: str, parsed, ctx: ssl.SSLContext, timeout: float):
    """TLS connection to the firewall: directly, or through the overlay hub's proxy for overlay addresses."""
    proxy = overlay_service.proxy_for(base_url)
    port = parsed.port or 443
    if proxy is None:
        return await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, port, ssl=ctx, server_hostname=parsed.hostname), timeout
        )
    reader, writer = await proxy_connect.open_tunnel(proxy, parsed.hostname, port, timeout)
    try:
        await asyncio.wait_for(writer.start_tls(ctx, server_hostname=parsed.hostname), timeout)
    except Exception:
        writer.close()
        raise
    return reader, writer


async def probe(base_url: str, timeout: float = 8.0) -> dict:
    """Fetch the certificate a firewall presents, *without* trusting it. The
    caller must show the fingerprint to a human to compare with the firewall."""
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise TlsPinError("Nur https-Adressen können festgesetzt werden")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await _open_tls(base_url, parsed, ctx, timeout)
    except (OSError, ssl.SSLError, asyncio.TimeoutError) as exc:
        raise TlsPinError(f"Verbindung fehlgeschlagen: {exc}") from exc
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    cert = x509.load_der_x509_certificate(der)
    info = _describe(cert)
    info["pem"] = cert.public_bytes(serialization.Encoding.PEM).decode()
    info["expired"] = info["not_after"] < datetime.now(timezone.utc)
    return info


async def verify_matches(base_url: str, pem: str, timeout: float = 8.0) -> None:
    """Handshake against the pinned context; raises TlsPinError if the firewall
    presents anything else."""
    parsed = urlparse(base_url)
    try:
        _, writer = await _open_tls(base_url, parsed, pinned_context(pem), timeout)
        writer.close()
    except ssl.SSLError as exc:
        raise TlsPinError("Das Zertifikat der Firewall passt nicht zum festgesetzten Zertifikat") from exc
    except (OSError, asyncio.TimeoutError) as exc:
        raise TlsPinError(f"Verbindung fehlgeschlagen: {exc}") from exc
