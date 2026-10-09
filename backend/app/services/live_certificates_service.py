"""Fetches the TLS certificates actually stored on a host right now, straight
from OPNsense's trust/cert/search — read-only, no persistence, mirrors
live_rules_service.py.

Response shape verified against the OPNsense core source (Trust/Api/
CertController.php, Trust/Store.php::parseX509()): {"total": N, "rowCount":
N, "current": 1, "rows": [...]}, each row carrying valid_from/valid_to as
Unix timestamps and a %caref label (the locally-managed issuing CA's name,
empty for self-signed or externally-imported certs). CSR-pending entries
have no parsed X.509 fields at all (valid_from/valid_to/commonname empty)
and are surfaced as status "unknown" rather than raised as an error.
"""
from datetime import datetime, timezone

from app.models.host import OpnsenseHost
from app.schemas.host import CertStatus, HostCa, HostCertificate
from app.services import opnsense_client

EXPIRING_SOON_DAYS = 30


def _parse_epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _expiry(valid_to: datetime | None) -> tuple[int | None, CertStatus]:
    if valid_to is None:
        return None, "unknown"
    days_until_expiry = (valid_to - datetime.now(timezone.utc)).days
    if days_until_expiry < 0:
        return days_until_expiry, "expired"
    if days_until_expiry <= EXPIRING_SOON_DAYS:
        return days_until_expiry, "expiring_soon"
    return days_until_expiry, "valid"


def _row_to_certificate(row: dict) -> HostCertificate:
    valid_from = _parse_epoch(row.get("valid_from"))
    valid_to = _parse_epoch(row.get("valid_to"))
    days_until_expiry, status = _expiry(valid_to)

    return HostCertificate(
        uuid=row.get("uuid", ""),
        refid=row.get("refid", ""),
        descr=row.get("descr") or "(ohne Beschreibung)",
        common_name=row.get("commonname") or None,
        issuer=row.get("%caref") or "Selbstsigniert",
        valid_from=valid_from,
        valid_to=valid_to,
        days_until_expiry=days_until_expiry,
        status=status,
        in_use=row.get("in_use") in ("1", 1, True),
    )


async def get_live_certificates(host: OpnsenseHost) -> list[HostCertificate]:
    data = await opnsense_client.search_certificates(host)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [_row_to_certificate(row) for row in rows]


def _row_to_ca(row: dict) -> HostCa:
    valid_from = _parse_epoch(row.get("valid_from"))
    valid_to = _parse_epoch(row.get("valid_to"))
    days_until_expiry, status = _expiry(valid_to)

    return HostCa(
        uuid=row.get("uuid", ""),
        refid=row.get("refid", ""),
        descr=row.get("descr") or "(ohne Beschreibung)",
        common_name=row.get("commonname") or None,
        valid_from=valid_from,
        valid_to=valid_to,
        days_until_expiry=days_until_expiry,
        status=status,
    )


async def get_live_cas(host: OpnsenseHost) -> list[HostCa]:
    data = await opnsense_client.search_ca(host)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [_row_to_ca(row) for row in rows]
