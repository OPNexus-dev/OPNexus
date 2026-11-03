"""Netzschicht des HA-Agenten (ha_net.py): Zertifikat und gepinnter Client. Braucht das Programm `openssl`.
Run: cd updater && python3 -m unittest test_ha_net"""
import json
import shutil
import ssl
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ha_net

seen: list[dict] = []


class Echo(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        out = json.dumps({"ok": True, "len": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _reply


class Tls(unittest.TestCase):
    def setUp(self):
        seen.clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pin = ha_net.make_gate_cert(self.tmp / "gate", "127.0.0.1")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.tmp / "gate" / "cert.pem", self.tmp / "gate" / "key.pem")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def test_the_certificate_is_new_ec_private_key_and_pin_is_a_sha256(self):
        self.assertRegex(self.pin, r"^[0-9a-f]{64}$")
        self.assertEqual(ha_net.cert_pin((self.tmp / "gate" / "cert.pem").read_text()), self.pin)

    def test_an_existing_certificate_for_the_same_address_is_kept(self):
        self.assertEqual(ha_net.make_gate_cert(self.tmp / "gate", "127.0.0.1"), self.pin)

    def test_a_certificate_for_another_address_is_replaced(self):
        self.assertNotEqual(ha_net.make_gate_cert(self.tmp / "gate", "127.0.0.2"), self.pin)

    def test_the_matching_pin_lets_the_request_through_with_the_bearer(self):
        status, body = ha_net.PeerClient("127.0.0.1", self.port, self.pin, token="geheim").request("POST", "/ha/peer/fence", params={})
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(seen[0]["auth"], "Bearer geheim")
        self.assertEqual(seen[0]["path"], "/ha/peer/fence")

    def test_a_wrong_pin_sends_nothing_at_all(self):
        with self.assertRaises(ha_net.PinMismatch):
            ha_net.PeerClient("127.0.0.1", self.port, "0" * 64, token="geheim").request("POST", "/ha/peer/fence", params={})
        self.assertEqual(seen, [], "the server must not have seen a request, let alone the token")

    def test_a_pin_in_upper_case_or_with_a_prefix_is_no_match(self):
        for pin in (self.pin.upper(), "sha256:" + self.pin, ""):
            with self.assertRaises(ha_net.PinMismatch, msg=pin):
                ha_net.PeerClient("127.0.0.1", self.port, pin).request("GET", "/ha/peer/status")
        self.assertEqual(seen, [])

    def test_a_raw_body_goes_through_with_its_length(self):
        status, body = ha_net.PeerClient("127.0.0.1", self.port, self.pin, token="t").request("POST", "/ha/peer/receive/data", body=b"x" * 5000)
        self.assertEqual((status, body["len"]), (200, 5000))
        self.assertEqual(len(seen[0]["body"]), 5000)

    def test_an_address_that_does_not_answer_is_a_peer_error_not_a_pin_error(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        with self.assertRaises(ha_net.PeerError):
            ha_net.PeerClient("127.0.0.1", self.port, self.pin, timeout=2).request("GET", "/ha/peer/status")

    def test_fetch_pin_reports_what_the_peer_shows_without_sending_anything(self):
        self.assertEqual(ha_net.PeerClient("127.0.0.1", self.port, "0" * 64).fetch_pin(), self.pin)
        self.assertEqual(seen, [])

    def test_a_server_that_only_speaks_old_tls_is_refused(self):
        old = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        old.maximum_version = ssl.TLSVersion.TLSv1_2
        old.load_cert_chain(self.tmp / "gate" / "cert.pem", self.tmp / "gate" / "key.pem")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        httpd.socket = old.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with self.assertRaises((ha_net.PeerError, ha_net.PinMismatch)):
            ha_net.PeerClient("127.0.0.1", httpd.server_address[1], self.pin, timeout=3).request("GET", "/ha/peer/status")
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
