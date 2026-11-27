"""`opnexus cert status|install|remove` (web certificate of the proxy).
Runs the real shell functions with a fake `docker` that imitates the proxy's volume (a directory) and the certificate the proxy would serve
(chosen from OPNEXUS_WEB_CERT in the .env, like proxy/docker-entrypoint.sh does); openssl on the host is real.
Run: cd updater && python3 -m unittest test_cli_cert"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("OPNEXUS_LANG", "de")
SCRIPT = Path(__file__).with_name("opnexus").resolve()

FAKE_DOCKER = r'''#!/usr/bin/env python3
import os, subprocess, sys, tarfile, io
vol = os.environ["FAKE_VOL"]                    # stands for /data of the proxy container
web = os.path.join(vol, "opnexus-web")
log = os.environ["FAKE_LOG"]
args = sys.argv[1:]
data = b"" if sys.stdin.isatty() else sys.stdin.buffer.read()
open(log, "a").write("docker " + " ".join(args)[:300] + "\n")
AUTO_FP = "sha256 Fingerprint=AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA:AA"

def fp(path):
    out = subprocess.run(["openssl", "x509", "-in", path, "-noout", "-fingerprint", "-sha256"], capture_output=True, text=True).stdout.strip()
    return out.replace("SHA256 Fingerprint", "sha256 Fingerprint")

def env_mode():
    for line in open(".env"):
        if line.startswith("OPNEXUS_WEB_CERT="):
            return line.strip().split("=", 1)[1]
    return "auto"

def served():
    crt, key = os.path.join(web, "custom.crt"), os.path.join(web, "custom.key")
    broken = os.environ.get("FAKE_PROXY_BROKEN") == "1"
    if env_mode() == "custom" and os.path.exists(crt) and os.path.exists(key) and not broken:
        return fp(crt), "custom-subject"
    return AUTO_FP, "OPNexus"

rest = args[1:] if args[:1] == ["compose"] else args
if rest[:1] == ["config"]:
    if os.environ.get("FAKE_COMPOSE_OLD") != "1":
        print("services:\n  proxy:\n    environment:\n      OPNEXUS_WEB_CERT: auto")
    sys.exit(0)
if rest[:1] == ["ps"]:
    print("cid123"); sys.exit(0)
if rest[:1] == ["up"]:
    sys.exit(0)
if rest[:1] == ["exec"]:
    script = rest[-1]
    if "s_client" in script:
        f, subj = served()
        print(f); print("subject=CN=" + subj); print("issuer=CN=Test CA"); print("notAfter=Jan  1 00:00:00 2030 GMT")
        print("X509v3 Subject Alternative Name:"); print("    IP Address:10.0.0.5, DNS:opnexus.lan")
        sys.exit(0)
    if "for f in custom web" in script:
        c = os.path.join(web, "custom.crt")
        if os.path.exists(c):
            print("custom " + fp(c).split("=", 1)[1])
        sys.exit(0)
if rest[:1] == ["run"]:
    script = rest[-1]
    entry = "tar" if "--entrypoint" in rest and rest[rest.index("--entrypoint") + 1] == "tar" else "sh"
    if entry == "tar":                                   # tar -C DIR -cf - files...
        d = rest[rest.index("-C") + 1]
        names = rest[rest.index("-cf") + 2:]
        sub = os.path.join(vol, d.replace("/data/", "", 1))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for n in names:
                if os.path.exists(os.path.join(sub, n)):
                    t.add(os.path.join(sub, n), arcname=n)
        sys.stdout.buffer.write(buf.getvalue()); sys.exit(0)
    if "sha256sum" in script:                             # ha_webcert_sum: checksum over custom.crt + custom.key, empty without both
        import hashlib
        crt, key = os.path.join(web, "custom.crt"), os.path.join(web, "custom.key")
        if os.path.exists(crt) and os.path.exists(key):
            print(hashlib.sha256(open(crt, "rb").read() + open(key, "rb").read()).hexdigest())
        sys.exit(0)
    if "tar -x" in script:                                # mkdir -p DIR && tar -x -C DIR -f - && chmod...
        os.makedirs(web, exist_ok=True)
        if os.environ.get("FAKE_WRITE_FAILS") == "1": sys.exit(1)
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(web)
        sys.exit(0)
    if "rm -f" in script:
        for n in ("custom.crt", "custom.key"):
            p = os.path.join(web, n)
            if os.path.exists(p): os.remove(p)
        sys.exit(0)
sys.exit(0)
'''


def openssl(*args, input=None):
    return subprocess.run(["openssl", *args], capture_output=True, text=True, input=input, check=True).stdout


class CertCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "inst"; self.dir.mkdir()
        self.vol = self.tmp / "vol"; self.vol.mkdir()
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER); (self.bin / "docker").chmod(0o755)
        (self.dir / "docker-compose.yml").write_text("services: {}\n")
        (self.dir / ".env").write_text("OPNEXUS_IMAGE_REPO=127.0.0.1:5000/opnexus\nOPNEXUS_SITES=10.0.0.5, opnexus.lan\nOPNEXUS_DEFAULT_SNI=10.0.0.5\n")
        self.log = self.tmp / "log"
        self.work = self.tmp / "work"; self.work.mkdir()

    def make_cert(self, name="a", sans="IP:10.0.0.5,DNS:opnexus.lan", days=30, key_of=None):
        crt, key = self.work / f"{name}.crt", self.work / f"{name}.key"
        openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
        if key_of:
            shutil.copy(self.work / f"{key_of}.key", key)
        validity = ["-days", str(days)] if days > 0 else ["-not_before", "20200101000000Z", "-not_after", "20200102000000Z"]   # days <= 0: long expired
        openssl("req", "-new", "-x509", "-key", str(key), "-subj", f"/CN={name}", "-addext", f"subjectAltName={sans}", *validity, "-out", str(crt))
        return crt, key

    def run_cli(self, *argv, env=None, stdin=""):
        e = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "OPNEXUS_DIR": str(self.dir), "FAKE_VOL": str(self.vol),
             "FAKE_LOG": str(self.log), "OPNEXUS_CERT_WAIT": "2", "OPNEXUS_LANG": "de", **(env or {})}
        return subprocess.run([str(SCRIPT), *argv], capture_output=True, text=True, input=stdin, env=e, cwd=self.work)

    def env_value(self, key):
        for line in (self.dir / ".env").read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return None

    def volume_files(self):
        d = self.vol / "opnexus-web"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    # --- install ---------------------------------------------------------------------------------------------------------
    def test_install_puts_the_files_into_the_volume_switches_the_mode_and_checks_what_the_proxy_serves(self):
        crt, key = self.make_cert()
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.volume_files(), ["custom.crt", "custom.key"])
        self.assertEqual((self.vol / "opnexus-web" / "custom.crt").read_text(), crt.read_text())
        self.assertEqual(self.env_value("OPNEXUS_WEB_CERT"), "custom")
        self.assertIn("Eigenes Zertifikat ist aktiv", r.stdout)
        self.assertIn("docker compose up -d --no-deps proxy", self.log.read_text())

    def test_an_installation_whose_compose_file_does_not_pass_the_setting_is_refused_with_the_reason(self):
        crt, key = self.make_cert()
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes", env={"FAKE_COMPOSE_OLD": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("vor 1.41", r.stderr)
        self.assertEqual(self.volume_files(), [])
        self.assertIsNone(self.env_value("OPNEXUS_WEB_CERT"))

    def test_relative_paths_mean_from_where_the_user_stands(self):
        self.make_cert()
        r = self.run_cli("cert", "install", "--cert", "a.crt", "--key", "a.key", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_key_that_does_not_belong_to_the_certificate_is_refused_before_anything_is_touched(self):
        crt, _ = self.make_cert("a")
        _, other = self.make_cert("b")
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(other), "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("gehört nicht zu diesem Zertifikat", r.stderr)
        self.assertEqual(self.volume_files(), [])
        self.assertIsNone(self.env_value("OPNEXUS_WEB_CERT"))
        self.assertNotIn("up -d", self.log.read_text() if self.log.exists() else "")

    def test_an_encrypted_key_is_refused_with_a_clear_message(self):
        crt, key = self.make_cert()
        enc = self.work / "enc.key"
        openssl("pkey", "-in", str(key), "-aes256", "-passout", "pass:secret", "-out", str(enc))
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(enc), "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Passphrase", r.stderr)

    def test_files_that_are_not_pem_are_refused(self):
        bad = self.work / "bad.crt"; bad.write_text("not a certificate\n")
        _, key = self.make_cert()
        r = self.run_cli("cert", "install", "--cert", str(bad), "--key", str(key), "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("kein Zertifikat", r.stderr)

    def test_an_expired_certificate_is_refused(self):
        crt, key = self.make_cert("old", days=0)
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("abgelaufen", r.stderr)
        self.assertEqual(self.volume_files(), [])

    def test_a_certificate_for_none_of_the_addresses_is_refused(self):
        crt, key = self.make_cert("x", sans="DNS:other.example")
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("keine Adresse", r.stderr)
        self.assertEqual(self.volume_files(), [])

    def test_a_certificate_that_covers_only_some_addresses_installs_with_a_warning(self):
        crt, key = self.make_cert("p", sans="IP:10.0.0.5")   # opnexus.lan is not covered
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ACHTUNG: gilt nicht für: opnexus.lan", r.stdout)

    def test_without_yes_the_user_is_asked_and_no_cancels(self):
        crt, key = self.make_cert()
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), stdin="n\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.volume_files(), [])
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), stdin="j\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_when_the_proxy_does_not_serve_the_new_certificate_everything_is_rolled_back(self):
        crt, key = self.make_cert()
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes", env={"FAKE_PROXY_BROKEN": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("zurückgenommen", r.stdout)
        self.assertIn("wiederhergestellt", r.stderr)
        self.assertEqual(self.volume_files(), [])
        self.assertEqual(self.env_value("OPNEXUS_WEB_CERT"), "auto")   # the previous mode, not "custom"

    def test_a_failed_replacement_restores_the_previous_own_certificate(self):
        a_crt, a_key = self.make_cert("a")
        self.assertEqual(self.run_cli("cert", "install", "--cert", str(a_crt), "--key", str(a_key), "--yes").returncode, 0)
        b_crt, b_key = self.make_cert("b")
        r = self.run_cli("cert", "install", "--cert", str(b_crt), "--key", str(b_key), "--yes", env={"FAKE_PROXY_BROKEN": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.vol / "opnexus-web" / "custom.crt").read_text(), a_crt.read_text())
        self.assertEqual(self.env_value("OPNEXUS_WEB_CERT"), "custom")

    def test_when_the_volume_cannot_be_written_nothing_changes(self):
        crt, key = self.make_cert()
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes", env={"FAKE_WRITE_FAILS": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIsNone(self.env_value("OPNEXUS_WEB_CERT"))

    # --- remove ---------------------------------------------------------------------------------------------------------------
    def test_remove_deletes_the_files_and_goes_back_to_auto(self):
        crt, key = self.make_cert()
        self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        r = self.run_cli("cert", "remove", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.volume_files(), [])
        self.assertEqual(self.env_value("OPNEXUS_WEB_CERT"), "auto")
        self.assertIn("internen CA ist wieder aktiv", r.stdout)

    # --- status ---------------------------------------------------------------------------------------------------------------
    def test_status_names_the_source_and_the_addresses(self):
        r = self.run_cli("cert", "status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OPNEXUS_WEB_CERT=auto", r.stdout)
        self.assertIn("interne CA", r.stdout)
        crt, key = self.make_cert()
        self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        r = self.run_cli("cert", "status")
        self.assertIn("eigenes Zertifikat", r.stdout)
        self.assertIn("IP:10.0.0.5", r.stdout)
        self.assertNotIn("ACHTUNG", r.stdout)

    def test_status_warns_when_custom_is_set_but_something_else_is_served(self):
        crt, key = self.make_cert()
        self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        r = self.run_cli("cert", "status", env={"FAKE_PROXY_BROKEN": "1"})
        self.assertIn("ACHTUNG: custom ist eingestellt", r.stdout)

    def test_status_is_available_in_english(self):
        r = self.run_cli("cert", "status", env={"OPNEXUS_LANG": "en"})
        self.assertIn("OPNexus web certificate", r.stdout)
        self.assertIn("Source", r.stdout)

    # --- machine readable: status --json, check ------------------------------------------------------------------------
    def test_status_json_describes_the_served_certificate(self):
        import json
        r = self.run_cli("cert", "status", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual((d["mode"], d["source"], d["custom_not_served"]), ("auto", "per-address", False))
        self.assertEqual(d["names"], ["IP Address:10.0.0.5".replace("IP Address:", "IP:"), "DNS:opnexus.lan"])
        self.assertEqual(d["uncovered"], [])
        self.assertIsInstance(d["days_left"], int)
        crt, key = self.make_cert()
        self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        d = json.loads(self.run_cli("cert", "status", "--json").stdout)
        self.assertEqual((d["mode"], d["source"]), ("custom", "custom"))
        d = json.loads(self.run_cli("cert", "status", "--json", env={"FAKE_PROXY_BROKEN": "1"}).stdout)
        self.assertTrue(d["custom_not_served"])

    def fake_busybox_date(self):
        """BusyBox date (the updater container) rejects `date -d <openssl date>` and needs `-u -D <format>`."""
        (self.bin / "date").write_text('#!/bin/sh\nif [ "$1" = "-d" ]; then echo "date: invalid date" >&2; exit 1; fi\n'
                                       'if [ "$1" = "-u" ] && [ "$2" = "-D" ]; then shift 3; fi\n'
                                       f'exec {shutil.which("date")} "$@"\n')
        (self.bin / "date").chmod(0o755)

    def test_the_days_left_are_known_with_busybox_date_too(self):
        import json
        self.fake_busybox_date()
        d = json.loads(self.run_cli("cert", "status", "--json").stdout)
        self.assertIsInstance(d["days_left"], int)
        self.assertGreater(d["days_left"], 100)
        crt, key = self.make_cert()
        d = json.loads(self.run_cli("cert", "check", "--cert", str(crt), "--key", str(key)).stdout)
        self.assertIsInstance(d["days_left"], int)
        self.assertIn("noch", self.run_cli("cert", "status").stdout)

    def test_unknown_days_left_do_not_break_the_output(self):
        import json
        (self.bin / "date").write_text("#!/bin/sh\ncase \"$*\" in *-d*) echo 'date: invalid date' >&2; exit 1;; esac\nexec %s \"$@\"\n" % shutil.which("date"))
        (self.bin / "date").chmod(0o755)
        r = self.run_cli("cert", "status", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(json.loads(r.stdout)["days_left"])
        self.assertEqual(self.run_cli("cert", "status").returncode, 0)

    def test_status_json_names_addresses_the_certificate_does_not_cover(self):
        import json
        (self.dir / ".env").write_text((self.dir / ".env").read_text() + "OPNEXUS_SITES=10.0.0.5, opnexus.lan, other.lan\n")
        d = json.loads(self.run_cli("cert", "status", "--json").stdout)
        self.assertEqual(d["uncovered"], ["other.lan"])

    def test_status_rejects_unknown_options(self):
        self.assertNotEqual(self.run_cli("cert", "status", "--xml").returncode, 0)

    def test_check_reports_the_certificate_and_changes_nothing(self):
        import json
        crt, key = self.make_cert(sans="IP:10.0.0.5")
        r = self.run_cli("cert", "check", "--cert", str(crt), "--key", str(key))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["subject"], "CN=a")
        self.assertEqual(d["names"], ["IP:10.0.0.5"])
        self.assertEqual(d["uncovered"], ["opnexus.lan"])
        self.assertTrue(29 <= d["days_left"] <= 30)
        self.assertRegex(d["fingerprint"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertEqual(self.volume_files(), [])                       # nothing written
        self.assertIsNone(self.env_value("OPNEXUS_WEB_CERT"))           # mode untouched
        self.assertFalse(self.log.exists() and "up " in self.log.read_text())   # proxy not restarted

    def test_check_refuses_what_install_refuses(self):
        a, _ = self.make_cert("a"); b, bkey = self.make_cert("b")
        for crt, key, needle in ((a, bkey, "gehört nicht zu diesem Zertifikat"), (self.make_cert("old", days=0)[0], self.work / "old.key", "abgelaufen"),
                                 (self.make_cert("far", sans="IP:10.9.9.9")[0], self.work / "far.key", "keine Adresse")):
            r = self.run_cli("cert", "check", "--cert", str(crt), "--key", str(key))
            self.assertNotEqual(r.returncode, 0, needle)
            self.assertIn(needle, r.stderr)
            self.assertEqual(r.stdout.strip(), "")

    def test_check_json_survives_quotes_and_backslashes_in_the_subject(self):
        import json
        crt, key = self.work / "q.crt", self.work / "q.key"
        openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
        openssl("req", "-new", "-x509", "-key", str(key), "-subj", '/CN=a"b\\c', "-addext", "subjectAltName=IP:10.0.0.5,DNS:opnexus.lan", "-days", "30", "-out", str(crt))
        d = json.loads(self.run_cli("cert", "check", "--cert", str(crt), "--key", str(key)).stdout)
        self.assertIn('a"b', d["subject"])

    def test_check_needs_both_files(self):
        r = self.run_cli("cert", "check", "--cert", "x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Aufruf", r.stderr)

    # --- HA: the primary hands the certificate to the standby right away ---------------------------------------------------
    def ha_env(self):
        (self.dir / ".env").write_text((self.dir / ".env").read_text() + "HA_ROLE=primary\nHA_NODE=a\nHA_NODE_IP=10.0.0.5\nHA_PEER=root@10.0.0.6\nHA_PEER_IP=10.0.0.6\n")
        sshlog = self.tmp / "ssh.log"
        fake = self.bin / "ssh"
        fake.write_text("#!/bin/sh\necho \"ssh $*\" >> %s\ncat >/dev/null\nexit ${FAKE_SSH_RC:-0}\n" % sshlog); fake.chmod(0o755)
        return sshlog

    def test_install_on_an_ha_primary_sends_the_certificate_to_the_standby(self):
        sshlog = self.ha_env()
        crt, key = self.make_cert()
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = sshlog.read_text()
        self.assertIn("ha receive web-cert-sum", calls)
        self.assertRegex(calls, r"ha receive web-cert [0-9a-f]{64}")

    def test_an_unreachable_standby_does_not_fail_the_install(self):
        self.ha_env()
        crt, key = self.make_cert()
        r = self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes", env={"FAKE_SSH_RC": "255"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.env_value("OPNEXUS_WEB_CERT"), "custom")

    def test_remove_on_an_ha_primary_clears_the_standby_if_it_still_holds_one(self):
        sshlog = self.ha_env()
        crt, key = self.make_cert()
        self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        fake = self.bin / "ssh"
        fake.write_text("#!/bin/sh\necho \"ssh $*\" >> %s\ncat >/dev/null\ncase \"$*\" in *web-cert-sum*) echo %s;; esac\nexit 0\n" % (sshlog, "e" * 64))
        sshlog.write_text("")
        r = self.run_cli("cert", "remove", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ha receive web-cert-clear", sshlog.read_text())

    def test_without_ha_nothing_is_sent(self):
        sshlog = self.tmp / "ssh.log"
        fake = self.bin / "ssh"; fake.write_text("#!/bin/sh\necho x >> %s\n" % sshlog); fake.chmod(0o755)
        crt, key = self.make_cert()
        self.run_cli("cert", "install", "--cert", str(crt), "--key", str(key), "--yes")
        self.assertFalse(sshlog.exists())

    # --- misc -----------------------------------------------------------------------------------------------------------------
    def test_unknown_subcommand_and_missing_arguments_print_the_usage_and_fail(self):
        for argv in (("cert",), ("cert", "frobnicate"), ("cert", "install"), ("cert", "install", "--cert", "x")):
            r = self.run_cli(*argv)
            self.assertNotEqual(r.returncode, 0, argv)
            self.assertIn("Aufruf", r.stderr)

    def test_the_commands_are_listed_in_the_usage_in_both_languages(self):
        for lang, needle in (("de", "Webzertifikat des Proxys"), ("en", "Web certificate of the proxy")):
            r = self.run_cli("nonsense", env={"OPNEXUS_LANG": lang})
            self.assertIn(needle, r.stdout)
            self.assertIn("opnexus cert install", r.stdout)


if __name__ == "__main__":
    unittest.main()
