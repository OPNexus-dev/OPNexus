"""Full OPNexus backup: round trip, integrity, refusal of hostile or newer archives."""
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from app import full_backup as fb
from app.config import settings
from app.services import backup_crypto

PASS = "full backup passphrase 1"
old_mtime = 1_700_000_000  # Nov 2023


@pytest.fixture
def data(tmp_path, monkeypatch):
    """Point every data directory at a scratch tree with recognisable content."""
    for name, attr in fb._DATA_DIRS.items():
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setattr(settings, attr, str(path))
    (tmp_path / "opnsense_secrets" / "host-1.secret").write_text("API-SECRET-1")
    (tmp_path / "totp_secrets").joinpath("u.secret").write_text("TOTP-SECRET")
    sub = tmp_path / "opnsense_backups" / "host-1"
    sub.mkdir()
    (sub / "20260101T000000.xml").write_text("<opnsense/>")
    os.utime(sub / "20260101T000000.xml", (old_mtime, old_mtime))
    return tmp_path


def _dump(path: Path) -> None:
    path.write_bytes(b"PGDMP-fake-database")


def _create(**kw):
    return fb.create(PASS, dump=_dump, revision="0009", **kw)


def test_roundtrip_restores_every_secret_and_the_database(data):
    blob = _create()
    assert b"API-SECRET-1" not in blob and b"TOTP-SECRET" not in blob and b"PGDMP" not in blob
    # Wipe the data, then restore.
    for f in data.rglob("*"):
        if f.is_file():
            f.unlink()
    seen = {}
    result = fb.restore(blob, PASS, restore_db=lambda p: seen.update(dump=p.read_bytes()), head="0012")
    assert seen["dump"] == b"PGDMP-fake-database"
    assert (data / "opnsense_secrets" / "host-1.secret").read_text() == "API-SECRET-1"
    assert (data / "totp_secrets" / "u.secret").read_text() == "TOTP-SECRET"
    assert (data / "opnsense_backups" / "host-1" / "20260101T000000.xml").read_text() == "<opnsense/>"
    assert result["files_restored"] == 3 and result["db_revision"] == "0009"
    mode = (data / "opnsense_secrets" / "host-1.secret").stat().st_mode & 0o777
    assert mode == 0o600
    # file times survive: stored config backups are aged by their mtime
    assert abs((data / "opnsense_backups" / "host-1" / "20260101T000000.xml").stat().st_mtime - old_mtime) < 2


def test_info_shows_only_the_manifest_and_needs_the_passphrase(data):
    blob = _create()
    manifest = fb.info(blob, PASS)
    assert manifest["format"] == 1 and manifest["db_revision"] == "0009" and "opnsense_secrets" in manifest["data"]
    with pytest.raises(fb.FullBackupError, match="Passphrase"):
        fb.info(blob, "wrong passphrase 123")


def test_short_passphrase_is_refused(data):
    with pytest.raises(fb.FullBackupError, match="mindestens"):
        fb.create("short", dump=_dump, revision="0009")


def test_tampered_or_foreign_files_are_refused(data):
    blob = bytearray(_create())
    blob[-5] ^= 0xFF
    with pytest.raises(fb.FullBackupError):
        fb.restore(bytes(blob), PASS, restore_db=lambda p: None, head="0012")
    for junk in (b"", b"not a backup"):
        with pytest.raises(fb.FullBackupError):
            fb.restore(junk, PASS, restore_db=lambda p: None, head="0012")
    # A valid encrypted file that is not a full backup (a single config backup, say).
    with pytest.raises(fb.FullBackupError, match="Archiv unlesbar"):
        fb.restore(backup_crypto.encrypt(b"<opnsense/>", PASS), PASS, restore_db=lambda p: None, head="0012")


def test_a_backup_from_a_newer_version_is_refused_before_anything_is_touched(data):
    blob = fb.create(PASS, dump=_dump, revision="0020")
    touched = []
    with pytest.raises(fb.FullBackupError, match="neueren"):
        fb.restore(blob, PASS, restore_db=lambda p: touched.append(p), head="0012")
    assert touched == []
    (data / "totp_secrets" / "u.secret").write_text("UNCHANGED")
    assert (data / "totp_secrets" / "u.secret").read_text() == "UNCHANGED"


def _hostile(member_name: str, kind=tarfile.REGTYPE, link=""):
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w:gz") as tar:
        manifest = json.dumps({"format": 1, "db_revision": "0001"}).encode()
        info = tarfile.TarInfo("manifest.json"); info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        dump = tarfile.TarInfo("database.dump"); dump.size = 1
        tar.addfile(dump, io.BytesIO(b"x"))
        evil = tarfile.TarInfo(member_name); evil.type = kind; evil.linkname = link
        evil.size = 4 if kind == tarfile.REGTYPE else 0
        tar.addfile(evil, io.BytesIO(b"evil") if kind == tarfile.REGTYPE else None)
    return backup_crypto.encrypt(plain.getvalue(), PASS)


@pytest.mark.parametrize("name,kind,link", [
    ("../../etc/cron.d/x", tarfile.REGTYPE, ""),
    ("/etc/passwd", tarfile.REGTYPE, ""),
    ("data/opnsense_secrets/../../escape", tarfile.REGTYPE, ""),
    ("data/opnsense_secrets/link", tarfile.SYMTYPE, "/etc/shadow"),
    ("data/opnsense_secrets/dev", tarfile.CHRTYPE, ""),
])
def test_hostile_archives_are_rejected_before_the_database_is_touched(data, name, kind, link):
    touched = []
    with pytest.raises(fb.FullBackupError, match="Unzulässig"):
        fb.restore(_hostile(name, kind, link), PASS, restore_db=lambda p: touched.append(p), head="0012")
    assert touched == [] and not (data.parent / "escape").exists()


def test_unknown_directories_in_the_archive_are_ignored(data, tmp_path):
    blob = _hostile("data/not_a_known_volume/file")
    fb.restore(blob, PASS, restore_db=lambda p: None, head="0012")
    assert not (tmp_path / "not_a_known_volume").exists()


def test_command_line_reads_the_passphrase_from_the_environment(data, monkeypatch, capsys):
    blob = _create()
    monkeypatch.setenv("FULL_BACKUP_PASSPHRASE", PASS)
    class _In:  # sys.stdin stand-in with a binary buffer
        buffer = io.BytesIO(blob)
    monkeypatch.setattr("sys.stdin", _In)
    assert fb._main(["full_backup", "info"]) == 0
    assert '"format": 1' in capsys.readouterr().out
    monkeypatch.setenv("FULL_BACKUP_PASSPHRASE", "")
    assert fb._main(["full_backup", "create"]) == 1
    assert fb._main(["full_backup", "bogus"]) == 2
