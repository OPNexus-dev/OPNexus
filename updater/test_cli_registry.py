"""Registry-Auswahl, Signaturprüfung, Update und Rollback der CLI `opnexus` mit GHCR und mit der Gitea-Registry.
Zusicherungen (Release-Pipeline mit zwei Registries): Die Registry einer Installation steht in ihrer .env (OPNEXUS_IMAGE_REPO) und
bleibt bei Update und Rollback unverändert, nichts wechselt sie von selbst; Pull und cosign-Prüfung nutzen genau diese Registry; eine
fehlende oder ungültige Signatur bricht VOR dem Wechsel ab. `docker` und `cosign` sind Attrappen.
Run: cd updater && python3 -m unittest test_cli_registry"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("opnexus").resolve()
GHCR = "ghcr.io/opnexus-dev"
GITEA = "git.opnexus.dev/opnexus"

FAKE_DOCKER = r'''#!__PY__
import json, os, sys
d = os.environ["FAKE_DIR"]
args = sys.argv[1:]
def env_version():
    for line in open(os.path.join(os.environ["OPNEXUS_DIR"], ".env")):
        if line.startswith("OPNEXUS_VERSION="):
            return line.strip().split("=", 1)[1]
    return ""
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write("docker " + " ".join(args) + (f"   [OPNEXUS_VERSION={os.environ['OPNEXUS_VERSION']}]" if "OPNEXUS_VERSION" in os.environ else "") + "\n")
state = os.path.join(d, "running")
if args[:1] == ["compose"]:
    rest = args[1:]
    if rest[:1] == ["pull"]:
        sys.exit(1 if os.environ.get("FAKE_MISSING_TAG") == os.environ.get("OPNEXUS_VERSION") else 0)
    if rest[:1] == ["up"]:
        open(state, "w").write(env_version())
        sys.exit(0)
    if rest[:1] == ["exec"] and "backend" in rest:
        print(open(state).read().strip()); sys.exit(0)
    if rest[:1] == ["exec"] and "postgres" in rest:
        print("0033"); sys.exit(0)
    if rest[:1] == ["ps"] and "--format" in rest:
        if rest[-1] == "{{.Health}}":
            bad = os.environ.get("FAKE_UNHEALTHY")
            print("unhealthy" if bad and open(state).read().strip() == bad else "healthy")
        sys.exit(0)
    if rest[:1] == ["config"]:
        print(json.dumps({"services": {"proxy": {"environment": {"OPNEXUS_GUI_SITES": "https://localhost:9441"}}}})); sys.exit(0)
sys.exit(0)
'''
FAKE_COSIGN = r'''#!__PY__
import os, sys
with open(os.path.join(os.environ["FAKE_DIR"], "calls.log"), "a") as f:
    f.write("cosign " + " ".join(sys.argv[1:]) + "\n")
sys.exit(int(os.environ.get("FAKE_COSIGN_RC", "0")))
'''


def _exe(path: Path, text: str):
    path.write_text(text.replace("__PY__", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class RegistryBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        _exe(self.tmp / "bin" / "docker", FAKE_DOCKER)
        _exe(self.tmp / "bin" / "cosign", FAKE_COSIGN)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        (self.proj / "docker-compose.yml").write_text("services:\n  backend:\n    image: x\n")
        (self.proj / "cosign.pub").write_text("key")
        self.fake = self.tmp / "fake"
        self.fake.mkdir()

    def install(self, repo: str, version="1.35.0"):
        (self.proj / ".env").write_text(f"POSTGRES_PASSWORD=x\nOPNEXUS_IMAGE_REPO={repo}\nOPNEXUS_VERSION={version}\n")
        (self.fake / "running").write_text(version)
        self.log = self.fake / "calls.log"

    def cli(self, *args, env=None):
        e = {**os.environ, "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "OPNEXUS_DIR": str(self.proj), "FAKE_DIR": str(self.fake),
             "OPNEXUS_NO_CLI_REFRESH": "1", "OPNEXUS_NO_COMPOSE_REFRESH": "1", "OPNEXUS_UPDATE_TIMEOUT": "2", "TMPDIR": str(self.tmp)}
        e.update(env or {})
        for k in ("COMPOSE_PROFILES", "OPNEXUS_UPDATER"):
            e.pop(k, None)
        return subprocess.run(["bash", str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=60)

    def env_value(self, key):
        m = re.search(rf"^{key}=(.*)$", (self.proj / ".env").read_text(), re.M)
        return m.group(1) if m else None

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def for_each_registry(self):
        for repo in (GHCR, GITEA):
            with self.subTest(registry=repo):
                for f in self.fake.iterdir():
                    f.unlink()
                self.install(repo)
                yield repo

    # ── Update ──
    def test_update_verifies_the_signatures_in_the_registry_of_the_installation(self):
        for repo in self.for_each_registry():
            r = self.cli("update", "1.36.0")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            verifies = [c for c in self.calls() if c.startswith("cosign verify")]
            for svc in ("backend", "frontend", "proxy"):
                self.assertTrue(any(c.endswith(f" {repo}/opnexus-{svc}:1.36.0") for c in verifies), (svc, verifies))
            other = GITEA if repo == GHCR else GHCR
            self.assertFalse([c for c in self.calls() if other in c])   # die andere Registry wird nie angefasst
            self.assertTrue(all("--key cosign.pub" in c for c in verifies))
            self.assertEqual(self.env_value("OPNEXUS_VERSION"), "1.36.0")
            self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), repo)   # die Registry bleibt, wie sie war

    def test_the_images_are_pulled_and_verified_before_anything_is_switched(self):
        self.install(GHCR)
        self.cli("update", "1.36.0")
        calls = self.calls()
        pull = next(i for i, c in enumerate(calls) if c.startswith("docker compose pull"))
        verify = next(i for i, c in enumerate(calls) if c.startswith("cosign verify"))
        up = next(i for i, c in enumerate(calls) if c.startswith("docker compose up"))
        self.assertLess(pull, verify)
        self.assertLess(verify, up)

    def test_a_bad_signature_stops_the_update_before_the_switch(self):
        for repo in self.for_each_registry():
            r = self.cli("update", "1.36.0", env={"FAKE_COSIGN_RC": "1"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Signatur", r.stderr)
            self.assertEqual(self.env_value("OPNEXUS_VERSION"), "1.35.0")
            self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), repo)
            self.assertFalse([c for c in self.calls() if c.startswith("docker compose up")])
            self.assertFalse((self.proj / ".opnexus-update-state").exists())

    def test_a_version_missing_in_the_registry_changes_nothing(self):
        # So sieht ein Update auf eine Version aus, die es in dieser Registry nicht gibt (z. B. ein Backfill fehlt).
        for repo in self.for_each_registry():
            r = self.cli("update", "1.36.0", env={"FAKE_MISSING_TAG": "1.36.0"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("nichts wurde verändert", r.stderr)
            self.assertEqual(self.env_value("OPNEXUS_VERSION"), "1.35.0")
            self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), repo)
            self.assertFalse([c for c in self.calls() if c.startswith("cosign") or c.startswith("docker compose up")])

    # ── Rollback ──
    def test_an_unhealthy_update_rolls_back_in_the_same_registry_without_pulling_again(self):
        for repo in self.for_each_registry():
            r = self.cli("update", "1.36.0", env={"FAKE_UNHEALTHY": "1.36.0"})
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn("zurückgerollt", r.stderr)
            self.assertEqual(self.env_value("OPNEXUS_VERSION"), "1.35.0")
            self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), repo)   # auch nach dem Rollback unverändert
            self.assertEqual((self.fake / "running").read_text(), "1.35.0")
            calls = self.calls()
            ups = [i for i, c in enumerate(calls) if c.startswith("docker compose up")]
            self.assertEqual(len(ups), 2)
            self.assertTrue(all("--no-build" in calls[i] for i in ups))
            self.assertFalse([c for c in calls[ups[0] + 1:] if c.startswith("docker compose pull")])   # der Rollback zieht nichts nach dem Wechsel

    def test_a_manual_rollback_restores_the_previous_version_and_keeps_the_registry(self):
        for repo in self.for_each_registry():
            self.assertEqual(self.cli("update", "1.36.0").returncode, 0)
            r = self.cli("rollback")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.env_value("OPNEXUS_VERSION"), "1.35.0")
            self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), repo)

    def test_a_rollback_needs_the_installations_own_registry_to_have_the_previous_version(self):
        """Dokumentiert die Abhängigkeit: Der Rollback startet mit `up --no-build`; fehlt das alte Image lokal, zieht Compose es
        aus der Registry der .env nach. Ein Wechsel der Registry zusammen mit einem Update braucht dort also die Vorversion."""
        self.install(GHCR)
        self.cli("update", "1.36.0")
        self.cli("rollback")
        self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), GHCR)
        text = SCRIPT.read_text()
        self.assertIn("up -d --no-build", text[text.index("cmd_rollback() {"):])

    # ── Keine versteckte Umstellung ──
    def test_nothing_in_the_cli_writes_the_registry(self):
        text = SCRIPT.read_text()
        self.assertIsNone(re.search(r"env_set(_literal)?\s+\"?OPNEXUS_IMAGE_REPO", text))
        self.assertIsNone(re.search(r"sed -i[^\n]*OPNEXUS_IMAGE_REPO", text))
        self.assertIn("OPNEXUS_IMAGE_REPO", re.search(r'ENV_KEEP_KEYS="([^"]*)"', text).group(1))   # auch ein Restore übernimmt sie nie

    def test_a_restore_never_takes_the_registry_from_a_backup(self):
        (self.proj / ".env").write_text(f"POSTGRES_PASSWORD=x\nOPNEXUS_IMAGE_REPO={GITEA}\n")
        backup = self.tmp / "backup.env"
        backup.write_text(f"POSTGRES_PASSWORD=y\nOPNEXUS_IMAGE_REPO={GHCR}\nHEARTBEAT_URL=https://example.invalid/x\n")
        script = f'source <(sed "/^case \\"\\${{1:-}}\\" in/,\\$d" "{SCRIPT}"); merge_env_from_backup "{backup}" 1'
        r = subprocess.run(["bash", "-c", script, str(SCRIPT)], env={**os.environ, "OPNEXUS_DIR": str(self.proj)}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), GITEA)
        self.assertEqual(self.env_value("HEARTBEAT_URL"), "https://example.invalid/x")

    def test_a_status_shows_the_registry_of_the_installation(self):
        for repo in self.for_each_registry():
            r = self.cli("status", "--porcelain")
            self.assertIn(f"repo={repo}", r.stdout)


if __name__ == "__main__":
    unittest.main()
