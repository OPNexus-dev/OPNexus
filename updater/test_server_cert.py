"""Updater endpoints for the own web certificate (/cert/status, /cert/check, /cert/install, /cert/remove).
The real `opnexus` script is replaced by a fake that answers `cert check|status|install|remove`, records what it was given
(including whether the key file existed with mode 0600 while it ran) and exits with a chosen code.
Run: cd updater && python3 -m unittest test_server_cert"""
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import server

os.environ.setdefault("OPNEXUS_LANG", "de")

FAKE = r"""#!/bin/sh
echo "$*" >> "$FAKE_CALLS"
case "$1" in
  status) echo "running_version=1.41.0"; echo "service.backend=healthy" ;;
  cert)
    case "$2" in
      check)
        [ -z "$FAKE_CHECK_FAIL" ] || { printf '\033[31mFehler:\033[0m Der Schlüssel gehört nicht zu diesem Zertifikat.\n' >&2; exit 1; }
        echo '{"subject":"CN=lab","issuer":"CN=lab","not_after":"Jan  1 00:00:00 2030 GMT","days_left":900,"names":["IP:10.0.0.5"],"fingerprint":"AA:BB","uncovered":[]}' ;;
      status)
        [ -z "$FAKE_STATUS_FAIL" ] || { printf 'Error: Der Proxy läuft nicht.\n' >&2; exit 1; }
        echo '{"mode":"auto","source":"all","custom_not_served":false,"subject":"CN=OPNexus","issuer":"CN=CA","not_after":"x","days_left":700,"names":["IP:10.0.0.5"],"fingerprint":"CC:DD","uncovered":[]}' ;;
      install)
        shift 2
        while [ $# -gt 0 ]; do case "$1" in --key) k="$2"; shift 2;; --cert) c="$2"; shift 2;; *) shift;; esac; done
        echo "key_mode=$(stat -c %a "$k") key_dir_mode=$(stat -c %a "$(dirname "$k")") cert=$(cat "$c" | head -1) key=$(head -1 "$k")" >> "$FAKE_CALLS"
        echo "$k" > "$FAKE_KEYPATH"
        sleep "${FAKE_SLEEP:-0.2}"; echo "Eigenes Zertifikat ist aktiv."; exit "${FAKE_EXIT:-0}" ;;
      remove) sleep "${FAKE_SLEEP:-0.2}"; echo "Das Zertifikat der internen CA ist wieder aktiv."; exit "${FAKE_EXIT:-0}" ;;
    esac ;;
esac
"""
CERT = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\nMIIBsecret\n-----END PRIVATE KEY-----\n"


class CertEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for d in ("state", "project"):
            (root / d).mkdir()
        (root / "project" / ".env").write_text("OPNEXUS_IMAGE_REPO=reg.test/opnexus\nOPNEXUS_VERSION=1.41.0\n")
        script = root / "opnexus"; script.write_text(FAKE); script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.calls, self.keypath = root / "calls", root / "keypath"
        os.environ.update(FAKE_CALLS=str(self.calls), FAKE_KEYPATH=str(self.keypath))
        for k in ("FAKE_EXIT", "FAKE_SLEEP", "FAKE_CHECK_FAIL", "FAKE_STATUS_FAIL"):
            os.environ.pop(k, None)
        state = root / "state"
        server.STATE_DIR = state; server.TOKEN_FILE = state / "token"
        server.JOB_FILE = state / "job.json"; server.LOG_FILE = state / "job.log"; server.PHASE_FILE = state / "job.phases"
        server.HA_JOB_FILE = state / "ha-job.json"; server.HA_LOG_FILE = state / "ha-job.log"
        server.CERT_JOB_FILE = state / "cert-job.json"; server.CERT_LOG_FILE = state / "cert-job.log"
        server.PROJECT_DIR = root / "project"; server.SCRIPT = str(script)
        server._status_cache.update(at=0.0, value=None)
        self.state = state
        self.token = server.load_token(); server.Handler.token = self.token
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler); self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        deadline = time.time() + 5
        while server._lock.locked() and time.time() < deadline:
            time.sleep(0.05)
        self.tmp.cleanup()

    def call(self, method, path, body=None, token="__default__", raw=None):
        token = self.token if token == "__default__" else token
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def wait_job(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            _, body = self.call("GET", "/cert/status")
            if body["job"] and body["job"]["state"] != "running":
                return body
            time.sleep(0.1)
        self.fail("job did not finish")

    def calls_text(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def workdirs(self):
        return [p.name for p in self.state.iterdir() if p.name.startswith(server.CERT_IN_PREFIX)]

    # --- access ------------------------------------------------------------------------------------------------------------
    def test_everything_needs_the_token(self):
        for token in (None, "", "wrong", self.token[:-1]):
            self.assertEqual(self.call("GET", "/cert/status", token=token)[0], 401)
            for path in ("check", "install", "remove"):
                self.assertEqual(self.call("POST", f"/cert/{path}", {"cert": CERT, "key": KEY}, token=token)[0], 401, path)
        self.assertEqual(self.calls_text(), "")   # nothing ran

    # --- status ------------------------------------------------------------------------------------------------------------
    def test_status_passes_the_cli_json_through(self):
        status, body = self.call("GET", "/cert/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"]["source"], "all")
        self.assertIsNone(body["error"]); self.assertIsNone(body["job"])
        self.assertIn("cert status --json", self.calls_text())

    def test_a_failing_status_reports_the_reason_instead_of_failing(self):
        os.environ["FAKE_STATUS_FAIL"] = "1"
        status, body = self.call("GET", "/cert/status")
        self.assertEqual(status, 200)
        self.assertIsNone(body["status"]); self.assertEqual(body["error"], "Der Proxy läuft nicht.")

    def test_status_reports_the_ha_role(self):
        (server.PROJECT_DIR / ".env").write_text((server.PROJECT_DIR / ".env").read_text() + "HA_ROLE=primary\n")
        self.assertEqual(self.call("GET", "/cert/status")[1]["ha_role"], "primary")

    # --- check -------------------------------------------------------------------------------------------------------------
    def test_check_returns_the_certificate_details_and_leaves_no_key_behind(self):
        status, body = self.call("POST", "/cert/check", {"cert": CERT, "key": KEY})
        self.assertEqual(status, 200)
        self.assertEqual(body["certificate"]["subject"], "CN=lab")
        self.assertEqual(self.workdirs(), [])

    def test_check_refusal_carries_the_clis_reason_and_leaves_no_key_behind(self):
        os.environ["FAKE_CHECK_FAIL"] = "1"
        status, body = self.call("POST", "/cert/check", {"cert": CERT, "key": KEY})
        self.assertEqual((status, body["error"]), (400, "invalid_certificate"))
        self.assertEqual(body["detail"], "Der Schlüssel gehört nicht zu diesem Zertifikat.")   # no colour codes, no "Fehler:" label
        self.assertEqual(self.workdirs(), [])

    def test_malformed_input_is_refused_before_anything_runs(self):
        cases = [({}, 400), ({"cert": CERT}, 400), ({"cert": 1, "key": KEY}, 400), ({"cert": "", "key": KEY}, 400),
                 ({"cert": "hello", "key": KEY}, 400), ({"cert": CERT, "key": "hello"}, 400),
                 ({"cert": CERT + "A" * 40000, "key": KEY}, 413), ({"cert": CERT, "key": KEY + "A" * 40000}, 413)]
        for body, want in cases:
            for path in ("check", "install"):
                status, _ = self.call("POST", f"/cert/{path}", body)
                self.assertEqual(status, want, (path, str(body)[:40]))
        self.assertEqual(self.calls_text(), "")
        self.assertEqual(self.call("POST", "/cert/check", raw=b"x" * 90000)[0], 413)
        self.assertEqual(self.call("POST", "/cert/check", raw=b"not json")[0], 400)

    # --- install / remove ---------------------------------------------------------------------------------------------------
    def test_install_runs_as_a_job_with_a_private_key_file_and_removes_it_afterwards(self):
        status, body = self.call("POST", "/cert/install", {"cert": CERT, "key": KEY})
        self.assertEqual(status, 202)
        self.assertEqual(body["job"]["verb"], "install")
        done = self.wait_job()
        self.assertEqual(done["job"]["state"], "succeeded")
        self.assertIn("Eigenes Zertifikat ist aktiv.", done["log"])
        text = self.calls_text()
        self.assertIn("key_mode=600 key_dir_mode=700", text)
        self.assertIn("cert=-----BEGIN CERTIFICATE-----", text)
        self.assertIn("--yes", text)
        self.assertFalse(Path(self.keypath.read_text().strip()).exists())
        self.assertEqual(self.workdirs(), [])
        # the key never reaches the log or the job record
        self.assertNotIn("MIIBsecret", (self.state / "cert-job.log").read_text())
        self.assertNotIn("MIIBsecret", (self.state / "cert-job.json").read_text())

    def test_a_failed_install_is_reported_and_cleans_up(self):
        os.environ["FAKE_EXIT"] = "1"
        self.call("POST", "/cert/install", {"cert": CERT, "key": KEY})
        done = self.wait_job()
        self.assertEqual((done["job"]["state"], done["job"]["exit_code"]), ("failed", 1))
        self.assertEqual(self.workdirs(), [])

    def test_install_of_an_invalid_pair_never_starts_a_job(self):
        os.environ["FAKE_CHECK_FAIL"] = "1"
        status, body = self.call("POST", "/cert/install", {"cert": CERT, "key": KEY})
        self.assertEqual((status, body["error"]), (400, "invalid_certificate"))
        self.assertNotIn("cert install", self.calls_text())
        self.assertFalse(server._lock.locked())
        self.assertIsNone(self.call("GET", "/cert/status")[1]["job"])

    def test_remove_runs_as_a_job(self):
        status, body = self.call("POST", "/cert/remove", {})
        self.assertEqual((status, body["job"]["verb"]), (202, "remove"))
        done = self.wait_job()
        self.assertEqual(done["job"]["state"], "succeeded")
        self.assertIn("cert remove --yes", self.calls_text())

    def test_while_a_job_runs_the_status_does_not_query_the_restarting_proxy(self):
        os.environ["FAKE_SLEEP"] = "1.5"
        self.call("POST", "/cert/remove", {})
        _, body = self.call("GET", "/cert/status")
        self.assertEqual(body["job"]["state"], "running"); self.assertIsNone(body["status"]); self.assertIsNone(body["error"])
        self.assertNotIn("cert status", self.calls_text())
        self.wait_job()

    def test_only_one_job_at_a_time(self):
        os.environ["FAKE_SLEEP"] = "1.5"
        self.assertEqual(self.call("POST", "/cert/remove", {})[0], 202)
        for path, body in (("remove", {}), ("install", {"cert": CERT, "key": KEY})):
            status, resp = self.call("POST", f"/cert/{path}", body)
            self.assertEqual((status, resp["error"]), (409, "busy"), path)
        self.wait_job()

    def test_a_standby_refuses_changes(self):
        (server.PROJECT_DIR / ".env").write_text((server.PROJECT_DIR / ".env").read_text() + "HA_ROLE=standby\n")
        for path, body in (("remove", {}), ("install", {"cert": CERT, "key": KEY})):
            status, resp = self.call("POST", f"/cert/{path}", body)
            self.assertEqual((status, resp["error"]), (409, "ha_standby"), path)
        self.assertNotIn("cert install", self.calls_text()); self.assertNotIn("cert remove", self.calls_text())
        self.assertEqual(self.workdirs(), [])
        self.assertFalse(server._lock.locked())

    def test_a_local_build_install_refuses_changes(self):
        (server.PROJECT_DIR / ".env").write_text("POSTGRES_PASSWORD=x\n")
        status, resp = self.call("POST", "/cert/remove", {})
        self.assertEqual((status, resp["error"]), (409, "not_release_mode"))
        self.assertFalse(server._lock.locked())

    # --- restart ------------------------------------------------------------------------------------------------------------
    def test_a_restart_marks_the_running_job_failed_and_removes_leftover_key_material(self):
        left = self.state / (server.CERT_IN_PREFIX + "abc"); left.mkdir(); (left / "key.pem").write_text(KEY)
        server.write_cert_job({"id": "x", "verb": "install", "state": "running", "started_at": "t", "finished_at": None, "exit_code": None})
        server.mark_interrupted()
        self.assertEqual(server.read_cert_job()["state"], "failed")
        self.assertFalse(left.exists())


if __name__ == "__main__":
    unittest.main()
