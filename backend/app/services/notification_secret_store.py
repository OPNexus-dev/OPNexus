"""Notification delivery secrets (SMTP password, webhook signing secret).

Exact mirror of opnsense_secret_store.py's convention: the secret never
touches the database, only disk, mode 0600 — keyed by a fixed name instead
of a host id, since these are singleton secrets (one SMTP config, one
webhook config for the whole install).
"""
from pathlib import Path

from app.config import settings
from app.services import secure_files

_SUFFIX = ".secret"


def _path(name: str) -> Path:
    return Path(settings.notification_secret_dir) / f"{name}{_SUFFIX}"


def save_secret(name: str, secret: str) -> None:
    path = _path(name)
    secure_files.write_private(path, secret.strip())


def read_secret(name: str) -> str | None:
    path = _path(name)
    if not path.exists():
        return None
    return path.read_text().strip()


def delete_secret(name: str) -> None:
    _path(name).unlink(missing_ok=True)
