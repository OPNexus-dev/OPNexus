"""Tests für updater/server.py — nur Standardbibliothek:

    python3 -m unittest discover -s updater -p 'test_*.py'

Das echte `opnexus`-Skript und Docker werden durch ein Fake-Skript ersetzt, das
`status --porcelain` beantwortet und `update`/`rollback` mit einem vorgegebenen
Exit-Code beendet; so sind Zustandsübergänge, Ablehnungen und die Authentifizierung
prüfbar, ohne einen Docker-Daemon.
"""
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

FAKE = """#!/bin/sh
case "$1" in
  status)
    echo "running_version=${FAKE_RUNNING:-1.14.0}"
    echo "db_revision=0004"
    echo "service.backend=healthy"
    if [ -n "$FAKE_LAST" ]; then
      echo "last_update.prev_version=1.13.0"
      echo "last_update.prev_setting=1.13.0"
      echo "last_update.prev_db_revision=${FAKE_PREV_REV:-0004}"
    fi
    ;;
  update|rollback)
    [ -z "$OPNEXUS_PHASE_FILE" ] || echo pulling >> "$OPNEXUS_PHASE_FILE"
    echo "arbeite $1 $2"
    sleep "${FAKE_SLEEP:-0.2}"
    exit "${FAKE_EXIT:-0}"
    ;;
esac
"""


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "state").mkdir()
        (root / "project").mkdir()
        (root / "project" / ".env").write_text("OPNEXUS_IMAGE_REPO=reg.test/opnexus\nOPNEXUS_VERSION=1.14.0\n")
        script = root / "opnexus"
        script.write_text(FAKE)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        state = root / "state"
        server.STATE_DIR = state
        server.TOKEN_FILE = state / "token"
        server.JOB_FILE = state / "job.json"
        server.LOG_FILE = state / "job.log"
        server.PHASE_FILE = state / "job.phases"
        server.PROJECT_DIR = root / "project"
        server.SCRIPT = str(script)
        server._status_cache.update(at=0.0, value=None)
        for key in ("FAKE_EXIT", "FAKE_RUNNING", "FAKE_LAST", "FAKE_PREV_REV", "FAKE_SLEEP"):
            os.environ.pop(key, None)
        self.token = server.load_token()
        server.Handler.token = self.token
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        deadline = time.time() + 5
        while server._lock.locked() and time.time() < deadline:
            time.sleep(0.05)
        self.tmp.cleanup()

    def call(self, method, path, body=None, token="__default__"):
        token = self.token if token == "__default__" else token
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def wait_done(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            _, body = self.call("GET", "/status")
            if body["job"] and body["job"]["state"] != "running":
                return body
            time.sleep(0.1)
        self.fail("job did not finish")

    # --- Authentifizierung -------------------------------------------------
    def test_health_is_public_everything_else_needs_the_token(self):
        self.assertEqual(self.call("GET", "/health", token=None)[0], 200)
        for token in (None, "", "wrong", self.token[:-1]):
            self.assertEqual(self.call("GET", "/status", token=token)[0], 401, token)
            self.assertEqual(self.call("POST", "/update", {"version": "1.14.1"}, token=token)[0], 401)
            self.assertEqual(self.call("POST", "/rollback", {}, token=token)[0], 401)

    def test_token_is_generated_once_and_reused(self):
        self.assertGreaterEqual(len(self.token), 32)
        self.assertEqual(server.load_token(), self.token)

    def test_unknown_paths_are_404(self):
        self.assertEqual(self.call("GET", "/etc/passwd")[0], 404)
        self.assertEqual(self.call("POST", "/exec", {"cmd": "id"})[0], 404)

    # --- Status ------------------------------------------------------------
    def test_status_reports_version_and_mode(self):
        status, body = self.call("GET", "/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "release")
        self.assertEqual(body["running_version"], "1.14.0")
        self.assertFalse(body["rollback_available"])

    def test_local_build_install_is_reported_as_local(self):
        (server.PROJECT_DIR / ".env").write_text("POSTGRES_PASSWORD=x\n")
        _, body = self.call("GET", "/status")
        self.assertEqual(body["mode"], "local")
        self.assertEqual(self.call("POST", "/update", {"version": "1.14.1"})[1], {"error": "not_release_mode"})

    # --- Update ------------------------------------------------------------
    def test_update_runs_and_succeeds(self):
        status, body = self.call("POST", "/update", {"version": "1.14.1"})
        self.assertEqual(status, 202)
        self.assertEqual(body["job"]["target"], "1.14.1")
        done = self.wait_done()
        self.assertEqual(done["job"]["state"], "succeeded")
        self.assertIn("pulling", done["phases"])
        self.assertTrue(any("arbeite update 1.14.1" in line for line in done["log"]))

    def test_failed_update_exit_codes(self):
        os.environ["FAKE_EXIT"] = "3"
        self.call("POST", "/update", {"version": "1.14.1"})
        self.assertEqual(self.wait_done()["job"]["state"], "rolled_back")
        server._status_cache.update(at=0.0, value=None)
        os.environ["FAKE_EXIT"] = "1"
        self.call("POST", "/update", {"version": "1.14.1"})
        self.assertEqual(self.wait_done()["job"]["state"], "failed")

    def test_invalid_and_disallowed_versions_never_start_a_job(self):
        cases = {
            "1.14.0": "not_newer", "1.13.9": "not_newer", "2.0.0": "major_change",
            "latest": "invalid_version", "1.14.1-rc1": "invalid_version", "1.14": "invalid_version",
            "1.14.1; id": "invalid_version", "../1.14.1": "invalid_version", "": "invalid_version",
        }
        for version, error in cases.items():
            status, body = self.call("POST", "/update", {"version": version})
            self.assertEqual(body, {"error": error}, version)
            self.assertIn(status, (400, 409))
        self.assertEqual(self.call("POST", "/update", {})[1], {"error": "invalid_version"})
        self.assertEqual(self.call("POST", "/update", {"version": 5})[1], {"error": "invalid_version"})
        self.assertIsNone(self.call("GET", "/status")[1]["job"])

    def test_second_request_while_running_is_busy(self):
        os.environ["FAKE_SLEEP"] = "1.5"
        self.assertEqual(self.call("POST", "/update", {"version": "1.14.1"})[0], 202)
        self.assertEqual(self.call("POST", "/update", {"version": "1.14.2"}), (409, {"error": "busy"}))
        self.assertEqual(self.call("POST", "/rollback", {}), (409, {"error": "busy"}))
        _, running = self.call("GET", "/status")
        self.assertEqual(running["job"]["state"], "running")
        self.assertEqual(self.wait_done()["job"]["state"], "succeeded")

    def test_oversized_or_malformed_body_is_rejected(self):
        req = urllib.request.Request(self.base + "/update", method="POST", data=b"x" * 5000,
                                     headers={"Authorization": "Bearer " + self.token, "Content-Length": "5000"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 413)
        req = urllib.request.Request(self.base + "/update", method="POST", data=b"{not json",
                                     headers={"Authorization": "Bearer " + self.token})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    # --- Rollback ----------------------------------------------------------
    def test_rollback_needs_a_previous_update(self):
        self.assertEqual(self.call("POST", "/rollback", {}), (409, {"error": "no_rollback"}))

    def test_rollback_and_data_loss_flag(self):
        os.environ["FAKE_LAST"] = "1"
        _, body = self.call("GET", "/status")
        self.assertTrue(body["rollback_available"])
        self.assertFalse(body["rollback_loses_data"])  # Revision unverändert
        server._status_cache.update(at=0.0, value=None)
        os.environ["FAKE_PREV_REV"] = "0003"
        _, body = self.call("GET", "/status")
        self.assertTrue(body["rollback_loses_data"])
        server._status_cache.update(at=0.0, value=None)
        self.assertEqual(self.call("POST", "/rollback", {})[0], 202)
        self.assertEqual(self.wait_done()["job"]["state"], "succeeded")

    # --- Neustart ----------------------------------------------------------
    def test_job_left_running_after_a_restart_is_marked_failed(self):
        server.write_job({"id": "x", "action": "update", "target": "1.14.1", "state": "running"})
        server.mark_interrupted()
        job = server.read_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("neu gestartet", server.LOG_FILE.read_text())


if __name__ == "__main__":
    unittest.main()
