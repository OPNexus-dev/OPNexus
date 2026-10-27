"""Digest-Auswahl in refresh_cli / refresh_compose (`image_digest_ref` in `opnexus`).

Fehler, den diese Tests festhalten: Dasselbe Image lag lokal unter mehreren Registry-Adressen (Gitea und GHCR). Die CLI nahm
`RepoDigests[0]` – irgendeine davon – und ließ cosign die Signatur in der falschen Registry prüfen („Signatur des Updater-Images
ungültig oder fehlt“, obwohl das Gitea-Image signiert war). Jetzt zählt nur der Eintrag, dessen Repository zur angeforderten
Referenz passt, nie die Position. Die Signaturprüfung bleibt strikt: sie gilt immer dem Digest unter der konfigurierten Registry.
Läuft die echten Funktionen mit Attrappen für `docker` und `cosign`.
Run: cd updater && python3 -m unittest test_cli_digest"""
import itertools
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("opnexus").resolve()
ROOT = Path(__file__).resolve().parent.parent
D1 = "sha256:944f773cfce79b16e35cebc448eb607fbf88013d5892df38a1cbb5a2c0543611"   # der Digest aus dem gemeldeten Fall
D2 = "sha256:" + "b" * 64
GITEA, GHCR_USER, GHCR_ORG = "git.opnexus.dev/opnexus", "ghcr.io/example-user", "ghcr.io/opnexus-dev"

FAKE_DOCKER = r"""#!/bin/bash
echo "$@" >> "$FAKE_LOG"
case "$1" in
  pull) [ "${FAKE_PULL_RC:-0}" = 0 ] || exit 1
        case "${2:-}" in -q) echo "${3:-}" ;; *) printf '%s\n' "${FAKE_PULL_OUT:-}" ;; esac ;;
  image) printf '%s\n' "${FAKE_REPODIGESTS:-}" | tr ';' '\n' ;;
  run) cat "$FAKE_PAYLOAD" ;;
esac
"""
FAKE_COSIGN = r"""#!/bin/sh
echo "cosign $*" >> "$FAKE_LOG"
case "$*" in *" ${FAKE_SIGNED_PREFIX:-git.opnexus.dev/opnexus/}"*) exit 0 ;; esac
exit 1
"""
NEW_CLI = '#!/usr/bin/env bash\nrefresh_cli() {\n :\n}\necho "NEW-CLI $*"\n'
NEW_COMPOSE = 'services:\n  backend:\n    image: ${OPNEXUS_IMAGE_REPO:-x}/opnexus-backend:2\n'
OLD_COMPOSE = 'services:\n  backend:\n    image: ${OPNEXUS_IMAGE_REPO:-x}/opnexus-backend:1\n'


def entry(repo, digest=D1, name="opnexus-updater"):
    return f"{repo}/{name}@{digest}"


class DigestSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("cosign", FAKE_COSIGN)):
            p = self.tmp / "bin" / name; p.write_text(text); p.chmod(p.stat().st_mode | stat.S_IXUSR)
        (self.tmp / "cosign.pub").write_text("key")
        (self.tmp / "docker-compose.yml").write_text(OLD_COMPOSE)
        self.log = self.tmp / "calls.log"
        self.payload = self.tmp / "payload"

    def run_fn(self, call, *, repo=GITEA, entries="", pull_out="", signed_prefix=None, payload=NEW_CLI, pull_rc=0):
        (self.tmp / ".env").write_text(f"OPNEXUS_IMAGE_REPO={repo}\n")
        self.payload.write_text(payload)
        env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "OPNEXUS_DIR": str(self.tmp), "FAKE_LOG": str(self.log), "FAKE_PAYLOAD": str(self.payload),
               "FAKE_REPODIGESTS": ";".join(entries) if isinstance(entries, (list, tuple)) else entries, "FAKE_PULL_OUT": pull_out, "FAKE_PULL_RC": str(pull_rc), "TMPDIR": str(self.tmp)}
        if signed_prefix:
            env["FAKE_SIGNED_PREFIX"] = signed_prefix
        for name in ("OPNEXUS_NO_CLI_REFRESH", "OPNEXUS_NO_COMPOSE_REFRESH", "CLI_REFRESHED", "FAKE_SIGNED_PREFIX_UNSET"):
            env.pop(name, None)
        if signed_prefix:
            env["FAKE_SIGNED_PREFIX"] = signed_prefix
        body = f'source <(sed "/^case \\"\\${{1:-}}\\" in/,\\$d" "{SCRIPT}"); {call}'
        return subprocess.run(["bash", "-c", body, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)

    def cli(self, **kw):
        return self.run_fn("refresh_cli 9.9.9; echo CONTINUED", **kw)

    def compose(self, **kw):
        return self.run_fn('refresh_compose 9.9.9; echo CONTINUED', payload=NEW_COMPOSE, **kw)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def verified(self):
        return [c.split()[-1] for c in self.calls() if c.startswith("cosign verify")]

    def executed(self):
        return [c for c in self.calls() if c.startswith("run ")]

    # ── der gemeldete Fall ──
    def test_the_reported_case_verifies_the_digest_under_the_configured_registry(self):
        entries = [entry(GHCR_USER), entry(GHCR_ORG), entry(GITEA)]   # GHCR zuerst, die konfigurierte Registry erst an dritter Stelle
        r = self.cli(entries=entries, pull_out=f"Digest: {D1}\nStatus: Image is up to date")
        self.assertIn("NEW-CLI __refreshed update 9.9.9", r.stdout, r.stdout + r.stderr)
        self.assertEqual(self.verified(), [entry(GITEA)])
        self.assertTrue(self.executed()[0].endswith(f"{entry(GITEA)} /usr/local/bin/opnexus"))   # derselbe geprüfte Digest wird ausgeführt

    def test_the_same_for_the_compose_file(self):
        entries = [entry(GHCR_USER), entry(GHCR_ORG), entry(GITEA)]
        r = self.compose(entries=entries, pull_out=f"Digest: {D1}")
        self.assertEqual(self.verified(), [entry(GITEA)])
        self.assertEqual((self.tmp / "docker-compose.yml").read_text(), NEW_COMPOSE, r.stdout + r.stderr)
        self.assertTrue(self.executed()[0].endswith(f"{entry(GITEA)} /usr/share/opnexus/docker-compose.yml"))

    def test_the_order_of_the_entries_never_matters(self):
        entries = [entry(GHCR_USER), entry(GHCR_ORG), entry(GITEA)]
        for perm in itertools.permutations(entries):
            self.log.unlink(missing_ok=True)
            r = self.cli(entries=list(perm), pull_out=f"Digest: {D1}")
            self.assertIn("NEW-CLI", r.stdout, perm)
            self.assertEqual(self.verified(), [entry(GITEA)], perm)

    def test_without_the_digest_line_the_single_entry_of_the_repository_is_used(self):
        entries = [entry(GHCR_USER), entry(GITEA), entry(GHCR_ORG)]
        r = self.cli(entries=entries, pull_out="")   # sehr alter Docker ohne "Digest:"-Zeile
        self.assertIn("NEW-CLI", r.stdout, r.stdout + r.stderr); self.assertEqual(self.verified(), [entry(GITEA)])

    # ── künftige GHCR-Installationen ──
    def test_a_ghcr_installation_verifies_under_ghcr_even_when_gitea_is_listed_first(self):
        entries = [entry(GITEA), entry(GHCR_USER), entry(GHCR_ORG)]
        r = self.cli(repo=GHCR_ORG, entries=entries, pull_out=f"Digest: {D1}", signed_prefix=GHCR_ORG + "/")
        self.assertIn("NEW-CLI", r.stdout, r.stdout + r.stderr); self.assertEqual(self.verified(), [entry(GHCR_ORG)])

    def test_each_configured_registry_selects_its_own_entry(self):
        entries = [entry(GITEA), entry(GHCR_USER), entry(GHCR_ORG)]
        for repo in (GITEA, GHCR_USER, GHCR_ORG):
            self.log.unlink(missing_ok=True)
            self.cli(repo=repo, entries=entries, pull_out=f"Digest: {D1}", signed_prefix=repo + "/")
            self.assertEqual(self.verified(), [entry(repo)], repo)

    # ── die Signaturprüfung bleibt strikt ──
    def test_a_signature_in_another_registry_does_not_count(self):
        entries = [entry(GHCR_USER), entry(GITEA)]
        r = self.cli(entries=entries, pull_out=f"Digest: {D1}", signed_prefix=GHCR_USER + "/")   # nur GHCR signiert, die konfigurierte Registry nicht
        self.assertNotEqual(r.returncode, 0); self.assertIn("Signatur des Updater-Images", r.stderr); self.assertNotIn("CONTINUED", r.stdout)
        self.assertEqual(self.verified(), [entry(GITEA)])   # geprüft wurde ausschließlich die konfigurierte Registry
        self.assertEqual(self.executed(), [])               # nichts aus dem Image ausgeführt

    def test_a_bad_signature_for_the_compose_file_stops_before_anything_changes(self):
        r = self.compose(entries=[entry(GHCR_USER), entry(GITEA)], pull_out=f"Digest: {D1}", signed_prefix=GHCR_USER + "/")
        self.assertNotEqual(r.returncode, 0); self.assertEqual((self.tmp / "docker-compose.yml").read_text(), OLD_COMPOSE); self.assertEqual(self.executed(), [])

    # ── keine passende Referenz: sicher überspringen ──
    def test_no_entry_for_the_configured_registry_skips_without_verifying_or_running_anything(self):
        r = self.cli(entries=[entry(GHCR_USER), entry(GHCR_ORG)], pull_out=f"Digest: {D1}")
        self.assertEqual(r.returncode, 0); self.assertIn("CONTINUED", r.stdout); self.assertIn("nicht eindeutig bestimmbar", r.stdout)
        self.assertEqual((self.verified(), self.executed()), ([], []))
        r = self.compose(entries=[entry(GHCR_USER)], pull_out=f"Digest: {D1}")
        self.assertIn("nicht eindeutig bestimmbar", r.stdout); self.assertEqual((self.tmp / "docker-compose.yml").read_text(), OLD_COMPOSE)

    def test_a_pulled_digest_that_is_not_listed_under_the_repository_skips(self):
        r = self.cli(entries=[entry(GITEA, D2), entry(GHCR_USER, D1)], pull_out=f"Digest: {D1}")   # der gezogene Digest steht nur bei GHCR
        self.assertIn("nicht eindeutig bestimmbar", r.stdout); self.assertEqual((self.verified(), self.executed()), ([], []))

    def test_two_digests_in_one_repository_are_resolved_by_the_pulled_digest(self):
        entries = [entry(GITEA, D2), entry(GITEA, D1)]
        r = self.cli(entries=entries, pull_out=f"Digest: {D1}")
        self.assertIn("NEW-CLI", r.stdout); self.assertEqual(self.verified(), [entry(GITEA, D1)])
        self.log.unlink()
        r = self.cli(entries=list(reversed(entries)), pull_out=f"Digest: {D2}")
        self.assertEqual(self.verified(), [entry(GITEA, D2)])

    def test_two_digests_in_one_repository_without_a_digest_line_are_ambiguous(self):
        r = self.cli(entries=[entry(GITEA, D2), entry(GITEA, D1)], pull_out="")
        self.assertIn("nicht eindeutig bestimmbar", r.stdout); self.assertEqual((self.verified(), self.executed()), ([], []))

    def test_a_lookalike_repository_is_not_taken_for_the_configured_one(self):
        entries = [f"mirror.example/{GITEA}/opnexus-updater@{D1}", f"{GITEA}-evil/opnexus-updater@{D1}", f"{GITEA}/opnexus-updater-x@{D1}"]
        r = self.cli(entries=entries, pull_out=f"Digest: {D1}")
        self.assertIn("nicht eindeutig bestimmbar", r.stdout); self.assertEqual(self.verified(), [])

    def test_malformed_entries_are_ignored(self):
        entries = [entry(GITEA, "sha256:zzz"), entry(GITEA, "sha256:" + "a" * 63), "", entry(GITEA, D1)]
        r = self.cli(entries=entries, pull_out=f"Digest: {D1}")
        self.assertEqual(self.verified(), [entry(GITEA, D1)], r.stdout + r.stderr)

    def test_a_registry_with_a_port_keeps_its_port_when_the_tag_is_removed(self):
        repo = "localhost:5000/opnexus"
        r = self.cli(repo=repo, entries=[entry("other:5000/x"), entry(repo)], pull_out=f"Digest: {D1}", signed_prefix=repo + "/")
        self.assertIn("NEW-CLI", r.stdout, r.stdout + r.stderr); self.assertEqual(self.verified(), [entry(repo)])

    def test_a_reference_that_already_is_a_digest_reference_keeps_its_repository(self):
        r = self.run_fn(f'image_digest_ref "{GITEA}/opnexus-updater@{D1}" ""', entries=[entry(GHCR_USER), entry(GITEA)])
        self.assertEqual(r.stdout.strip(), entry(GITEA))

    def test_the_pull_is_not_quiet_because_the_digest_comes_from_its_output(self):
        self.cli(entries=[entry(GITEA)], pull_out=f"Digest: {D1}")
        self.assertIn(f"pull {GITEA}/opnexus-updater:9.9.9", self.calls()); self.assertFalse([c for c in self.calls() if c.startswith("pull -q")])

    def test_only_the_requested_tag_is_removed_when_the_image_is_not_kept(self):
        self.cli(entries=[entry(GHCR_USER)], pull_out=f"Digest: {D1}")   # überspringen -> rmi nur der angeforderte Name
        self.assertEqual([c for c in self.calls() if c.startswith("rmi")], [f"rmi {GITEA}/opnexus-updater:9.9.9"])

    def test_a_failed_pull_still_continues_with_the_present_files(self):
        r = self.cli(pull_rc=1)
        self.assertIn("nicht ladbar", r.stdout); self.assertIn("CONTINUED", r.stdout)

    # ── Codebase ──
    def test_no_code_picks_a_repo_digest_by_position(self):
        pattern = re.compile(r"index\s+\.RepoDigests\s+\d|RepoDigests\[\d|RepoDigests\s+0|\.RepoDigests\s*\|\s*head")
        offenders = []
        for path in [ROOT / "updater/opnexus", *sorted((ROOT / "scripts").glob("*.sh")), *sorted((ROOT / "scripts").glob("*.py")), ROOT / "updater/server.py"]:
            if not path.exists() or path.is_symlink():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
