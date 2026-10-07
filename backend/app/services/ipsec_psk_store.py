"""Per-tunnel IPsec pre-shared-key storage.

Mirrors opnsense_secret_store.py's convention exactly (itself mirroring
InfraPanel's proxmox_connection.py / webhook_secret_store.py): the PSK
secret never touches the database, only disk, one file per tunnel id,
mode 0600. IpsecTunnel.pre_shared_key_set is the DB-side flag tracking
whether a file exists, the same pattern used by NotificationSettings for
its webhook signing secret / SMTP password.
"""
import uuid
from pathlib import Path

from app.config import settings

_SUFFIX = ".psk"


def _path(tunnel_id: uuid.UUID) -> Path:
    return Path(settings.ipsec_psk_secret_dir) / f"{tunnel_id}{_SUFFIX}"


def save_secret(tunnel_id: uuid.UUID, secret: str) -> None:
    path = _path(tunnel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.strip())
    path.chmod(0o600)


def read_secret(tunnel_id: uuid.UUID) -> str | None:
    path = _path(tunnel_id)
    if not path.exists():
        return None
    return path.read_text().strip()


def delete_secret(tunnel_id: uuid.UUID) -> None:
    _path(tunnel_id).unlink(missing_ok=True)
