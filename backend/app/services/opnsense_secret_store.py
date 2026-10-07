"""Per-host OPNsense API secret storage.

Mirrors InfraPanel's proxmox_connection.py / webhook_secret_store.py
convention: the secret never touches the database, only disk, one file per
host id, mode 0600.
"""
import uuid
from pathlib import Path

from app.config import settings

_SUFFIX = ".secret"


def _path(host_id: uuid.UUID) -> Path:
    return Path(settings.opnsense_secret_dir) / f"{host_id}{_SUFFIX}"


def save_secret(host_id: uuid.UUID, secret: str) -> None:
    path = _path(host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.strip())
    path.chmod(0o600)


def read_secret(host_id: uuid.UUID) -> str | None:
    path = _path(host_id)
    if not path.exists():
        return None
    return path.read_text().strip()


def delete_secret(host_id: uuid.UUID) -> None:
    _path(host_id).unlink(missing_ok=True)
