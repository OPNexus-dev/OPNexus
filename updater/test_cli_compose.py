"""refresh_compose / restore_compose in `opnexus` (B18): `update` takes the target release's docker-compose.yml from the
signed updater image, keeps a backup, never overwrites a file that was changed here, and `rollback` puts it back.
Runs the real functions with fake `docker` and `cosign` in front of PATH.
Run: cd updater && python3 -m unittest test_cli_compose"""
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("opnexus").resolve()
DIGEST = "registry.example/opnexus/opnexus-updater@sha256:" + "b" * 64
FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1" in
  pull) exit 0 ;;
  image) echo "$FAKE_DIGEST" ;;
  run) [ -f "$FAKE_COMPOSE" ] || exit 1; cat "$FAKE_COMPOSE" ;;
  compose) exit 0 ;;
esac
"""
FAKE_COSIGN = """#!/bin/sh
echo "cosign $*" >> "$FAKE_LOG"
[ "${FAKE_COSIGN_RC:-0}" = 0 ]
"""
OLD = "services:\n  backend:\n    image: x/opnexus-backend:1\n"
NEW = "services:\n  backend:\n    image: x/opnexus-backend:2\n    security_opt: [\"no-new-privileges:true\"]\n"


def _exe(path: Path, text: str):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ComposeRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        _exe(self.tmp / "bin" / "docker", FAKE_DOCKER)
        _exe(self.tmp / "bin" / "cosign", FAKE_COSIGN)
        (self.tmp / ".env").write_text("OPNEXUS_IMAGE_REPO=registry.example/opnexus\n")
        (self.tmp / "cosign.pub").write_text("key")
        (self.tmp / "docker-compose.yml").write_text(OLD)
        self.offered = self.tmp / "offered.yml"
        self.offered.write_text(NEW)
        self.log = self.tmp / "calls.log"

    def run_body(self, body: str, *, no_cosign=False, env_extra=None):
        if no_cosign:
            (self.tmp / "bin" / "cosign").unlink()
        path = f"{self.tmp / 'bin'}:/usr/bin:/bin"
        env = {**os.environ, "PATH": path, "OPNEXUS_DIR": str(self.tmp), "FAKE_LOG": str(self.log),
               "FAKE_COMPOSE": str(self.offered), "FAKE_DIGEST": DIGEST, "TMPDIR": str(self.tmp)}
        env.pop("OPNEXUS_NO_COMPOSE_REFRESH", None)
        env.update(env_extra or {})
        script = f'source <(sed "/^case \\"\\${{1:-}}\\" in/,\\$d" "{SCRIPT}"); {body}'
        return subprocess.run(["bash", "-c", script, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)

    def refresh(self, **kw):
        return self.run_body('refresh_compose 9.9.9; echo "BACKUP=$COMPOSE_BACKUP PREV=$COMPOSE_PREV_SHA"', **kw)

    def compose(self) -> str:
        return (self.tmp / "docker-compose.yml").read_text()

    def recorded(self) -> str | None:
        f = self.tmp / ".opnexus-compose-sha"
        return f.read_text().strip() if f.exists() else None

    def test_first_update_replaces_the_file_and_keeps_a_backup(self):
        r = self.refresh()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.compose(), NEW)
        self.assertEqual((self.tmp / "docker-compose.yml.bak-alt").read_text(), OLD)
        self.assertEqual(self.recorded(), _sha(NEW))
        self.assertIn("BACKUP=docker-compose.yml.bak-alt", r.stdout)
        self.assertIn("docker-compose.override.yml", r.stdout)  # first time: point to the override file
        calls = self.log.read_text().splitlines()
        self.assertTrue(any(c.startswith("cosign verify") and c.endswith(DIGEST) for c in calls))
        run = next(c for c in calls if c.startswith("run "))
        self.assertTrue(run.endswith(f"{DIGEST} /usr/share/opnexus/docker-compose.yml"))  # the verified digest, not the tag

    def test_an_unchanged_file_from_the_last_update_is_replaced_quietly(self):
        (self.tmp / ".opnexus-compose-sha").write_text(_sha(OLD) + "\n")
        r = self.refresh()
        self.assertEqual(self.compose(), NEW)
        self.assertIn(f"PREV={_sha(OLD)}", r.stdout)
        self.assertNotIn("diff ", r.stdout)

    def test_a_file_changed_here_is_kept_and_the_new_one_offered_beside_it(self):
        (self.tmp / ".opnexus-compose-sha").write_text(_sha(OLD) + "\n")
        (self.tmp / "docker-compose.yml").write_text(OLD + "    # my change\n")
        r = self.refresh()
        self.assertEqual(self.compose(), OLD + "    # my change\n")
        self.assertEqual((self.tmp / "docker-compose.yml.new").read_text(), NEW)
        self.assertIn("BACKUP= ", r.stdout)
        self.assertIn("docker-compose.override.yml", r.stdout)

    def test_an_identical_file_only_records_its_checksum(self):
        (self.tmp / "docker-compose.yml").write_text(NEW)
        self.refresh()
        self.assertEqual(self.recorded(), _sha(NEW))
        self.assertFalse(list(self.tmp.glob("docker-compose.yml.bak-*")))

    def test_an_old_release_without_a_compose_file_changes_nothing(self):
        self.offered.unlink()
        r = self.refresh()
        self.assertEqual(self.compose(), OLD)
        self.assertIn("vor 1.34", r.stdout)

    def test_something_that_is_not_an_opnexus_compose_file_is_ignored(self):
        self.offered.write_text("hello: world\n")
        self.refresh()
        self.assertEqual(self.compose(), OLD)

    def test_without_cosign_nothing_is_taken_from_an_image(self):
        r = self.refresh(no_cosign=True)
        self.assertEqual(self.compose(), OLD)
        self.assertIn("ohne cosign", r.stdout)
        self.assertFalse(any(c.startswith("run ") for c in (self.log.read_text().splitlines() if self.log.exists() else [])))

    def test_a_bad_signature_stops_before_anything_changes(self):
        r = self.refresh(env_extra={"FAKE_COSIGN_RC": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.compose(), OLD)

    def test_rollback_puts_the_previous_file_and_checksum_back(self):
        (self.tmp / ".opnexus-compose-sha").write_text(_sha(OLD) + "\n")
        self.refresh()
        r = self.run_body(f'prev_compose=docker-compose.yml.bak-alt prev_compose_sha={_sha(OLD)} restore_compose')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.compose(), OLD)
        self.assertEqual(self.recorded(), _sha(OLD))

    def test_rollback_without_a_swapped_file_does_nothing(self):
        r = self.run_body('prev_compose="" restore_compose; echo DONE')
        self.assertIn("DONE", r.stdout)
        self.assertEqual(self.compose(), OLD)


if __name__ == "__main__":
    unittest.main()
