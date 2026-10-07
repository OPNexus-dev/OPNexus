"""Writing secret material to disk without a window in which it is readable.

``Path.write_text()`` followed by ``chmod(0o600)`` creates the file with the
process umask (typically 0644) first. Here the file is created with mode 0600
from the start, under a fresh unpredictable name (``O_EXCL`` + ``O_NOFOLLOW``,
so nothing planted there is followed), flushed to disk and then renamed over
the destination — readers see either the complete old or the complete new
content, never a partial write, and a symlink at the destination is replaced
rather than written through.
"""
import os
import secrets
from pathlib import Path


def write_private(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = data.encode() if isinstance(data, str) else data
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
