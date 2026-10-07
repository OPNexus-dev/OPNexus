"""Per-account External-Account-Binding HMAC storage for AcmeAccount.

Mirrors ipsec_psk_store.py/cert_key_store.py's convention exactly: `eab_hmac`
never touches the database, only disk, one file per account id, mode 0600.
AcmeAccount.eab_hmac_set is the DB-side flag tracking whether a file
exists. Same "never appears in last_payload, not even the pre-existing
IPsec/OpenVPN/WireGuard wrinkle" rule as cert_key_store.py — see
AcmeAccount's own docstring."""
import uuid
from pathlib import Path

from app.config import settings
from app.services import secure_files

_SUFFIX = ".eabhmac"


def _path(account_id: uuid.UUID) -> Path:
    return Path(settings.acme_secret_dir) / f"{account_id}{_SUFFIX}"


def save_secret(account_id: uuid.UUID, secret: str) -> None:
    path = _path(account_id)
    secure_files.write_private(path, secret.strip())


def read_secret(account_id: uuid.UUID) -> str | None:
    path = _path(account_id)
    if not path.exists():
        return None
    return path.read_text().strip()


def delete_secret(account_id: uuid.UUID) -> None:
    _path(account_id).unlink(missing_ok=True)
