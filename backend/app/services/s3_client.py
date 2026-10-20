"""Minimal S3-compatible object client (AWS Signature V4) for offsite backups.

Just what is needed — PUT, GET, DELETE and a connection test — so no AWS SDK is pulled
into the image. Works with AWS S3, MinIO, Backblaze B2, Wasabi, Cloudflare R2, Hetzner
Object Storage... Path-style addressing (``https://endpoint/bucket/key``) is the default
because it works everywhere; virtual-hosted style is available for providers that need it.
"""
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from app.config import settings


class S3Error(Exception):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _quote(path: str) -> str:
    return quote(path, safe="/-_.~")


def sign(*, method: str, host: str, path: str, query: str, payload_hash: str, access_key: str, secret_key: str,
         region: str, now: datetime, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    """SigV4 headers for one request (the algorithm of the AWS docs, service ``s3``)."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    headers.update({k.lower(): v.strip() for k, v in (extra_headers or {}).items()})
    names = sorted(headers)
    canonical_headers = "".join(f"{n}:{headers[n]}\n" for n in names)
    signed = ";".join(names)
    canonical_request = "\n".join([method, _quote(path), query, canonical_headers, signed, payload_hash])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical_request.encode())])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    return headers


class S3Client:
    def __init__(self, *, endpoint: str, region: str, bucket: str, access_key: str, secret_key: str, path_style: bool = True):
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise S3Error("Ungültiger Endpunkt")
        if parts.scheme == "http" and not settings.allow_insecure_offsite:
            # Credentials and (encrypted, but still) backups must not travel in the clear.
            raise S3Error("Der Endpunkt muss https:// sein")
        # httpx leaves a scheme's default port out of the Host header, so the signed host must not carry it either.
        port = parts.port
        default = {"https": 443, "http": 80}[parts.scheme]
        host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
        self.scheme = parts.scheme
        self.netloc = host if port in (None, default) else f"{host}:{port}"
        self.region, self.bucket, self.path_style = region or "us-east-1", bucket, path_style
        self.access_key, self.secret_key = access_key, secret_key

    def _target(self, key: str) -> tuple[str, str, str]:
        """(host header, URL path, URL)"""
        if self.path_style:
            host, path = self.netloc, f"/{self.bucket}/{key}"
        else:
            host, path = f"{self.bucket}.{self.netloc}", f"/{key}"
        return host, path, f"{self.scheme}://{host}{_quote(path)}"

    async def _request(self, method: str, key: str, body: bytes = b"") -> httpx.Response:
        host, path, url = self._target(key)
        headers = sign(
            method=method, host=host, path=path, query="", payload_hash=_sha256(body), access_key=self.access_key,
            secret_key=self.secret_key, region=self.region, now=datetime.now(timezone.utc),
        )
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                return await client.request(method, url, content=body if method == "PUT" else None, headers=headers)
        except httpx.HTTPError as exc:
            raise S3Error(f"Verbindung fehlgeschlagen ({type(exc).__name__})") from None

    @staticmethod
    def _check(response: httpx.Response, what: str) -> None:
        if response.status_code >= 300:
            raise S3Error(f"{what}: HTTP {response.status_code}")

    async def put(self, key: str, data: bytes) -> None:
        self._check(await self._request("PUT", key, data), "Hochladen")

    async def get(self, key: str) -> bytes:
        response = await self._request("GET", key)
        self._check(response, "Herunterladen")
        return response.content

    async def delete(self, key: str) -> None:
        response = await self._request("DELETE", key)
        if response.status_code not in (200, 204, 404):
            self._check(response, "Löschen")

    async def test(self, key: str) -> None:
        """Write, read back and delete a tiny object: proves credentials, bucket and permissions."""
        probe = b"opnexus connection test"
        await self.put(key, probe)
        if await self.get(key) != probe:
            raise S3Error("Der Speicher lieferte etwas anderes zurück")
        await self.delete(key)
