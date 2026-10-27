"""Die Release-Pipeline mit zwei Registries (GHCR + Gitea): Versions-Tags, Kopieren samt Signatur, Floating-Tags, Prüfung, Bau, Bundle, Ablauf.
Die Skripte laufen echt, `docker` und `cosign` sind Attrappen mit einem kleinen Registry-Zustand (JSON); es wird nichts gebaut,
gepusht oder signiert. Fehler lassen sich einschleusen (FAKE_*): ein bestimmter Kopiervorgang, der n-te Schreibzugriff, ein
nicht lesbares Tag, ein Netzfehler bei cosign. Der Lauf gegen echte Registries steht in docs/MAINTAINER.md.
Run: cd updater && python3 -m unittest test_release_pipeline"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
GHCR = "ghcr.io/opnexus-dev"
GITEA = "git.opnexus.dev/opnexus"
SERVICES = ("backend", "frontend", "proxy", "updater")
COMPOSE_OK = "services:\n  backend:\n    image: ${{OPNEXUS_IMAGE_REPO:-{repo}}}/opnexus-backend:${{OPNEXUS_VERSION:-{version}}}\n"
SECRETS = ("topsecretvalue99", "SUPERSECRETBEARER123", "ghp_ABCDEFGHIJKLMNOP1234567890", "pw-in-url-77")

FAKE_DOCKER = r'''#!__PY__
import json, os, shutil, sys
state_path = os.environ["FAKE_STATE"]
st = json.load(open(state_path))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("docker " + " ".join(args) + "\n")
def save():
    json.dump(st, open(state_path, "w"))
def split(ref):
    if "@" in ref:
        img, dg = ref.split("@", 1)
        return img, None, dg
    img, tag = ref.rsplit(":", 1)
    return img, tag, None
def resolve(ref):
    img, tag, dg = split(ref)
    if dg is not None:
        return dg if any(v == dg for k, v in st["tags"].items() if k.rsplit(":", 1)[0] == img) else None
    return st["tags"].get(f"{img}:{tag}")
def leak():
    # So sieht ein Fehler echter Werkzeuge aus: mit Zugangsdaten in der Meldung, die nie ausgegeben werden dürfen.
    return ("unexpected status 403 Forbidden; Authorization: Bearer SUPERSECRETBEARER123; token=ghp_ABCDEFGHIJKLMNOP1234567890; "
            "https://user:pw-in-url-77@ghcr.io/v2/; " + os.environ.get("GHCR_TOKEN", ""))
if args[:3] == ["buildx", "imagetools", "inspect"]:
    ref = args[3]
    if any(s and s in ref for s in os.environ.get("FAKE_INSPECT_ERROR", "").split(",")):
        print("ERROR: failed to do request: Head https://ghcr.io/v2/: dial tcp: connection refused", file=sys.stderr)
        sys.exit(1)
    d = resolve(ref)
    if d is None:
        print(f"ERROR: {ref}: not found", file=sys.stderr)
        sys.exit(1)
    if "--format" in args:
        print(json.dumps(d))
        sys.exit(0)
    print("Digest: " + d)
    for p in st["platforms"].get(d, []):
        print("  Platform: " + p)
    sys.exit(0)
if args[:3] == ["buildx", "imagetools", "create"]:
    st["n"] = st.get("n", 0) + 1
    n = st["n"]
    save()
    dsts = [args[i + 1] for i, a in enumerate(args) if a == "-t"]
    d = resolve(args[-1])
    if d is None:
        print(f"ERROR: {args[-1]}: not found", file=sys.stderr)
        sys.exit(1)
    if str(n) == os.environ.get("FAKE_FAIL_NTH") or (os.environ.get("FAKE_FAIL_FROM_NTH") and n >= int(os.environ["FAKE_FAIL_FROM_NTH"])):
        print("ERROR: failed commit on ref: " + leak(), file=sys.stderr)
        sys.exit(1)
    for dst in dsts:
        img, tag = dst.rsplit(":", 1)
        if any(s and s in dst for s in os.environ.get("FAKE_FAIL_CREATE", "").split(",")):
            print("ERROR: failed commit on ref: " + leak(), file=sys.stderr)
            sys.exit(1)
        if os.environ.get("FAKE_DROP_SIG_COPY") and tag.startswith("sha256-"):
            continue
        if os.environ.get("FAKE_REJECT_SIG_FIRST") and tag.startswith("sha256-") and not any(v == "sha256:" + tag[7:] for k, v in st["tags"].items() if k.rsplit(":", 1)[0] == img):
            print("ERROR: manifest invalid: subject does not exist", file=sys.stderr)
            sys.exit(1)
        st["tags"][dst] = d
    save()
    sys.exit(0)
if args[:1] == ["pull"]:
    sys.exit(1 if os.environ.get("FAKE_FAIL_PULL") else 0)
if args[:1] == ["run"] and "--entrypoint" in args and "cat" in args:
    image = args[args.index("cat") + 1]
    d = resolve(image)
    text = st["compose"].get(d or "")
    if text is None:
        sys.exit(1)
    sys.stdout.write(text)
    sys.exit(0)
if args[:1] == ["build"] or args[:2] == ["buildx", "build"]:
    ctx = args[-1]
    if os.path.exists(os.path.join(ctx, "docker-compose.release.yml")):
        shutil.copy(os.path.join(ctx, "docker-compose.release.yml"), os.path.join(os.environ["FAKE_OUT"], "compose-release.yml"))
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    img = args[2].rsplit(":", 1)[0]
    entries = os.environ.get("FAKE_REPODIGESTS")
    entries = entries.replace("{IMG}", img).split(";") if entries else [img + "@sha256:" + "c" * 64]
    if any("range .RepoDigests" in a for a in args):
        print("\n".join(entries))
    else:
        print(json.dumps(entries))
    sys.exit(0)
sys.exit(0)
'''

FAKE_COSIGN = r'''#!__PY__
import json, os, sys
st = json.load(open(os.environ["FAKE_STATE"]))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("cosign " + " ".join(args) + "\n")
ref = args[-1]
if os.environ.get("FAKE_COSIGN_RC"):
    print("Error: Get https://ghcr.io/v2/: dial tcp: i/o timeout", file=sys.stderr)
    sys.exit(int(os.environ["FAKE_COSIGN_RC"]))
if os.environ.get("FAKE_COSIGN_NETERR_FOR") and os.environ["FAKE_COSIGN_NETERR_FOR"] in ref:
    print("Error: Get https://ghcr.io/v2/: dial tcp: i/o timeout", file=sys.stderr)
    sys.exit(1)
keyid = open(args[args.index("--key") + 1]).read().strip()
def resolve(ref):
    if "@" in ref:
        return ref.split("@", 1)
    img, tag = ref.rsplit(":", 1)
    return img, st["tags"].get(f"{img}:{tag}")
img, dg = resolve(ref)
if dg is None:
    print("Error: image tag not found: MANIFEST_UNKNOWN", file=sys.stderr)
    sys.exit(1)
sigtag = f"{img}:sha256-{dg.split(':', 1)[1]}"
if args[0] == "verify":
    got = st["tags"].get(sigtag) or st["tags"].get(sigtag + ".sig")
    if got == "sig:" + keyid:
        sys.exit(0)
    if got is None:
        print("Error: no signatures found", file=sys.stderr)
    else:
        print("Error: no matching attestations: failed to verify signature: accepted signatures do not match threshold, Found: 0, Expected 1", file=sys.stderr)
    sys.exit(1)
if args[0] == "sign":
    st["tags"][sigtag] = "sig:" + keyid
    json.dump(st, open(os.environ["FAKE_STATE"], "w"))
    sys.exit(0)
sys.exit(0)
'''


def _exe(path: Path, text: str):
    path.write_text(text.replace("__PY__", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _git(cwd: Path, *a: str):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], cwd=cwd, check=True, capture_output=True)


class Sandbox(unittest.TestCase):
    """Ein Wegwerf-Repo mit den echten Skripten, Attrappen vor dem PATH und einem leeren Registry-Zustand."""

    FILES = ("release_common.sh", "copy_release_images.sh", "verify_release_images.sh", "promote_floating_tags.sh", "build_release_images.sh",
             "release_compose.sh", "check_version.sh", "make_release_bundle.sh")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        for name in self.FILES:
            shutil.copy(SCRIPTS / name, self.repo / "scripts" / name)
        shutil.copy(ROOT / "updater" / "opnexus", self.repo / "scripts" / "opnexus")
        for name in ("docker-compose.yml", ".env.example", "LICENSE"):
            shutil.copy(ROOT / name, self.repo / name)
        (self.repo / "cosign.pub").write_text("KEY-A")
        (self.repo / "backend/app").mkdir(parents=True)
        (self.repo / "frontend/lib").mkdir(parents=True)
        for svc in SERVICES:
            (self.repo / svc).mkdir(exist_ok=True)
            (self.repo / svc / "Dockerfile").write_text("FROM scratch\n")
        subprocess.run(["git", "init", "-q", "-b", "master", str(self.repo)], check=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        _exe(self.bin / "docker", FAKE_DOCKER)
        _exe(self.bin / "cosign", FAKE_COSIGN)
        self.state_path = self.tmp / "state.json"
        self.log = self.tmp / "calls.log"
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.state = {"tags": {}, "platforms": {}, "compose": {}}
        self.flush()
        (self.tmp / "key.priv").write_text("KEY-A")

    # ── Zustand ──
    def flush(self):
        self.state_path.write_text(json.dumps(self.state))

    def reload(self):
        self.state = json.loads(self.state_path.read_text())
        return self.state

    def add_image(self, repo, svc, version, *, seed=None, signed=True, platforms=("linux/amd64", "linux/arm64"), compose=None, sig_key="KEY-A"):
        d = _digest(seed or f"{svc}-{version}")
        self.state["tags"][f"{repo}/opnexus-{svc}:{version}"] = d
        self.state["platforms"][d] = list(platforms)
        sigtag = f"{repo}/opnexus-{svc}:sha256-{d.split(':', 1)[1]}"
        if signed:
            self.state["tags"][sigtag] = "sig:" + sig_key
        else:
            self.state["tags"].pop(sigtag, None)
        if compose is not None:
            self.state["compose"][d] = compose
        self.flush()
        return d

    def add_release(self, repo, version, **kw):
        out = {}
        for svc in SERVICES:
            kw2 = dict(kw)
            if svc == "updater" and "compose" not in kw2:
                kw2["compose"] = COMPOSE_OK.format(repo=GHCR, version=version)
            out[svc] = self.add_image(repo, svc, version, **kw2)
        return out

    def tag_git(self, *versions):
        if not (self.repo / ".git" / "refs" / "heads" / "master").exists():
            _git(self.repo, "commit", "--allow-empty", "-m", "x")
        for v in versions:
            _git(self.repo, "tag", f"v{v}")

    def tags_of(self, repo, svc="backend"):
        self.reload()
        pre = f"{repo}/opnexus-{svc}:"
        return {k[len(pre):]: v for k, v in self.state["tags"].items() if k.startswith(pre) and not k[len(pre):].startswith("sha256-")}

    def sig_of(self, repo, svc, digest):
        return self.reload()["tags"].get(f"{repo}/opnexus-{svc}:sha256-{digest.split(':', 1)[1]}")

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def creates(self):
        return [c for c in self.calls() if c.startswith("docker buildx imagetools create")]

    def run_script(self, name, *args, env=None, **kw):
        e = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp)}
        for k in ("OPNEXUS_PRIMARY_REPO", "OPNEXUS_MIRROR_REPO", "OPNEXUS_BUILD_REPO", "OPNEXUS_IMAGE_REPO", "OPNEXUS_COMPOSE_REPO",
                  "OPNEXUS_COSIGN_PUB", "OPNEXUS_SIGN", "COSIGN_KEY", "GHCR_TOKEN") + tuple(k for k in os.environ if k.startswith("FAKE_")):
            e.pop(k, None)
        e.update(env or {})
        e["FAKE_STATE"], e["FAKE_LOG"], e["FAKE_OUT"] = str(self.state_path), str(self.log), str(self.out)
        result = subprocess.run(["bash", str(self.repo / "scripts" / name), *args], cwd=self.repo, env=e, capture_output=True, text=True, timeout=60, **kw)
        self.reload()   # die Attrappen haben den Zustand verändert
        return result

    def assertNoSecrets(self, r):
        for s in SECRETS:
            self.assertNotIn(s, r.stdout + r.stderr)


class ReleaseTags(Sandbox):
    def tags(self, version):
        self.tag_git(*self.git_versions)
        r = subprocess.run(["bash", "-c", f". scripts/release_common.sh; release_tags {version}"], cwd=self.repo, capture_output=True, text=True,
                           env={**os.environ, "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split()

    git_versions = ("1.34.0", "1.34.1", "1.35.0")

    def test_the_newest_stable_version_gets_every_tag(self):
        self.assertEqual(self.tags("1.35.0"), ["1.35.0", "1.35", "1", "latest"])

    def test_a_backfilled_older_patch_never_moves_latest_or_the_major_tag(self):
        self.assertEqual(self.tags("1.34.1"), ["1.34.1", "1.34"])

    def test_an_older_patch_in_its_own_line_gets_only_the_full_tag(self):
        self.assertEqual(self.tags("1.34.0"), ["1.34.0"])

    def test_a_prerelease_only_gets_its_full_tag(self):
        self.assertEqual(self.tags("1.36.0-rc1"), ["1.36.0-rc1"])

    def test_a_prerelease_never_counts_as_a_newer_version(self):
        self.git_versions = ("1.35.0",)
        self.tag_git("1.36.0-rc1")
        self.assertEqual(self.tags("1.35.0"), ["1.35.0", "1.35", "1", "latest"])

    def test_a_new_major_version_gets_all_tags(self):
        self.assertEqual(self.tags("2.0.0"), ["2.0.0", "2.0", "2", "latest"])

    def test_a_malformed_tag_is_refused(self):
        r = subprocess.run(["bash", "-c", ". scripts/release_common.sh; release_version_of_tag v1.2"], cwd=self.repo, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


class Diagnostics(Sandbox):
    def helper(self, body, env=None):
        return subprocess.run(["bash", "-c", f". scripts/release_common.sh; {body}"], cwd=self.repo, capture_output=True, text=True,
                              env={**os.environ, "PATH": "/usr/bin:/bin", **(env or {})})

    def fake_env(self, **extra):
        return {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_STATE": str(self.state_path), "FAKE_LOG": str(self.log),
                "OPNEXUS_COSIGN_PUB": str(self.repo / "cosign.pub"), **extra}

    def test_credentials_are_removed_from_messages(self):
        text = ("Authorization: Bearer SUPERSECRETBEARER123\nGet \"https://user:pw-in-url-77@ghcr.io/v2/\": denied\n"
                "token=ghp_ABCDEFGHIJKLMNOP1234567890 password: hunter22\nmein geheimer topsecretvalue99 ok\nnormale Zeile bleibt\n")
        r = self.helper(f"printf '%s' '{text}' | release_redact", env={"GHCR_TOKEN": "topsecretvalue99"})
        for s in SECRETS + ("hunter22",):
            self.assertNotIn(s, r.stdout)
        self.assertIn("normale Zeile bleibt", r.stdout)
        self.assertIn("ghcr.io/v2/", r.stdout)   # Host und Pfad bleiben lesbar

    def test_a_failed_step_names_service_registry_and_step_and_keeps_the_tools_output(self):
        r = self.helper("release_step backend ghcr.io/x 'Image kopieren' sh -c 'echo \"boom: unexpected status 403\" >&2; exit 7'")
        self.assertNotEqual(r.returncode, 0)
        for part in ("Dienst: backend", "Registry: ghcr.io/x", "Schritt: Image kopieren", "Exit-Code: 7", "unexpected status 403"):
            self.assertIn(part, r.stderr)

    def test_a_successful_step_is_silent(self):
        r = self.helper("release_step backend ghcr.io/x 'Schritt' true")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_a_missing_tag_an_error_and_an_existing_one_are_told_apart(self):
        self.add_image(GITEA, "backend", "1.0.0")
        probe = ('for ref in %s/opnexus-backend:1.0.0 %s/opnexus-backend:9.9.9; do rc=0; release_tag_digest "$ref" out || rc=$?; echo "$rc"; done'
                 % (GITEA, GITEA))
        r = subprocess.run(["bash", "-c", f". scripts/release_common.sh; {probe}"], cwd=self.repo, capture_output=True, text=True, env=self.fake_env())
        self.assertEqual(r.stdout.split(), ["0", "1"])
        r = subprocess.run(["bash", "-c", f". scripts/release_common.sh; rc=0; release_tag_digest {GITEA}/opnexus-backend:1.0.0 out || rc=$?; echo $rc"],
                           cwd=self.repo, capture_output=True, text=True, env=self.fake_env(FAKE_INSPECT_ERROR="opnexus-backend"))
        self.assertEqual(r.stdout.split()[-1], "2")   # Netzfehler ist weder "gefunden" noch "nicht vorhanden"

    def test_the_signature_state_is_never_none_for_a_network_error(self):
        d = self.add_image(GITEA, "backend", "1.0.0")

        def state(**extra):
            r = subprocess.run(["bash", "-c", f". scripts/release_common.sh; release_signature_state {GITEA}/opnexus-backend@{d}"], cwd=self.repo,
                               capture_output=True, text=True, env=self.fake_env(**extra))
            return r.stdout.strip()
        self.assertEqual(state(), "valid")
        self.assertEqual(state(FAKE_COSIGN_NETERR_FOR="opnexus-backend"), "unknown")
        del self.state["tags"][f"{GITEA}/opnexus-backend:sha256-{d.split(':', 1)[1]}"]
        self.flush()
        self.assertEqual(state(), "none")
        self.add_image(GITEA, "backend", "1.0.0", sig_key="KEY-B")
        self.assertEqual(state(), "invalid")


class CopyImages(Sandbox):
    def setUp(self):
        super().setUp()
        self.tag_git("1.34.0", "1.35.0")
        self.src = self.add_release(GITEA, "1.35.0")

    def copy(self, *a, **kw):
        return self.run_script("copy_release_images.sh", "v1.35.0", *a, **kw)

    def assertComplete(self, repo=GHCR):
        for svc in SERVICES:
            self.assertEqual(self.tags_of(repo, svc).get("1.35.0"), self.src[svc], svc)
            self.assertEqual(self.sig_of(repo, svc, self.src[svc]), "sig:KEY-A", svc)

    # ── Grundverhalten ──
    def test_the_default_direction_is_gitea_to_ghcr(self):
        r = self.copy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{GITEA} -> {GHCR}", r.stdout)
        self.assertComplete()
        self.assertIn("VOLLSTÄNDIG", r.stdout)

    def test_the_signature_is_copied_before_the_image_of_every_service(self):
        self.assertEqual(self.copy().returncode, 0)
        creates = self.creates()
        for svc in SERVICES:
            sig = next(i for i, c in enumerate(creates) if f"{GHCR}/opnexus-{svc}:sha256-" in c)
            img = next(i for i, c in enumerate(creates) if f"{GHCR}/opnexus-{svc}:1.35.0 " in c)
            self.assertLess(sig, img, svc)

    def test_no_floating_tag_is_ever_set_by_the_copy(self):
        self.assertEqual(self.copy().returncode, 0)
        for svc in SERVICES:
            self.assertEqual(set(self.tags_of(GHCR, svc)), {"1.35.0"})
        self.assertFalse([c for c in self.creates() if any(f":{t} " in c for t in ("latest", "1", "1.35"))])

    def test_a_dry_run_checks_everything_and_writes_nothing(self):
        r = self.copy("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.tags_of(GHCR), {})
        self.assertIn("Trockenlauf", r.stdout)

    def test_copying_twice_is_fine_and_changes_nothing(self):
        self.assertEqual(self.copy().returncode, 0)
        before = dict(self.reload()["tags"])
        r = self.copy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("schon vorhanden", r.stdout)
        self.assertEqual(self.reload()["tags"], before)

    def test_a_prerelease_is_copied_with_its_full_tag(self):
        self.tag_git("1.36.0-rc1")
        self.add_release(GITEA, "1.36.0-rc1")
        r = self.run_script("copy_release_images.sh", "v1.36.0-rc1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(set(self.tags_of(GHCR)), {"1.36.0-rc1"})

    # ── Quelle ──
    def test_an_unsigned_source_is_never_copied(self):
        self.add_release(GITEA, "1.35.0", signed=False)
        r = self.copy()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Signatur", r.stderr)
        self.assertEqual(self.creates(), [])

    def test_one_bad_service_stops_the_whole_copy_before_anything_is_written(self):
        self.add_image(GITEA, "proxy", "1.35.0", signed=False)
        r = self.copy()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.tags_of(GHCR, "backend"), {})

    def test_a_signature_by_another_key_is_refused(self):
        self.add_image(GITEA, "backend", "1.35.0", sig_key="KEY-B")
        r = self.copy()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.creates(), [])

    def test_a_source_without_both_platforms_is_refused(self):
        self.add_image(GITEA, "frontend", "1.35.0", platforms=("linux/amd64",))
        r = self.copy()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Plattformen", r.stderr)
        self.assertEqual(self.creates(), [])

    def test_a_missing_source_is_refused(self):
        r = self.run_script("copy_release_images.sh", "v1.34.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("gibt es nicht", r.stderr)
        self.assertEqual(self.creates(), [])

    def test_an_unreadable_source_registry_is_reported_as_such(self):
        r = self.copy(env={"FAKE_INSPECT_ERROR": GITEA})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht lesbar", r.stderr)
        self.assertIn("connection refused", r.stderr)
        self.assertEqual(self.creates(), [])

    def test_a_signature_in_the_old_scheme_is_refused_instead_of_corrupting_it(self):
        d = self.src["backend"]
        self.state["tags"].pop(f"{GITEA}/opnexus-backend:sha256-{d.split(':', 1)[1]}")
        self.state["tags"][f"{GITEA}/opnexus-backend:sha256-{d.split(':', 1)[1]}.sig"] = "sig:KEY-A"
        self.flush()
        r = self.copy()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cosign 3", r.stderr)
        self.assertEqual(self.creates(), [])

    def test_same_source_and_target_is_refused(self):
        self.assertNotEqual(self.copy("--from", GHCR, "--to", GHCR).returncode, 0)

    def test_without_cosign_nothing_is_copied(self):
        (self.bin / "cosign").unlink()
        r = self.copy()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.creates(), [])

    # ── Schutz vorhandener Versions-Tags im Ziel ──
    def test_a_different_digest_without_a_valid_signature_needs_force(self):
        other = self.add_image(GHCR, "backend", "1.35.0", seed="unsigned-github-build", signed=False)
        r = self.copy()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--force", r.stderr)
        self.assertEqual(self.tags_of(GHCR)["1.35.0"], other)
        self.assertEqual(self.creates(), [])

    def test_force_replaces_a_tag_without_a_valid_signature(self):
        self.add_image(GHCR, "backend", "1.35.0", seed="unsigned-github-build", signed=False)
        r = self.copy("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ACHTUNG --force", r.stdout)
        self.assertComplete()

    def test_force_replaces_a_tag_whose_signature_is_not_from_our_key(self):
        self.add_image(GHCR, "backend", "1.35.0", seed="foreign-build", sig_key="KEY-B")
        self.assertNotEqual(self.copy().returncode, 0)
        r = self.copy("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertComplete()

    def test_a_validly_signed_tag_with_another_digest_is_never_replaced_not_even_with_force(self):
        other = self.add_image(GHCR, "frontend", "1.35.0", seed="a-different-signed-release", signed=True)
        for args in ((), ("--force",)):
            r = self.copy(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("gültig signiert", r.stderr)
            self.assertIn("nie überschrieben", r.stderr)
            self.assertEqual(self.tags_of(GHCR, "frontend")["1.35.0"], other)
            self.assertEqual(self.creates(), [], "es darf nichts geschrieben werden, auch nicht für die anderen Dienste")
            self.assertEqual(self.tags_of(GHCR, "backend"), {})

    def test_an_undecidable_signature_state_protects_the_tag_too(self):
        other = self.add_image(GHCR, "proxy", "1.35.0", seed="unsure", signed=False)
        r = self.copy("--force", env={"FAKE_COSIGN_NETERR_FOR": f"{GHCR}/opnexus-proxy@"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht beurteilen", r.stderr)
        self.assertEqual(self.tags_of(GHCR, "proxy")["1.35.0"], other)
        self.assertEqual(self.creates(), [])

    def test_an_unreadable_target_is_not_taken_for_an_empty_one(self):
        r = self.copy(env={"FAKE_INSPECT_ERROR": GHCR})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht feststellbar", r.stderr)
        self.assertEqual(self.creates(), [])

    def test_assume_absent_is_the_explicit_way_for_a_brand_new_package(self):
        r = self.copy("--assume-absent", env={"FAKE_INSPECT_ERROR": f"{GHCR}/opnexus-backend:1.35.0"})
        self.assertIn("--assume-absent", r.stdout)   # ohne die Erklärung bricht derselbe Fall ab (Test davor)

    # ── Abbruch und Wiederanlauf ──
    def test_an_abort_between_signature_and_image_leaves_a_harmless_signature_and_resumes(self):
        r = self.copy(env={"FAKE_FAIL_CREATE": f"{GHCR}/opnexus-frontend:1.35.0"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.tags_of(GHCR, "backend")["1.35.0"], self.src["backend"])           # fertig
        self.assertEqual(self.sig_of(GHCR, "frontend", self.src["frontend"]), "sig:KEY-A")         # nur die Signatur ...
        self.assertNotIn("1.35.0", self.tags_of(GHCR, "frontend"))                                 # ... kein Versions-Tag
        for svc in ("proxy", "updater"):
            self.assertEqual(self.tags_of(GHCR, svc), {})
        for part in ("Dienst: frontend", f"Registry: {GHCR}", "Schritt: Image kopieren"):
            self.assertIn(part, r.stderr)
        self.assertIn("Derselbe Aufruf setzt fort", r.stderr)
        self.assertNotIn("VOLLSTÄNDIG", r.stdout)
        r = self.copy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertComplete()

    def test_resuming_after_a_partial_copy_finishes_the_rest_and_keeps_the_finished(self):
        r = self.copy(env={"FAKE_FAIL_CREATE": f"{GHCR}/opnexus-proxy:1.35.0"})
        self.assertNotEqual(r.returncode, 0)
        done = {svc: self.tags_of(GHCR, svc).get("1.35.0") for svc in SERVICES}
        self.assertEqual((done["backend"], done["frontend"], done["proxy"], done["updater"]), (self.src["backend"], self.src["frontend"], None, None))
        self.assertIn("Fertig: backend frontend", r.stderr)
        r = self.copy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("schon vorhanden", r.stdout)
        self.assertComplete()

    def test_when_the_registry_refuses_a_signature_before_the_image_it_falls_back_with_a_notice(self):
        r = self.copy(env={"FAKE_REJECT_SIG_FIRST": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nimmt die Signatur vor dem Image nicht an", r.stdout)
        self.assertComplete()
        creates = self.creates()
        img = next(i for i, c in enumerate(creates) if f"{GHCR}/opnexus-backend:1.35.0 " in c)
        sig_after = [i for i, c in enumerate(creates) if f"{GHCR}/opnexus-backend:sha256-" in c and i > img]
        self.assertTrue(sig_after)

    def test_a_tag_without_signature_after_a_failed_fallback_is_reported_and_repaired_by_the_next_run(self):
        r = self.copy(env={"FAKE_FAIL_CREATE": f"{GHCR}/opnexus-backend:sha256-"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("OHNE Signatur", r.stderr)
        self.assertIn("Schritt: Signatur kopieren (nach dem Image)", r.stderr)
        self.assertEqual(self.tags_of(GHCR, "backend")["1.35.0"], self.src["backend"])   # das Tag existiert ...
        self.assertIsNone(self.sig_of(GHCR, "backend", self.src["backend"]))               # ... ohne Signatur: gilt als NICHT veröffentlicht
        self.assertNotIn("VOLLSTÄNDIG", r.stdout)
        r = self.copy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertComplete()

    def test_an_unreadable_signature_after_copying_is_an_error_and_nothing_is_resigned_silently(self):
        r = self.copy(env={"FAKE_DROP_SIG_COPY": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nach dem Kopieren ungültig", r.stderr)
        self.assertIn("NICHT veröffentlicht", r.stderr)
        self.assertIn("no signatures found", r.stderr)   # die Ausgabe von cosign bleibt sichtbar
        self.assertFalse([c for c in self.calls() if c.startswith("cosign sign")])

    def test_resigning_with_the_same_key_is_opt_in_and_still_verified(self):
        env = {"FAKE_DROP_SIG_COPY": "1", "OPNEXUS_RESIGN_ON_COPY_FAILURE": "1", "COSIGN_KEY": str(self.tmp / "key.priv")}
        r = self.copy(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len([c for c in self.calls() if c.startswith("cosign sign")]), 4)
        wrong = self.tmp / "key.wrong"
        wrong.write_text("KEY-B")
        self.state["tags"] = {k: v for k, v in self.reload()["tags"].items() if not k.startswith(GHCR)}
        self.flush()
        r = self.copy(env={**env, "COSIGN_KEY": str(wrong)})
        self.assertNotEqual(r.returncode, 0)   # ein Schlüssel, der nicht zu cosign.pub passt, besteht die Prüfung nicht

    # ── Fehlerdiagnose ──
    def test_errors_name_service_registry_and_step_and_leak_no_credentials(self):
        r = self.copy(env={"FAKE_FAIL_CREATE": f"{GHCR}/opnexus-frontend:1.35.0", "GHCR_TOKEN": "topsecretvalue99"})
        self.assertNotEqual(r.returncode, 0)
        for part in ("Dienst: frontend", f"Registry: {GHCR}", "Schritt: Image kopieren", "Exit-Code: 1", "403 Forbidden"):
            self.assertIn(part, r.stderr)
        self.assertNoSecrets(r)

    def test_a_failed_signature_step_shows_the_tools_message(self):
        r = self.copy(env={"FAKE_FAIL_CREATE": f"{GHCR}/opnexus-backend:sha256-,{GHCR}/opnexus-backend:1.35.0"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Schritt: Signatur kopieren (vor dem Image)", r.stderr)
        self.assertIn("Dienst: backend", r.stderr)


class PromoteFloatingTags(Sandbox):
    def setUp(self):
        super().setUp()
        self.tag_git("1.34.0", "1.35.0")
        self.old = {r: self.add_release(r, "1.34.0") for r in (GHCR, GITEA)}
        self.src = self.add_release(GITEA, "1.35.0")
        self.assertEqual(self.run_script("copy_release_images.sh", "v1.35.0").returncode, 0)
        # Stand vor dem Release: "1" und "latest" zeigen auf 1.34.0 (X.Y = 1.35 ist neu)
        for r in (GHCR, GITEA):
            for svc in SERVICES:
                for t in ("1", "latest"):
                    self.state["tags"][f"{r}/opnexus-{svc}:{t}"] = self.old[r][svc]
        self.state["n"] = 0   # der Zähler der Attrappe für Schreibzugriffe (FAKE_FAIL_NTH) zählt erst ab hier
        self.flush()
        self.log.unlink()

    def promote(self, *a, **kw):
        return self.run_script("promote_floating_tags.sh", "v1.35.0", *a, **kw)

    def floating(self, repo, svc):
        t = self.tags_of(repo, svc)
        return {k: t.get(k) for k in ("1.35", "1", "latest")}

    def assertAllOld(self):
        for r in (GHCR, GITEA):
            for svc in SERVICES:
                f = self.floating(r, svc)
                self.assertEqual((f["1"], f["latest"]), (self.old[r][svc], self.old[r][svc]), (r, svc))

    def assertAllNew(self):
        for r in (GHCR, GITEA):
            for svc in SERVICES:
                self.assertEqual(set(self.floating(r, svc).values()), {self.src[svc]}, (r, svc))

    def test_everything_valid_sets_the_floating_tags_in_both_registries(self):
        r = self.promote()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertAllNew()
        self.assertIn("Endprüfung", r.stdout)

    def test_the_checks_run_before_the_first_floating_tag_is_touched(self):
        self.promote()
        calls = self.calls()
        first_create = next(i for i, c in enumerate(calls) if c.startswith("docker buildx imagetools create"))
        verifies = [i for i, c in enumerate(calls) if c.startswith("cosign verify")]
        pulls = [i for i, c in enumerate(calls) if c.startswith("docker pull")]
        self.assertTrue(verifies and pulls)
        self.assertLess(min(verifies), first_create)
        self.assertLess(min(pulls), first_create)   # die Release-Compose im Updater-Image wird geprüft, bevor etwas gesetzt wird

    def test_a_dry_run_changes_nothing(self):
        r = self.promote("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.creates(), [])
        self.assertIn("würde setzen", r.stdout)
        self.assertAllOld()

    def test_a_missing_signature_anywhere_blocks_every_floating_tag(self):
        self.add_image(GHCR, "proxy", "1.35.0", seed="proxy-1.35.0", signed=False)   # gleicher Digest, Signatur fehlt nur in GHCR
        r = self.promote()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("KEIN schwebendes Tag", r.stderr)
        self.assertEqual(self.creates(), [])
        self.assertAllOld()

    def test_a_missing_release_compose_blocks_every_floating_tag(self):
        self.state["compose"] = {}
        self.flush()
        r = self.promote()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Release-Compose", r.stderr)
        self.assertEqual(self.creates(), [])
        self.assertAllOld()

    def test_a_release_compose_for_another_version_blocks_the_floating_tags(self):
        d = self.src["updater"]
        self.state["compose"][d] = COMPOSE_OK.format(repo=GHCR, version="1.34.0")
        self.flush()
        self.assertNotEqual(self.promote().returncode, 0)
        self.assertAllOld()

    def test_different_digests_in_the_two_registries_block_the_floating_tags(self):
        self.add_image(GHCR, "frontend", "1.35.0", seed="other-frontend")
        r = self.promote()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.creates(), [])
        self.assertAllOld()

    def test_a_missing_image_in_one_registry_blocks_the_floating_tags(self):
        del self.state["tags"][f"{GHCR}/opnexus-updater:1.35.0"]
        self.flush()
        self.assertNotEqual(self.promote().returncode, 0)
        self.assertEqual(self.creates(), [])

    def test_an_unreadable_floating_tag_stops_before_any_change(self):
        r = self.promote(env={"FAKE_INSPECT_ERROR": "opnexus-proxy:latest"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht lesbar", r.stderr)
        self.assertEqual(self.creates(), [])

    def assertAllOldInGitea(self):
        for svc in SERVICES:
            f = self.floating(GITEA, svc)
            self.assertEqual((f["1"], f["latest"], f["1.35"]), (self.old[GITEA][svc], self.old[GITEA][svc], None), svc)

    def test_a_failure_in_the_middle_rolls_the_moved_tags_back(self):
        # Schreibzugriffe in GHCR: backend (1.35, 1, latest) = 1-3, frontend = 4-6, proxy = 7 (1.35), 8 (1) schlägt fehl
        r = self.promote(env={"FAKE_FAIL_NTH": "8", "GHCR_TOKEN": "topsecretvalue99"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Dienst: proxy", r.stderr)
        self.assertIn("Schritt: Tag 1 setzen", r.stderr)
        self.assertIn("zurückgesetzt", r.stderr)
        for svc in SERVICES:   # "1" und "latest" stehen überall wieder auf der Vorversion
            f = self.floating(GHCR, svc)
            self.assertEqual((f["1"], f["latest"]), (self.old[GHCR][svc], self.old[GHCR][svc]), svc)
        self.assertAllOldInGitea()
        self.assertNotIn("Fertig:", r.stdout)
        self.assertIn("war neu", r.stderr)   # X.Y (1.35) gab es vorher nicht: es lässt sich mit diesem Werkzeug nicht löschen und wird genannt
        self.assertNoSecrets(r)

    def test_a_failed_rollback_names_every_tag_that_could_not_be_restored(self):
        r = self.promote(env={"FAKE_FAIL_FROM_NTH": "8"})   # ab dem 8. Schreibzugriff geht nichts mehr, auch das Zurücksetzen nicht
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht zurückgesetzt", r.stderr)
        self.assertIn("docker buildx imagetools create -t", r.stderr)          # fertiger Befehl zur manuellen Reparatur
        self.assertIn(self.old[GHCR]["backend"], r.stderr)                      # mit dem alten Digest
        self.assertIn("NICHT einheitlich", r.stderr)

    def test_a_repeated_call_after_a_rolled_back_failure_completes(self):
        self.assertNotEqual(self.promote(env={"FAKE_FAIL_NTH": "8"}).returncode, 0)
        r = self.promote()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertAllNew()

    def test_a_repeated_call_when_everything_is_set_writes_nothing(self):
        self.assertEqual(self.promote().returncode, 0)
        self.log.unlink()
        r = self.promote()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.creates(), [])
        self.assertAllNew()

    def test_an_older_version_does_not_move_latest_or_the_major_tag(self):
        self.assertEqual(self.promote().returncode, 0)
        r = self.run_script("promote_floating_tags.sh", "v1.34.0")   # 1.34.0 ist die höchste Version ihrer Reihe: nur "1.34" ist zu setzen
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertAllNew()   # 1.35, 1 und latest bleiben auf 1.35.0
        for repo in (GHCR, GITEA):
            for svc in SERVICES:
                self.assertEqual(self.tags_of(repo, svc)["1.34"], self.old[repo][svc])

    def test_a_version_that_has_a_higher_patch_has_no_floating_tags_at_all(self):
        self.tag_git("1.34.1")
        for repo in (GHCR, GITEA):
            self.add_release(repo, "1.34.1")
        r = self.run_script("promote_floating_tags.sh", "v1.34.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nichts zu tun", r.stdout)
        self.assertEqual(self.creates(), [])

    def test_a_prerelease_sets_no_floating_tag(self):
        self.tag_git("1.36.0-rc1")
        self.add_release(GITEA, "1.36.0-rc1")
        r = self.run_script("promote_floating_tags.sh", "v1.36.0-rc1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.creates(), [])
        self.assertAllOld()

    def test_the_non_atomic_limit_is_documented_in_the_script(self):
        text = (SCRIPTS / "promote_floating_tags.sh").read_text()
        self.assertIn("atomare", text)
        self.assertIn("Mischung", text)


class VerifyImages(Sandbox):
    def setUp(self):
        super().setUp()
        self.tag_git("1.35.0")
        self.add_release(GITEA, "1.35.0")
        self.assertEqual(self.run_script("copy_release_images.sh", "v1.35.0").returncode, 0)
        self.assertEqual(self.run_script("promote_floating_tags.sh", "v1.35.0").returncode, 0)
        self.log.unlink()

    def verify(self, *a, env=None):
        return self.run_script("verify_release_images.sh", "v1.35.0", *a, env=env)

    def test_a_complete_release_passes(self):
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Alles in Ordnung", r.stdout)

    def test_both_registries_are_checked_by_default(self):
        self.verify()
        text = "\n".join(self.calls())
        self.assertIn(f"{GHCR}/opnexus-backend", text)
        self.assertIn(f"{GITEA}/opnexus-backend", text)

    def test_one_registry_can_be_selected(self):
        r = self.verify("--repo", GHCR, "--no-compose")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(f"{GITEA}/opnexus", "\n".join(self.calls()))

    def test_different_digests_in_the_two_registries_fail(self):
        self.add_image(GHCR, "proxy", "1.35.0", seed="other-build")
        r = self.verify()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("weicht", r.stderr)

    def test_a_missing_signature_fails_and_names_service_and_registry(self):
        self.add_image(GHCR, "frontend", "1.35.0", signed=False)
        r = self.verify()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Signatur", r.stderr)
        self.assertIn("Dienst: frontend", r.stderr)
        self.assertIn(f"Registry: {GHCR}", r.stderr)
        self.assertIn("no signatures found", r.stderr)

    def test_a_missing_image_fails(self):
        del self.state["tags"][f"{GHCR}/opnexus-updater:1.35.0"]
        self.flush()
        self.assertNotEqual(self.verify().returncode, 0)

    def test_a_missing_platform_fails(self):
        self.add_image(GHCR, "backend", "1.35.0", platforms=("linux/amd64",))
        r = self.verify()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Plattformen", r.stderr)

    def test_a_floating_tag_on_another_image_fails_but_not_in_exact_only_mode(self):
        self.state["tags"][f"{GHCR}/opnexus-backend:latest"] = _digest("someone-else")
        self.flush()
        r = self.verify()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(":latest", r.stderr)
        self.assertEqual(self.verify("--exact-only").returncode, 0)

    def test_a_missing_release_compose_in_the_updater_image_fails(self):
        self.state["compose"] = {}
        self.flush()
        r = self.verify()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Release-Compose", r.stderr)
        self.assertEqual(self.verify("--no-compose").returncode, 0)

    def test_a_release_compose_for_another_version_fails(self):
        d = self.state["tags"][f"{GHCR}/opnexus-updater:1.35.0"]
        self.state["compose"][d] = COMPOSE_OK.format(repo=GHCR, version="1.34.0")
        self.flush()
        self.assertNotEqual(self.verify().returncode, 0)

    def test_a_release_compose_with_a_build_section_fails(self):
        d = self.state["tags"][f"{GHCR}/opnexus-updater:1.35.0"]
        self.state["compose"][d] = COMPOSE_OK.format(repo=GHCR, version="1.35.0") + "    build:\n      context: .\n"
        self.flush()
        self.assertNotEqual(self.verify().returncode, 0)

    def test_the_signature_is_never_skipped(self):
        r = self.verify(env={"FAKE_COSIGN_RC": "1"})
        self.assertNotEqual(r.returncode, 0)

    def test_a_network_error_is_reported_with_the_tools_message(self):
        r = self.verify(env={"FAKE_COSIGN_RC": "1"})
        self.assertIn("i/o timeout", r.stderr)


class BuildAndBundle(Sandbox):
    def setUp(self):
        super().setUp()
        (self.repo / "backend/app/version.py").write_text('APP_VERSION = "1.2.3"\nAPP_CODENAME = "X"\n')
        (self.repo / "frontend/lib/version.ts").write_text('export const APP_VERSION = "1.2.3";\n')
        (self.repo / "CHANGELOG.md").write_text('# Changelog\n\n## [1.2.3] "X" — 2026-01-01\n')
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "x")
        _git(self.repo, "tag", "v1.2.3")

    def build(self, tag="v1.2.3", *a, env=None):
        return self.run_script("build_release_images.sh", tag, *a, env=env)

    def test_the_default_build_target_is_the_gitea_registry_and_the_compose_default_is_ghcr(self):
        r = self.build("v1.2.3", "--push")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pushes = [c for c in self.calls() if c.startswith("docker push")]
        self.assertTrue(all(f" {GITEA}/opnexus-" in c for c in pushes), pushes)
        self.assertEqual(len(pushes), 4 * 4)   # vier Dienste x (1.2.3, 1.2, 1, latest)
        compose = (self.out / "compose-release.yml").read_text()
        self.assertIn(f"${{OPNEXUS_IMAGE_REPO:-{GHCR}}}", compose)
        self.assertIn("${OPNEXUS_VERSION:-1.2.3}", compose)
        self.assertNotRegex(compose, r"(?m)^\s+build:")

    def test_no_floating_pushes_only_the_full_version_tag(self):
        r = self.build("v1.2.3", "--push", "--no-floating")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pushes = [c for c in self.calls() if c.startswith("docker push")]
        self.assertEqual(len(pushes), 4)
        self.assertTrue(all(c.endswith(":1.2.3") for c in pushes), pushes)
        builds = [c for c in self.calls() if c.startswith("docker build ")]
        self.assertTrue(all(c.count(" -t ") == 1 for c in builds), builds)

    def test_the_release_compose_lands_in_the_updater_build_context(self):
        self.build("v1.2.3")
        builds = [c for c in self.calls() if c.startswith("docker build ")]
        self.assertEqual(len(builds), 4)
        self.assertTrue((self.out / "compose-release.yml").exists())   # die Attrappe kopiert sie nur, wenn sie im Kontext lag

    def test_the_build_target_and_the_compose_default_can_be_chosen(self):
        self.build("v1.2.3", "--push", env={"OPNEXUS_BUILD_REPO": "registry.example/x", "OPNEXUS_COMPOSE_REPO": "other.example/y"})
        self.assertTrue(all(" registry.example/x/opnexus-" in c for c in self.calls() if c.startswith("docker push")))
        self.assertIn("${OPNEXUS_IMAGE_REPO:-other.example/y}", (self.out / "compose-release.yml").read_text())

    def test_a_prerelease_build_pushes_only_the_full_tag(self):
        # Ein rc-Image meldet die endgültige Version (check_version.sh schneidet das Suffix ab).
        (self.repo / "backend/app/version.py").write_text('APP_VERSION = "1.3.0"\nAPP_CODENAME = "X"\n')
        (self.repo / "frontend/lib/version.ts").write_text('export const APP_VERSION = "1.3.0";\n')
        (self.repo / "CHANGELOG.md").write_text('## [1.3.0] "X"\n')
        _git(self.repo, "commit", "-q", "-am", "rc")
        _git(self.repo, "tag", "v1.3.0-rc1")
        self.build("v1.3.0-rc1", "--push")
        pushes = [c for c in self.calls() if c.startswith("docker push")]
        self.assertEqual(len(pushes), 4)
        self.assertTrue(all(c.endswith(":1.3.0-rc1") for c in pushes), pushes)

    def test_signing_uses_the_digest_and_checks_with_the_project_key(self):
        r = self.build("v1.2.3", "--push", env={"OPNEXUS_SIGN": "1", "COSIGN_KEY": str(self.tmp / "key.priv")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        signs = [c for c in self.calls() if c.startswith("cosign sign")]
        verifies = [c for c in self.calls() if c.startswith("cosign verify")]
        self.assertEqual((len(signs), len(verifies)), (4, 4))
        self.assertTrue(all("@sha256:" in c for c in signs))
        self.assertTrue(all("--key cosign.pub" in c for c in verifies))
        self.assertTrue(all("--tlog-upload=false" in c for c in signs))

    def test_a_missing_tag_is_refused(self):
        self.assertNotEqual(self.build("v9.9.9").returncode, 0)

    def test_an_existing_version_tag_in_the_target_is_never_overwritten(self):
        # nur ein Dienst hat das Tag schon (z. B. Rest eines abgebrochenen Pushs): kein Bau, kein Push, keine Signatur
        before = self.add_image(GITEA, "proxy", "1.2.3", seed="old-proxy")
        r = self.build("v1.2.3", "--push", env={"OPNEXUS_SIGN": "1", "COSIGN_KEY": str(self.tmp / "key.priv")})
        self.assertNotEqual(r.returncode, 0); self.assertIn("existiert bereits", r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith(("docker build ", "docker push ", "docker buildx build ", "cosign sign"))], [])
        self.assertEqual(self.tags_of(GITEA, "proxy")["1.2.3"], before)

    def test_an_undeterminable_target_state_builds_nothing(self):
        r = self.build("v1.2.3", "--push", env={"FAKE_INSPECT_ERROR": GITEA})
        self.assertNotEqual(r.returncode, 0); self.assertIn("nicht feststellbar", r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith(("docker build ", "docker push ", "docker buildx build "))], [])

    def test_a_build_without_push_does_not_look_at_the_registry(self):
        self.add_image(GITEA, "backend", "1.2.3", seed="old-backend")
        r = self.build("v1.2.3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c for c in self.calls() if "imagetools" in c], [])

    def sign_env(self, digests):
        return {"OPNEXUS_SIGN": "1", "COSIGN_KEY": str(self.tmp / "key.priv"), "FAKE_REPODIGESTS": digests}

    def test_signing_picks_the_digest_of_the_built_repository_not_the_first_entry(self):
        # fremde Adressen stehen zuerst und tragen andere Digests
        r = self.build("v1.2.3", "--push", env=self.sign_env("ghcr.io/other/opnexus-x@sha256:" + "1" * 64 + ";{IMG}@sha256:" + "2" * 64 + ";mirror.example/{IMG}@sha256:" + "3" * 64))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        signs = [c for c in self.calls() if c.startswith("cosign sign")]
        self.assertEqual(len(signs), 4)
        self.assertTrue(all(c.endswith("@sha256:" + "2" * 64) for c in signs), signs)   # weder der erste Eintrag noch der Teilstring-Treffer "mirror.example/<repo>"

    def test_two_different_digests_for_one_repository_are_not_guessed(self):
        r = self.build("v1.2.3", "--push", env=self.sign_env("{IMG}@sha256:" + "1" * 64 + ";{IMG}@sha256:" + "2" * 64))
        self.assertNotEqual(r.returncode, 0); self.assertIn("nicht eindeutig", r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("cosign sign")], [])

    def test_the_same_digest_twice_is_fine(self):
        r = self.build("v1.2.3", "--push", env=self.sign_env("{IMG}@sha256:" + "2" * 64 + ";{IMG}@sha256:" + "2" * 64))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_entry_for_the_repository_means_no_signature(self):
        r = self.build("v1.2.3", "--push", env=self.sign_env("ghcr.io/other/opnexus-x@sha256:" + "1" * 64))
        self.assertNotEqual(r.returncode, 0); self.assertIn("nicht gefunden", r.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("cosign sign")], [])

    def test_the_bundle_points_new_installations_at_the_given_registry_and_never_ships_an_env_file(self):
        (self.repo / ".env").write_text("OPNEXUS_IMAGE_REPO=git.opnexus.dev/opnexus\nPOSTGRES_PASSWORD=secret\n")
        r = self.run_script("make_release_bundle.sh", "1.2.3", GHCR)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with tarfile.open(self.repo / "dist/opnexus-1.2.3.tar.gz") as t:
            names = t.getnames()
            compose = t.extractfile("opnexus-1.2.3/docker-compose.yml").read().decode()
            example = t.extractfile("opnexus-1.2.3/.env.example").read().decode()
        self.assertIn(f"${{OPNEXUS_IMAGE_REPO:-{GHCR}}}", compose)
        self.assertIn(f"\nOPNEXUS_IMAGE_REPO={GHCR}\n", example)
        self.assertIn("\nOPNEXUS_VERSION=1.2.3\n", example)
        self.assertFalse([n for n in names if n.endswith("/.env")])
        self.assertEqual((self.repo / ".env").read_text(), "OPNEXUS_IMAGE_REPO=git.opnexus.dev/opnexus\nPOSTGRES_PASSWORD=secret\n")   # bestehende .env bleibt


class Wiring(unittest.TestCase):
    """Reihenfolge und Zusicherungen in den Dateien, die sich nicht gefahrlos ausführen lassen (release_all.sh pusht)."""

    @unittest.skipUnless((SCRIPTS / "release_all.sh").exists(), "release_all.sh gehört nicht zum öffentlichen Export")
    def test_release_all_orders_the_steps_so_floating_tags_and_the_feed_come_last(self):
        s = (SCRIPTS / "release_all.sh").read_text()
        order = ["docker login ghcr.io", 'git commit -q -m "Release', 'scripts/publish_public.sh "$tag"', 'scripts/build_release_images.sh "$tag"',
                 'scripts/copy_release_images.sh "$tag"', 'scripts/promote_floating_tags.sh "$tag"', 'scripts/verify_release_images.sh "$tag"',
                 "scripts/make_release_bundle.sh ", 'scripts/make_update_feed.sh "$tag"']
        positions = [s.index(x) for x in order]
        self.assertEqual(positions, sorted(positions), dict(zip(order, positions)))
        self.assertIn('scripts/build_release_images.sh "$tag" --push --multiarch --no-floating', s)   # Gitea bekommt die schwebenden Tags erst nach der Prüfung
        self.assertIn("scripts/make_release_bundle.sh \"$ver\" \"$OPNEXUS_PRIMARY_REPO\"", s)   # neue Installationen: GHCR
        self.assertIn("OPNEXUS_SIGN=1", s)
        for v in ("GHCR_USER", "GHCR_TOKEN", "COSIGN_KEY"):
            self.assertIn(v, s)
        self.assertIn("git push -q origin master", s)
        self.assertGreater(s.index("git push -q origin master"), s.index('scripts/make_update_feed.sh "$tag"'))

    @unittest.skipUnless((SCRIPTS / "publish_public.sh").exists(), "publish_public.sh gehört nicht zum öffentlichen Export")
    def test_the_public_export_keeps_the_workflows_and_the_new_tools(self):
        s = (SCRIPTS / "publish_public.sh").read_text()
        excludes = s[s.index("EXCLUDES=("):s.index("work=\"$(mktemp")]
        for kept in (".github", "release_common.sh", "copy_release_images.sh", "verify_release_images.sh", "promote_floating_tags.sh", "build_release_images.sh"):
            self.assertNotIn(kept, excludes)

    def test_the_github_workflow_cannot_build_or_publish(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        for forbidden in ("build-push-action", "packages: write", "docker/login-action", "push:", "REGISTRY_TOKEN", "GITHUB_TOKEN"):
            self.assertNotIn(forbidden, text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn("scripts/verify_release_images.sh", text)

    def test_every_release_script_is_valid_bash(self):
        for name in ("release_common.sh", "copy_release_images.sh", "verify_release_images.sh", "promote_floating_tags.sh", "build_release_images.sh",
                     "make_release_bundle.sh", "release_compose.sh"):
            r = subprocess.run(["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
