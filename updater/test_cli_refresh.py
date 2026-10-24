"""refresh_cli in `opnexus` (B8): the CLI takes the newer copy from the target's updater image and restarts itself, but
only with a cosign check of the image DIGEST that succeeded in this run; every other failure continues with the present
CLI. Runs the real function with fake `docker` and `cosign` in front of PATH.
Run: cd updater && python3 -m unittest test_cli_refresh"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("opnexus").resolve()
DIGEST = "registry.example/opnexus/opnexus-updater@sha256:" + "a" * 64
FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1" in
  pull) [ "${FAKE_PULL_RC:-0}" = 0 ] || exit 1 ;;
  image) [ -z "${FAKE_NO_DIGEST:-}" ] && echo "$FAKE_DIGEST" ;;
  run) [ "${FAKE_RC:-0}" = 0 ] || exit "$FAKE_RC"; cat "$FAKE_CLI" 2>/dev/null ;;
esac
"""
FAKE_COSIGN = """#!/bin/sh
echo "cosign $*" >> "$FAKE_LOG"
[ "${FAKE_COSIGN_RC:-0}" = 0 ]
"""
NEW_CLI = '#!/usr/bin/env bash\\nrefresh_cli() {\\n :\\n}\\necho "NEW-CLI $*"\\n'.replace("\\n", "\n")


def _exe(path: Path, text: str):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class RefreshCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        _exe(self.tmp / "bin" / "docker", FAKE_DOCKER)
        _exe(self.tmp / "bin" / "cosign", FAKE_COSIGN)
        (self.tmp / ".env").write_text("OPNEXUS_IMAGE_REPO=registry.example/opnexus\n")
        (self.tmp / "cosign.pub").write_text("key")
        self.log = self.tmp / "calls.log"
        self.cli = self.tmp / "new-cli"

    def run_refresh(self, new_cli=NEW_CLI, *, env_extra=None, env_file=None, pre="", no_cosign=False, no_pub=False):
        self.cli.write_text(new_cli)
        if env_file is not None:
            (self.tmp / ".env").write_text(env_file)
        if no_pub:
            (self.tmp / "cosign.pub").unlink()
        if no_cosign:
            (self.tmp / "bin" / "cosign").unlink()
        path = f"{self.tmp / 'bin'}:/usr/bin:/bin" if no_cosign else f"{self.tmp / 'bin'}:{os.environ['PATH']}"  # a real cosign must not leak in
        env = {**os.environ, "PATH": path, "OPNEXUS_DIR": str(self.tmp),
               "FAKE_LOG": str(self.log), "FAKE_CLI": str(self.cli), "FAKE_DIGEST": DIGEST, "TMPDIR": str(self.tmp)}
        for name in ("OPNEXUS_NO_CLI_REFRESH", "CLI_REFRESHED"):
            env.pop(name, None)
        env.update(env_extra or {})
        body = f'source <(sed "/^case \\"\\${{1:-}}\\" in/,\\$d" "{SCRIPT}"); {pre} refresh_cli 9.9.9; echo CONTINUED'
        return subprocess.run(["bash", "-c", body, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_verified_newer_cli_runs_by_digest_with_the_internal_marker_argument(self):
        r = self.run_refresh()
        self.assertIn("NEW-CLI __refreshed update 9.9.9", r.stdout)
        self.assertNotIn("CONTINUED", r.stdout)  # exec: the old script does not go on
        calls = self.calls()
        self.assertTrue(any(c.startswith("cosign verify") and c.endswith(DIGEST) for c in calls))     # signature of the DIGEST
        run = next(c for c in calls if c.startswith("run "))
        self.assertTrue(run.endswith(f"{DIGEST} /usr/local/bin/opnexus"))                              # that same digest is executed
        self.assertLess([c.split()[0] for c in calls].index("cosign"), [c.split()[0] for c in calls].index("run"))

    def test_identical_cli_continues_and_removes_the_image_it_pulled(self):
        r = self.run_refresh(SCRIPT.read_text())
        self.assertIn("CONTINUED", r.stdout)
        self.assertTrue(any(c.startswith("rmi registry.example/opnexus/opnexus-updater:9.9.9") for c in self.calls()))

    def test_image_stays_when_the_updater_profile_is_enabled(self):
        self.run_refresh(SCRIPT.read_text(), env_file="OPNEXUS_IMAGE_REPO=registry.example/opnexus\nCOMPOSE_PROFILES=updater\n")
        self.assertFalse(any(c.startswith("rmi") for c in self.calls()))

    def test_nothing_runs_without_a_real_signature_check(self):
        for label, kwargs in {"no cosign": {"no_cosign": True}, "no cosign.pub": {"no_pub": True}}.items():
            with self.subTest(label):
                self.log.unlink(missing_ok=True)
                r = self.run_refresh(**kwargs)
                self.assertIn("CONTINUED", r.stdout)
                self.assertFalse(any(c.startswith(("pull", "run")) for c in self.calls()))

    def test_invalid_signature_aborts_the_update_and_runs_nothing(self):
        r = self.run_refresh(env_extra={"FAKE_COSIGN_RC": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("CONTINUED", r.stdout)
        self.assertNotIn("NEW-CLI", r.stdout)
        self.assertFalse(any(c.startswith("run ") for c in self.calls()))

    def test_pull_failure_never_falls_back_to_a_local_image(self):
        r = self.run_refresh(env_extra={"FAKE_PULL_RC": "1"})
        self.assertIn("CONTINUED", r.stdout)
        self.assertFalse(any(c.startswith(("run ", "cosign")) for c in self.calls()))

    def test_unknown_digest_missing_marker_broken_or_empty_copies_are_skipped(self):
        for label, kwargs in {
            "no digest": {"env_extra": {"FAKE_NO_DIGEST": "1"}},
            "old CLI without refresh_cli": {"new_cli": "#!/usr/bin/env bash\necho OLD-CLI\n"},
            "syntax error": {"new_cli": "refresh_cli() {\nif then fi (\n"},
            "empty": {"new_cli": ""},
            "docker run fails": {"env_extra": {"FAKE_RC": "1"}},
        }.items():
            with self.subTest(label):
                r = self.run_refresh(**kwargs)
                self.assertIn("CONTINUED", r.stdout)
                self.assertNotIn("NEW-CLI", r.stdout)
                self.assertNotIn("OLD-CLI", r.stdout)

    def test_opt_out_and_missing_repo_do_not_even_ask_docker(self):
        for label, kwargs in {"opt-out": {"env_extra": {"OPNEXUS_NO_CLI_REFRESH": "1"}},
                              "no repo": {"env_file": "POSTGRES_PASSWORD=x\n"}}.items():
            with self.subTest(label):
                self.log.unlink(missing_ok=True)
                r = self.run_refresh(**kwargs)
                self.assertIn("CONTINUED", r.stdout)
                self.assertEqual(self.calls(), [])

    def test_restart_guard_is_a_shell_variable_an_inherited_environment_value_cannot_switch_it_off(self):
        inherited = self.run_refresh(env_extra={"CLI_REFRESHED": "1"})
        self.assertIn("NEW-CLI __refreshed", inherited.stdout)     # inheriting the name changes nothing
        self.log.unlink(missing_ok=True)
        again = self.run_refresh(pre="CLI_REFRESHED=1;")           # the refreshed copy itself does not refresh again
        self.assertIn("CONTINUED", again.stdout)
        self.assertEqual(self.calls(), [])

    def test_refreshed_copy_removes_its_own_temp_file(self):
        copy = self.tmp / "opnexus-cli.abc123"
        shutil.copy(SCRIPT, copy)
        env = {**os.environ, "OPNEXUS_DIR": str(self.tmp)}
        subprocess.run(["bash", str(copy), "__refreshed", "bogus"], env=env, capture_output=True, text=True, timeout=30)
        self.assertFalse(copy.exists())


if __name__ == "__main__":
    unittest.main()


class RefreshCliOnDisk(RefreshCli):
    """B18: in the release bundle (CLI next to docker-compose.yml) the file on disk is replaced as well, so a later
    `rollback` runs the new CLI; a repo checkout (scripts/opnexus -> updater/opnexus) is never touched."""

    def run_copy(self, *, with_compose: bool):
        bundle = self.tmp / "bundle"
        bundle.mkdir()
        copy = bundle / "opnexus"
        shutil.copy2(SCRIPT, copy)
        if with_compose:
            (bundle / "docker-compose.yml").write_text("services: {}\n")
        self.cli.write_text(NEW_CLI)
        env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}", "OPNEXUS_DIR": str(self.tmp),
               "FAKE_LOG": str(self.log), "FAKE_CLI": str(self.cli), "FAKE_DIGEST": DIGEST, "TMPDIR": str(self.tmp)}
        for name in ("OPNEXUS_NO_CLI_REFRESH", "CLI_REFRESHED"):
            env.pop(name, None)
        body = f'source <(sed "/^case \\"\\${{1:-}}\\" in/,\\$d" "{copy}"); refresh_cli 9.9.9; echo CONTINUED'
        result = subprocess.run(["bash", "-c", body, str(copy)], env=env, capture_output=True, text=True, timeout=30)
        return result, copy

    def test_bundle_cli_on_disk_is_replaced_with_a_backup(self):
        r, copy = self.run_copy(with_compose=True)
        self.assertIn("NEW-CLI __refreshed update 9.9.9", r.stdout)
        self.assertEqual(copy.read_text(), NEW_CLI)
        self.assertEqual(Path(str(copy) + ".bak").read_text(), SCRIPT.read_text())
        self.assertTrue(os.access(copy, os.X_OK))

    def test_a_cli_without_a_compose_file_next_to_it_is_left_alone(self):
        r, copy = self.run_copy(with_compose=False)
        self.assertIn("NEW-CLI __refreshed update 9.9.9", r.stdout)
        self.assertEqual(copy.read_text(), SCRIPT.read_text())
        self.assertFalse(Path(str(copy) + ".bak").exists())


class RefreshedCliInstallsItself(unittest.TestCase):
    """The first hop is started by an OLD CLI (no on-disk replacement in its refresh_cli): the new one, running from
    its temp copy with __refreshed, puts itself into the bundle so a later `rollback` runs the new code."""

    def test_the_refreshed_cli_replaces_the_bundle_file(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bundle = tmp / "bundle"
        bundle.mkdir()
        (bundle / "opnexus").write_text("#!/bin/bash\necho old\n")
        (bundle / "docker-compose.yml").write_text("services: {}\n")
        (bundle / ".env").write_text("OPNEXUS_IMAGE_REPO=registry.example/opnexus\n")  # a real install always has one
        running = tmp / "opnexus-cli.abc123"
        shutil.copy2(SCRIPT, running)
        env = {**os.environ, "OPNEXUS_DIR": str(bundle)}
        r = subprocess.run(["bash", str(running), "__refreshed", "no-such-command"], env=env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual((bundle / "opnexus").read_text(), SCRIPT.read_text())
        self.assertEqual((bundle / "opnexus.bak").read_text(), "#!/bin/bash\necho old\n")
        self.assertFalse(running.exists())  # the temp copy is cleaned up as before
        self.assertIn("CLI auf der Platte aktualisiert", r.stdout)
