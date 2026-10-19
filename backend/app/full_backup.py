"""Full OPNexus backup: the database and every secret store in one encrypted file.

The nightly database dump is not enough to rebuild an OPNexus server: the firewalls' API secrets, TOTP
secrets, VPN/certificate keys and the stored config snapshots live in separate volumes. This packs all of it
(``database.dump`` + the data directories + ``manifest.json``) into a tar.gz and encrypts it with a passphrase
(AES-256-GCM, services/backup_crypto.py). Without the passphrase the file is useless, so keep it elsewhere.

Normally driven by the host CLI:

    ./opnexus backup --full [FILE]      # writes an encrypted .opxfull file
    ./opnexus restore FILE              # onto a fresh or existing installation

or directly (passphrase in FULL_BACKUP_PASSPHRASE):

    python -m app.full_backup create  > file.opxfull
    python -m app.full_backup info    < file.opxfull       # manifest only
    python -m app.full_backup restore < file.opxfull
"""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import settings
from app.services import backup_crypto
from app.version import APP_VERSION

FORMAT = 1
MIN_PASSPHRASE = 12
# manifest key -> settings attribute holding the directory
_DATA_DIRS = {
    "opnsense_secrets": "opnsense_secret_dir",
    "opnsense_backups": "opnsense_backup_dir",
    "ipsec_psk_secrets": "ipsec_psk_secret_dir",
    "openvpn_key_secrets": "openvpn_key_secret_dir",
    "wireguard_key_secrets": "wireguard_key_secret_dir",
    "cert_key_secrets": "cert_key_secret_dir",
    "acme_secrets": "acme_secret_dir",
    "totp_secrets": "totp_secret_dir",
    "notification_secrets": "notification_secret_dir",
    "overlay": "overlay_dir",
}


class FullBackupError(Exception):
    pass


def _dir(name: str) -> Path:
    return Path(getattr(settings, _DATA_DIRS[name]))


def _db_args() -> tuple[list[str], dict]:
    url = make_url(settings.database_url)
    return (
        ["-h", url.host or "localhost", "-p", str(url.port or 5432), "-U", url.username or "", "-d", url.database or ""],
        {**os.environ, "PGPASSWORD": url.password or ""},
    )


def _dump_database(target: Path) -> None:
    args, env = _db_args()
    skip = [f"--exclude-table-data={t}" for t in ("firewall_log_entries", "host_resource_samples")]
    subprocess.run(["pg_dump", *args, "-F", "c", *skip, "-f", str(target)], env=env, check=True, capture_output=True, timeout=1800)


def _restore_database(source: Path) -> None:
    args, env = _db_args()
    result = subprocess.run(
        ["pg_restore", *args, "--clean", "--if-exists", "--no-owner", "--exit-on-error", str(source)],
        env=env, capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        raise FullBackupError(f"pg_restore fehlgeschlagen: {result.stderr.strip()[:400]}")


def _head_revision() -> str:
    from alembic.script import ScriptDirectory
    from app import migrate
    return ScriptDirectory.from_config(migrate._config()).get_current_head()


def create(passphrase: str, *, dump=_dump_database, revision=None) -> bytes:
    if len(passphrase) < MIN_PASSPHRASE:
        raise FullBackupError(f"Die Passphrase braucht mindestens {MIN_PASSPHRASE} Zeichen")
    manifest = {
        "format": FORMAT, "app_version": APP_VERSION, "db_revision": revision or _head_revision(),
        "created_at": datetime.now(timezone.utc).isoformat(), "data": sorted(_DATA_DIRS),
    }
    plain = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=plain, mode="w:gz") as tar:
        raw = json.dumps(manifest, indent=1).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        dump_path = Path(tmp) / "database.dump"
        dump(dump_path)
        tar.add(dump_path, arcname="database.dump")
        for name in _DATA_DIRS:
            path = _dir(name)
            if path.is_dir():
                tar.add(path, arcname=f"data/{name}")
    return backup_crypto.encrypt(plain.getvalue(), passphrase)


def _open(blob: bytes, passphrase: str) -> tarfile.TarFile:
    try:
        plain = backup_crypto.decrypt(blob, passphrase)
    except backup_crypto.DecryptError as exc:
        raise FullBackupError(str(exc)) from None
    try:
        return tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz")
    except tarfile.TarError:
        raise FullBackupError("Das ist keine vollständige OPNexus-Sicherung (Archiv unlesbar)") from None


def _manifest(tar: tarfile.TarFile) -> dict:
    try:
        member = tar.extractfile("manifest.json")
        manifest = json.loads(member.read()) if member else None
    except (KeyError, ValueError):
        manifest = None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise FullBackupError("Das ist keine vollständige OPNexus-Sicherung (Manifest fehlt oder ist neuer)")
    return manifest


def info(blob: bytes, passphrase: str) -> dict:
    with _open(blob, passphrase) as tar:
        return _manifest(tar)


def restore(blob: bytes, passphrase: str, *, restore_db=_restore_database, head=None) -> dict:
    """Put database and data directories back. The caller stops the backend first."""
    with _open(blob, passphrase) as tar:
        manifest = _manifest(tar)
        if not _older_or_equal(manifest.get("db_revision"), head):
            raise FullBackupError(
                f"Die Sicherung stammt aus einer neueren OPNexus-Version ({manifest.get('app_version')}). "
                "Erst auf diese Version aktualisieren, dann zurückspielen."
            )
        members = tar.getmembers()
        for member in members:  # reject anything that could escape: absolute paths, .., links, devices
            if member.name.startswith(("/", "\\")) or ".." in Path(member.name).parts or not (member.isfile() or member.isdir()):
                raise FullBackupError(f"Unzulässiger Eintrag im Archiv: {member.name!r}")
        known = {f"data/{name}" for name in _DATA_DIRS}
        with tempfile.TemporaryDirectory() as tmp:
            dump = tar.extractfile("database.dump")
            if dump is None:
                raise FullBackupError("Die Sicherung enthält keine Datenbank")
            dump_path = Path(tmp) / "database.dump"
            dump_path.write_bytes(dump.read())
            restore_db(dump_path)
        restored = []
        for member in members:
            parts = Path(member.name).parts
            if len(parts) < 2 or parts[0] != "data" or f"data/{parts[1]}" not in known:
                continue
            target_root = _dir(parts[1])
            target = target_root.joinpath(*parts[2:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = tar.extractfile(member)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(source.read())
            os.utime(target, (member.mtime, member.mtime))  # snapshot ages come from file times
            restored.append(parts[1])
        return {**manifest, "files_restored": len(restored)}


def _older_or_equal(revision: str | None, head: str | None) -> bool:
    """Revisions are zero-padded numbers ('0009'): a dump from an older schema is fine, the app migrates it."""
    try:
        return int(revision) <= int(head or _head_revision())
    except (TypeError, ValueError):
        return False


def _main(argv: list[str]) -> int:
    passphrase = os.environ.get("FULL_BACKUP_PASSPHRASE", "")
    if len(argv) != 2 or argv[1] not in ("create", "info", "restore"):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        if argv[1] == "create":
            sys.stdout.buffer.write(create(passphrase))
        elif argv[1] == "info":
            print(json.dumps(info(sys.stdin.buffer.read(), passphrase), indent=1))
        else:
            print(json.dumps(restore(sys.stdin.buffer.read(), passphrase), indent=1))
    except (FullBackupError, subprocess.SubprocessError, OSError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
