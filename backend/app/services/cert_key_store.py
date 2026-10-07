"""Per-certificate private-key storage for ManagedCertificate.

Mirrors ipsec_psk_store.py's convention exactly: the key material never
touches the database, only disk, one file per entity id, mode 0600.
ManagedCertificate.private_key_set is the DB-side flag tracking whether a
file exists. Only relevant for `action="import"` (the one action where the
operator supplies their own key material — every other action either has
no private key at all (external/sign_csr/import_csr/manual) or has
OPNsense itself generate and store the key server-side (internal/reissue,
via `private_key_location="firewall"`, this app's default — see
managed_certificate_service.py)).

UNLIKE ipsec_psk_store.py/openvpn_key_store.py/wireguard_key_store.py's
documented (pre-existing) wrinkle of embedding secret material into
`last_payload` for no-op-push-detection, this key is deliberately NEVER
included in ManagedCertificate.last_payload — the plan is explicit that
private key material must never touch Postgres, full stop, with no
"matched here for consistency" carve-out. no-op detection therefore
compares a `private_key_set` boolean stand-in instead of the real value —
see managed_certificate_service.py's build_payload()."""
import uuid
from pathlib import Path

from app.config import settings
from app.services import secure_files

_SUFFIX = ".certkey"


def _path(cert_id: uuid.UUID) -> Path:
    return Path(settings.cert_key_secret_dir) / f"{cert_id}{_SUFFIX}"


def save_secret(cert_id: uuid.UUID, secret: str) -> None:
    path = _path(cert_id)
    secure_files.write_private(path, secret.strip())


def read_secret(cert_id: uuid.UUID) -> str | None:
    path = _path(cert_id)
    if not path.exists():
        return None
    return path.read_text().strip()


def delete_secret(cert_id: uuid.UUID) -> None:
    _path(cert_id).unlink(missing_ok=True)
