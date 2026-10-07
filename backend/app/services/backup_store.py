"""Per-host OPNsense config.xml backup storage, on disk under
<backup_dir>/<host_id>/<timestamp>.xml — mirrors opnsense_secret_store.py's
convention (never in the database, settings-driven directory, chmod 600)
but keeps multiple retained snapshots per host instead of a single file.
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

_SUFFIX = ".xml"


def _host_dir(host_id: uuid.UUID) -> Path:
    return Path(settings.opnsense_backup_dir) / str(host_id)


def save_snapshot(host_id: uuid.UUID, content: bytes) -> str:
    """Writes a new snapshot, filename = sortable UTC timestamp with
    microsecond precision to avoid collisions on rapid consecutive saves.
    Returns the filename, used as the backup's id."""
    host_dir = _host_dir(host_id)
    host_dir.mkdir(parents=True, exist_ok=True)
    filename = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + _SUFFIX
    path = host_dir / filename
    path.write_bytes(content)
    path.chmod(0o600)
    return filename


def list_snapshots(host_id: uuid.UUID) -> list[dict]:
    """Returns [{"id", "created_at", "size"}], newest first."""
    host_dir = _host_dir(host_id)
    if not host_dir.exists():
        return []
    items = []
    for path in host_dir.glob(f"*{_SUFFIX}"):
        stat = path.stat()
        items.append({
            "id": path.name,
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            "size": stat.st_size,
        })
    return sorted(items, key=lambda i: i["created_at"], reverse=True)


def read_snapshot(host_id: uuid.UUID, backup_id: str) -> bytes | None:
    path = _host_dir(host_id) / backup_id
    # backup_id comes straight from a URL path parameter — reject anything
    # that would escape the host's own directory (e.g. "../../etc/passwd")
    # before it ever reaches the filesystem.
    if backup_id != path.name or not path.is_file():
        return None
    return path.read_bytes()
