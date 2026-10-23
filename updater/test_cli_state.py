"""The CLI never executes its own state file, and keeps .env private (audit 2026-10-23).
Runs the real functions of `opnexus` in a scratch directory.
Run: cd updater && python3 -m unittest test_cli_state"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("opnexus").resolve()
GOOD = "started=20261023T120000Z\nprev_version=1.32.1\nprev_setting=latest\nprev_db_revision=0031\ntarget=1.33.0\n"


class CliState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / ".env").write_text("OPNEXUS_IMAGE_REPO=registry.example/opnexus\n")

    def run_cli(self, body: str):
        env = {**os.environ, "OPNEXUS_DIR": str(self.tmp)}
        script = f'source <(sed "/^case \\"\\${{1:-}}\\" in/,\\$d" "{SCRIPT}"); {body}'
        return subprocess.run(["bash", "-c", script, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)

    def read_state(self, text: str):
        (self.tmp / ".opnexus-update-state").write_text(text)
        return self.run_cli(
            'f() { local started prev_version prev_setting prev_db_revision target; read_update_state; '
            'echo "$started|$prev_version|$prev_setting|$prev_db_revision|$target"; }; f')

    def test_a_normal_state_file_is_read(self):
        r = self.read_state(GOOD)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "20261023T120000Z|1.32.1|latest|0031|1.33.0")

    def test_commands_in_the_state_file_are_refused_and_never_run(self):
        marker = self.tmp / "PWNED"
        for evil in (f"prev_version=1.0; touch {marker}", f"prev_version=$(touch {marker})", f"prev_setting=`touch {marker}`",
                     "target=1.33.0 && true", "started=a b"):
            with self.subTest(evil):
                r = self.read_state(GOOD + evil + "\n")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("ungültigen Wert", r.stderr)
                self.assertFalse(marker.exists())

    def test_a_missing_key_stays_empty(self):
        r = self.read_state("prev_version=1.32.1\n")
        self.assertEqual(r.stdout.strip(), "|1.32.1|||")

    def test_env_readable_by_others_is_set_to_0600_and_a_private_one_is_left_alone(self):
        env = self.tmp / ".env"
        env.chmod(0o644)
        r = self.run_cli("protect_env")
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertIn("0600", r.stdout)
        r = self.run_cli("protect_env")
        self.assertEqual(r.stdout.strip(), "")  # nothing to report the second time


if __name__ == "__main__":
    unittest.main()
