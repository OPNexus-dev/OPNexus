"""H1: the full backup can carry files of the server itself (.env, the proxy's CA) for a host move.

They come from the CLI as a tar on stdin, live under host/ in the archive and only ever leave it again as a tar for
the CLI (``host-files``); ``restore`` never writes them, and nothing outside env/ and caddy-ca/ is accepted.
"""
import io
import json
import tarfile

import pytest

from app import full_backup as fb
from tests.test_full_backup import PASS, _dump, data  # noqa: F401  (fixture)


def _tar(entries: dict[str, bytes | None], *, links: dict[str, str] | None = None) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return out.getvalue()


HOST = _tar({
    "./env": b"POSTGRES_PASSWORD=pw-of-old-server\nHEARTBEAT_URL=https://hc.example/x\n",
    "./caddy-ca": None,
    "./caddy-ca/local": None,
    "./caddy-ca/local/root.crt": b"-----BEGIN CERTIFICATE-----CA",
    "./caddy-ca/local/root.key": b"-----BEGIN EC PRIVATE KEY-----KEY",
})


def _host_tar_names(raw: bytes) -> dict[str, bytes | None]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        return {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}


def test_host_files_round_trip_and_are_encrypted(data):  # noqa: F811
    blob = fb.create(PASS, dump=_dump, revision="0009", host_files=HOST)
    assert b"pw-of-old-server" not in blob and b"PRIVATE KEY" not in blob
    assert fb.info(blob, PASS)["host"] == ["caddy-ca", "env"]
    files = _host_tar_names(fb.host_files(blob, PASS))
    assert files["env"].startswith(b"POSTGRES_PASSWORD=pw-of-old-server")
    assert files["caddy-ca/local/root.key"] == b"-----BEGIN EC PRIVATE KEY-----KEY"


def test_restore_never_writes_host_files(data):  # noqa: F811
    blob = fb.create(PASS, dump=_dump, revision="0009", host_files=HOST)
    before = {p for p in data.rglob("*")}
    fb.restore(blob, PASS, restore_db=lambda p: None, head="0012")
    created = {p for p in data.rglob("*")} - before
    assert not [p for p in created if "env" in p.name or "caddy" in str(p)]


def test_a_backup_without_host_files_hands_back_an_empty_tar(data):  # noqa: F811
    blob = fb.create(PASS, dump=_dump, revision="0009")
    assert "host" not in fb.info(blob, PASS)
    assert _host_tar_names(fb.host_files(blob, PASS)) == {}


@pytest.mark.parametrize("bad", [
    _tar({"etc/passwd": b"x"}),                      # outside env/ and caddy-ca/
    _tar({"caddy-ca/../../etc/x": b"x"}),            # escaping
    _tar({"/env": b"x"}),                            # absolute
    _tar({}, links={"env": "/etc/shadow"}),          # symlink
    b"not a tar at all",
    _tar({}),                                        # empty
])
def test_hostile_or_empty_host_files_are_refused_before_the_dump(data, bad):  # noqa: F811
    dumped = []
    with pytest.raises(fb.FullBackupError):
        fb.create(PASS, dump=lambda p: dumped.append(p), revision="0009", host_files=bad)
    assert dumped == []


def test_oversized_host_files_are_refused(data, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fb, "_HOST_MAX_BYTES", 100)
    with pytest.raises(fb.FullBackupError, match="zu groß"):
        fb.create(PASS, dump=_dump, revision="0009", host_files=HOST)


def test_command_line_create_with_host_files_and_extract(data, monkeypatch, capsysbinary):  # noqa: F811
    monkeypatch.setenv("FULL_BACKUP_PASSPHRASE", PASS)
    monkeypatch.setattr(fb, "_dump_database", _dump)
    monkeypatch.setattr(fb, "_head_revision", lambda: "0009")

    class Stdin:
        def __init__(self, raw):
            self.buffer = io.BytesIO(raw)

    monkeypatch.setattr("sys.stdin", Stdin(HOST))
    assert fb._main(["full_backup", "create", "--host-files"]) == 0
    blob = capsysbinary.readouterr().out
    monkeypatch.setattr("sys.stdin", Stdin(blob))
    assert fb._main(["full_backup", "host-files"]) == 0
    assert "env" in _host_tar_names(capsysbinary.readouterr().out)
    monkeypatch.setattr("sys.stdin", Stdin(blob))
    assert fb._main(["full_backup", "info"]) == 0
    assert json.loads(capsysbinary.readouterr().out)["host"] == ["caddy-ca", "env"]
    assert fb._main(["full_backup", "restore", "--host-files"]) == 2  # the flag belongs to create only
