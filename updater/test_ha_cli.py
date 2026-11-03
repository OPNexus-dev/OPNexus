"""Kopplung und Partner-Aufrufe von Ende zu Ende: ha_cli.py (Primär-Seite) gegen die echte Server-Logik (Standby-Seite) über TLS mit Pin.
Das Gate (Caddy) wird durch eine Handler-Unterklasse ersetzt, die X-HA-Remote wie Caddy überschreibt.
Run: cd updater && python3 -m unittest test_ha_cli"""
import contextlib
import io
import os
import shutil
import ssl
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ha_agent
import ha_cli
import ha_net
import server
import test_server_ha

FAKE = test_server_ha.FAKE


class FakeGate(server.Handler):
    """Caddy: setzt X-HA-Remote auf die Adresse des Gegenübers und nichts anderes."""

    def parse_request(self):
        ok = super().parse_request()
        if ok:
            del self.headers["X-HA-Remote"]
            self.headers["X-HA-Remote"] = "127.0.0.1"
        return ok


def tls_server(handler, cert_dir: Path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_dir / "cert.pem", cert_dir / "key.pem")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir_b = self.tmp / "b"      # künftiger Standby: echte Server-Logik
        self.dir_a = self.tmp / "a"      # Primär: ha_cli
        for d in (self.dir_a, self.dir_b):
            (d / "project").mkdir(parents=True)
            (d / "gate").mkdir()
        (self.dir_b / "project" / ".env").write_text("OPNEXUS_IMAGE_REPO=reg.test/opnexus\nOPNEXUS_VERSION=1.37.0\n")
        (self.dir_b / "state").mkdir()
        script = self.tmp / "opnexus"
        script.write_text(FAKE)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        os.environ.update(FAKE_CALLS=str(self.tmp / "calls.txt"), FAKE_RECV=str(self.tmp / "recv"))
        for key in ("FAKE_EXIT", "FAKE_SLEEP"):
            os.environ.pop(key, None)
        state = self.dir_b / "state"
        server.STATE_DIR, server.TOKEN_FILE = state, state / "token"
        server.JOB_FILE, server.LOG_FILE, server.PHASE_FILE = state / "job.json", state / "job.log", state / "job.phases"
        server.HA_JOB_FILE, server.HA_LOG_FILE = state / "ha-job.json", state / "ha-job.log"
        server.PROJECT_DIR, server.SCRIPT = self.dir_b / "project", str(script)
        server._status_cache.update(at=0.0, value=None)
        server.Handler.token = server.load_token()
        # Standby (B): Gate mit eigenem Zertifikat
        self.pin_b = ha_net.make_gate_cert(self.dir_b / "gate", "127.0.0.1")
        self.httpd_b = tls_server(FakeGate, self.dir_b / "gate")
        self.port_b = self.httpd_b.server_address[1]
        self.addCleanup(self.httpd_b.server_close)
        self.addCleanup(self.httpd_b.shutdown)
        # Primär (A): Gate, an dem der Partner zurückfragt (Ping prüft das Clustergeheimnis gegen A's Speicher)
        self.pin_a = ha_net.make_gate_cert(self.dir_a / "gate", "127.0.0.1")
        store_a = ha_agent.Store(self.dir_a / "project" / ".opnexus-ha" / "agent")

        class GateA(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                ok = self.path == "/ha/peer/ping" and ha_agent.authorize_peer(store_a, self.headers, "127.0.0.1")
                body = b'{"ok": true}' if ok else b"{}"
                self.send_response(200 if ok else 401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd_a = tls_server(GateA, self.dir_a / "gate")
        self.port_a = self.httpd_a.server_address[1]
        self.addCleanup(self.httpd_a.server_close)
        self.addCleanup(self.httpd_a.shutdown)

    def tearDown(self):
        import time
        deadline = time.time() + 5
        while server._lock.locked() and time.time() < deadline:
            time.sleep(0.05)

    def run_cli(self, side, *argv, stdin=None):
        """ha_cli.main als Knoten A oder B; gibt (Exit-Code, stdout, stderr) zurück."""
        base = self.dir_a if side == "a" else self.dir_b
        ha_cli.PROJECT_DIR, ha_cli.GATE_DIR = base / "project", base / "gate"
        ha_cli.POLL_SECONDS = 0.1
        stdout, stderr = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.TextIOWrapper(io.BytesIO(stdin))
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = ha_cli.main(list(argv))
        finally:
            sys.stdin = old_stdin
        return code, stdout.getvalue(), stderr.getvalue()

    def kv(self, text):
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def code_b(self):
        code, out, _ = self.run_cli("b", "prepare", "--ip", "127.0.0.1", "--port", str(self.port_b))
        self.assertEqual(code, 0)
        return self.kv(out)["code"]

    def pair(self):
        code = self.code_b()
        return self.run_cli("a", "pair", code, "--own-pin", self.pin_a, "--own-port", str(self.port_a)), code

    # -----------------------------------------------------------------------
    def test_prepare_makes_a_code_that_carries_address_port_and_the_certificate_pin(self):
        parts = ha_agent.decode_code(self.code_b())
        self.assertEqual((parts["ip"], parts["port"], parts["pin"].hex()), ("127.0.0.1", self.port_b, self.pin_b))

    def test_hello_shows_the_partner_facts_without_using_the_code_up(self):
        code = self.code_b()
        status, out, _ = self.run_cli("a", "hello", code)
        facts = self.kv(out)
        self.assertEqual((status, facts["version"], facts["db_revision"], facts["mode"]), (0, "1.37.0", "0034", "release"))
        self.assertTrue(ha_agent.Store(self.dir_b / "project" / ".opnexus-ha" / "agent").pairing_open())

    def test_hello_waits_longer_than_the_partner_may_take_to_read_its_state(self):
        """Review R20: the partner's facts may take up to 45 s (its service status); the client's 30 s turned a slow answer into "unreachable"."""
        seen = {}
        real = ha_cli._client
        ha_cli._client = lambda parts, token, timeout=30.0: seen.setdefault("t", timeout) and real(parts, token, timeout)
        self.addCleanup(lambda: setattr(ha_cli, "_client", real))
        self.run_cli("a", "hello", self.code_b())
        self.assertGreater(seen["t"], 45.0)

    def test_the_whole_pairing_ends_with_both_sides_knowing_each_other(self):
        (status, out, err), code = self.pair()
        self.assertEqual(status, 0, err)
        cluster_a = ha_agent.Store(self.dir_a / "project" / ".opnexus-ha" / "agent").cluster()
        cluster_b = ha_agent.Store(self.dir_b / "project" / ".opnexus-ha" / "agent").cluster()
        self.assertEqual(cluster_a["secret"], cluster_b["secret"])
        self.assertEqual((cluster_a["peer_pin"], cluster_b["peer_pin"]), (self.pin_b, self.pin_a))
        self.assertEqual((cluster_a["peer_port"], cluster_b["peer_port"]), (self.port_b, self.port_a))
        self.assertEqual(self.run_cli("a", "hello", code)[0], 5)  # the code is used up
        self.assertEqual(self.run_cli("a", "status")[1].split(), ["paired=yes", "peer=127.0.0.1", f"port={self.port_b}"])

    def test_after_pairing_verbs_work_from_the_primary(self):
        self.pair()
        status, out, _ = self.run_cli("a", "call", "status")
        self.assertEqual(status, 0)
        self.assertIn("role=standby", out)
        status, out, _ = self.run_cli("a", "call", "fence")
        self.assertEqual((status, out.strip()), (0, "gestoppt"))
        status, out, _ = self.run_cli("a", "call", "ping")
        self.assertEqual((status, self.kv(out)["ok"]), (0, "True"))

    def test_a_job_is_followed_to_its_end_with_the_exit_code(self):
        self.pair()
        status, out, _ = self.run_cli("a", "call", "promote")
        self.assertEqual(status, 0)
        self.assertIn("arbeite promote --yes", out)
        os.environ["FAKE_EXIT"] = "7"
        self.assertEqual(self.run_cli("a", "call", "resync")[0], 7)

    def test_a_data_stream_is_sent_from_stdin_with_its_parameter(self):
        self.pair()
        blob = bytes(range(256)) * 400
        status, _, err = self.run_cli("a", "call", "receive/data", stdin=blob)
        self.assertEqual(status, 0, err)
        self.assertEqual(Path(str(self.tmp / "recv") + ".data").read_bytes(), blob)
        status, _, _ = self.run_cli("a", "call", "receive/ca", "--param", "b" * 64, stdin=b"tar")
        self.assertEqual(status, 0)
        self.assertEqual(self.run_cli("a", "call", "receive/ca", "--param", "nope", stdin=b"tar")[0], 1)

    def test_a_replaced_certificate_at_the_partner_stops_every_call_before_the_secret_is_sent(self):
        self.pair()
        # the partner's gate now shows a different certificate than the pinned one (man in the middle, or a swapped host)
        other = ha_net.make_gate_cert(self.tmp / "other", "127.0.0.1")
        self.httpd_b.shutdown()
        self.httpd_b.server_close()
        self.httpd_b = tls_server(FakeGate, self.tmp / "other")
        store = ha_agent.Store(self.dir_a / "project" / ".opnexus-ha" / "agent")
        cluster = store.cluster()
        store.set_cluster(cluster["secret"], cluster["peer_ip"], self.httpd_b.server_address[1], cluster["peer_pin"])
        self.addCleanup(self.httpd_b.server_close)
        self.addCleanup(self.httpd_b.shutdown)
        status, _, err = self.run_cli("a", "call", "status")
        self.assertEqual(status, 3)
        self.assertIn("passt nicht", err)
        self.assertNotEqual(other, self.pin_b)

    def test_a_wrong_pin_in_the_code_stops_before_the_pairing_secret_is_sent(self):
        parts = ha_agent.decode_code(self.code_b())
        forged = ha_agent.encode_code(parts["ip"], parts["port"], parts["secret"], bytes(32))
        status, _, err = self.run_cli("a", "hello", forged)
        self.assertEqual(status, 3)
        self.assertEqual(self.run_cli("a", "pair", forged, "--own-pin", self.pin_a, "--own-port", str(self.port_a))[0], 3)
        self.assertIsNone(ha_agent.Store(self.dir_a / "project" / ".opnexus-ha" / "agent").cluster())
        self.assertTrue(ha_agent.Store(self.dir_b / "project" / ".opnexus-ha" / "agent").pairing_open())  # nothing reached B

    def test_a_failed_callback_leaves_the_primary_unpaired(self):
        code = self.code_b()
        status, _, err = self.run_cli("a", "pair", code, "--own-pin", "0" * 64, "--own-port", str(self.port_a))
        self.assertEqual(status, 5)
        self.assertIn("erreicht dieses Gate nicht", err)
        self.assertIsNone(ha_agent.Store(self.dir_a / "project" / ".opnexus-ha" / "agent").cluster())
        self.assertIsNone(ha_agent.Store(self.dir_b / "project" / ".opnexus-ha" / "agent").cluster())

    def test_garbage_and_foreign_codes(self):
        self.assertEqual(self.run_cli("a", "hello", "nonsense")[0], 2)
        self.assertEqual(self.run_cli("a", "pair", "OPNX1-AAAA", "--own-pin", self.pin_a)[0], 2)
        self.assertEqual(self.run_cli("a", "call", "shell")[0], 2)
        self.assertEqual(self.run_cli("a", "call", "status")[0], 1)  # not paired

    def test_a_second_pairing_attempt_is_refused_locally(self):
        self.pair()
        status, _, err = self.run_cli("a", "pair", self.code_b(), "--own-pin", self.pin_a, "--own-port", str(self.port_a))
        self.assertEqual(status, 1)
        self.assertIn("schon gekoppelt", err)

    def test_unpair_forgets_the_partner_here(self):
        self.pair()
        self.assertEqual(self.run_cli("a", "unpair")[0], 0)
        self.assertEqual(self.run_cli("a", "status")[1].strip(), "paired=no")
        self.assertEqual(self.run_cli("a", "call", "status")[0], 1)

    def test_gate_reports_whether_the_certificate_changed_so_a_running_gate_can_be_restarted(self):
        first = self.kv(self.run_cli("b", "gate", "--ip", "127.0.0.1")[1])
        self.assertEqual(first["changed"], "no")                       # setUp made the certificate already
        again = self.kv(self.run_cli("b", "gate", "--ip", "127.0.0.1")[1])
        self.assertEqual((again["changed"], again["pin"]), ("no", first["pin"]))
        moved = self.kv(self.run_cli("b", "gate", "--ip", "127.0.0.2")[1])
        self.assertEqual(moved["changed"], "yes")
        self.assertNotEqual(moved["pin"], first["pin"])
        self.assertEqual(self.kv(self.run_cli("b", "prepare", "--ip", "127.0.0.2")[1])["changed"], "no")
        self.assertEqual(self.kv(self.run_cli("b", "prepare", "--ip", "127.0.0.1")[1])["changed"], "yes")

    def test_the_code_can_come_from_stdin_so_it_stays_out_of_the_process_list(self):
        code = self.code_b()
        status, out, _ = self.run_cli("a", "hello", "-", stdin=(code + "\n").encode())
        self.assertEqual((status, self.kv(out)["version"]), (0, "1.37.0"))
        self.assertEqual(self.run_cli("a", "parse", "-", stdin=(code + "\n").encode())[1].split(), ["ip=127.0.0.1", f"port={self.port_b}"])
        self.assertEqual(self.run_cli("a", "hello", "-", stdin=b"garbage\n")[0], 2)

    def test_a_large_stream_is_spooled_through_a_file_and_arrives_whole(self):
        self.pair()
        blob = os.urandom(6 * 1024 * 1024)
        status, _, err = self.run_cli("a", "call", "receive/data", stdin=blob)
        self.assertEqual(status, 0, err)
        self.assertEqual(Path(str(self.tmp / "recv") + ".data").read_bytes(), blob)

    def test_short_verbs_wait_longer_than_the_agent_lets_them_run(self):
        self.pair()
        seen = {}
        real = ha_cli._peer_client
        ha_cli._peer_client = lambda timeout=30.0: seen.setdefault("t", timeout) and None
        try:
            for verb, floor in (("fence", 240), ("status", 90), ("receive/stamp", 30), ("ping", 120)):
                seen.clear()
                self.run_cli("a", "call", verb, *(["--param", "1"] if verb == "receive/stamp" else []))
                self.assertGreaterEqual(seen["t"], floor + 30, verb)
        finally:
            ha_cli._peer_client = real

    # -- Aufträge verfolgen: Lücken ja, aber nicht endlos ---------------------------------------------------
    def follow(self, replies, gap=0.3):
        calls = iter(replies)
        real = ha_cli._call
        ha_cli.POLL_SECONDS, ha_cli.JOB_GAP_SECONDS = 0.02, gap
        ha_cli._call = lambda *a, **k: next(calls)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = ha_cli._follow_job(object())
        finally:
            ha_cli._call = real
        return code, stdout.getvalue(), stderr.getvalue()

    DONE = (200, {"job": {"state": "succeeded", "exit_code": 0}, "log": ["fertig"]})

    def test_gaps_from_restarts_are_waited_out_including_a_502_from_the_gate(self):
        code, out, _ = self.follow([4, (502, {}), 4, (503, {}), (200, {"job": {"state": "running"}, "log": ["eins"]}),
                                  (200, {"job": {"state": "succeeded", "exit_code": 0}, "log": ["eins", "fertig"]})])   # the log is cumulative
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["eins", "fertig"])

    def test_a_partner_that_stays_silent_ends_the_wait_with_a_clear_message(self):
        def endless():   # bounded, so that a missing deadline fails the test instead of hanging it
            for _ in range(500):
                yield 4
            raise AssertionError("the wait for a silent partner never ended")
        code, _, err = self.follow(endless())
        self.assertEqual(code, 4)
        self.assertIn("nicht gemeldet", err)

    def test_a_real_error_status_is_a_failure_at_once_and_a_failed_job_keeps_its_exit_code(self):
        self.assertEqual(self.follow([(500, {})])[0], 1)
        self.assertEqual(self.follow([(401, {})])[0], 1)
        self.assertEqual(self.follow([(200, {"job": {"state": "failed", "exit_code": 7}, "log": []})])[0], 7)

    def test_the_gap_clock_restarts_after_a_good_answer(self):
        # many one-poll gaps, each cut short by a good answer: together they last longer than the allowed gap, none does alone
        running = (200, {"job": {"state": "running"}, "log": []})
        code, _, _ = self.follow([4, running, 4, running, 4, running, 4, running, 4, running, 4, self.DONE], gap=0.1)
        self.assertEqual(code, 0)

    def test_the_gate_certificate_helper_rejects_non_addresses(self):
        for bad in ("127.0.0.1,DNS:evil", "x", "1.2.3", "127.0.0.1\nfoo"):
            with self.assertRaises(ValueError):
                ha_net.make_gate_cert(self.tmp / "bad", bad)


if __name__ == "__main__":
    unittest.main()
