"""Partner-Schnittstelle des Updaters (server.py: /ha/pair, /ha/peer) mit einem Fake-Skript statt der echten CLI.
Run: cd updater && python3 -m unittest test_server_ha"""
import http.client
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import ha_agent
import ha_net
import server

FAKE = """#!/bin/sh
echo "$*" >> "$FAKE_CALLS"
case "$1" in
  status)
    echo "running_version=1.37.0"; echo "db_revision=0034"; echo "host_count=${FAKE_HOSTS-3}"; echo "service.backend=healthy" ;;
  ha)
    case "$2" in
      status) echo "role=standby"; echo "node=b" ;;
      vip) exit "${FAKE_VIP_EXIT:-0}" ;;
      sync) if [ "$3" = "--quiet" ]; then exit "${FAKE_SYNC_EXIT:-0}"; else echo "abgleich"; sleep "${FAKE_SLEEP:-0.1}"; exit "${FAKE_SYNC_EXIT:-0}"; fi ;;
      suggest) echo "node_ip=192.168.2.21"; echo "dev=eth0"; exit "${FAKE_SUGGEST_EXIT:-0}" ;;
      prepare) exit "${FAKE_PREPARE_EXIT:-0}" ;;
      init) read code; echo "$code" > "$FAKE_RECV.initcode"; echo "init-lauf"; sleep "${FAKE_SLEEP:-0.1}"; exit "${FAKE_EXIT:-0}" ;;
      switch|rebuild-standby|dissolve) echo "arbeite $2 $3 $4"; sleep "${FAKE_SLEEP:-0.1}"; exit "${FAKE_EXIT:-0}" ;;
      fence) echo "gestoppt" ;;
      promote|resync|update-standby) echo "arbeite $2 $3 $4 actor=${OPNEXUS_HA_ACTOR:-}"; sleep "${FAKE_SLEEP:-0.3}"; exit "${FAKE_EXIT:-0}" ;;
      receive)
        case "$3" in
          data|env|ca) cat > "$FAKE_RECV.$3" && touch "$FAKE_RECV.$3.done" ;;
          stamp) echo "$4" > "$FAKE_RECV.stamp" ;;
          ca-sum) echo "feedface" ;;
        esac ;;
    esac ;;
esac
exit 0
"""
CLUSTER = "ab" * 32
ACCOUNT = "3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b"   # the id of the account that clicked "take over" (never its free-form name)
PIN = "cd" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "state").mkdir()
        (root / "project").mkdir()
        (root / "project" / ".env").write_text("OPNEXUS_IMAGE_REPO=reg.test/opnexus\nOPNEXUS_VERSION=1.37.0\n")
        script = root / "opnexus"
        script.write_text(FAKE)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.calls = root / "calls.txt"
        os.environ["FAKE_CALLS"] = str(self.calls)
        os.environ["FAKE_RECV"] = str(root / "recv")
        for key in ("FAKE_EXIT", "FAKE_SLEEP"):
            os.environ.pop(key, None)
        state = root / "state"
        server.STATE_DIR = state
        server.TOKEN_FILE = state / "token"
        server.JOB_FILE = state / "job.json"
        server.LOG_FILE = state / "job.log"
        server.PHASE_FILE = state / "job.phases"
        server.HA_JOB_FILE = state / "ha-job.json"
        server.HA_LOG_FILE = state / "ha-job.log"
        server.PROJECT_DIR = root / "project"
        server.SCRIPT = str(script)
        server._status_cache.update(at=0.0, value=None)
        self.token = server.load_token()
        server.Handler.token = self.token
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.store = server.ha_store()
        self.root = root

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        deadline = time.time() + 5
        while server._lock.locked() and time.time() < deadline:
            time.sleep(0.05)
        self.tmp.cleanup()

    def req(self, method, path, body=None, token=None, remote=None, raw=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = dict(extra or {})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if remote is not None:
            headers["X-HA-Remote"] = remote
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        try:
            return response.status, json.loads(payload)
        except ValueError:
            return response.status, {}

    def called(self, needle):
        return self.calls.exists() and needle in self.calls.read_text()

    def pair(self, remote="192.168.2.20"):
        self.store.set_cluster(CLUSTER, remote, 8445, PIN)

    def peer(self, method, name, body=None, **kw):
        return self.req(method, "/ha/peer/" + name, body, token=kw.pop("token", CLUSTER), remote=kw.pop("remote", "192.168.2.20"), **kw)

    def wait_job(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            status, body = self.peer("GET", "job")
            if body.get("job") and body["job"]["state"] != "running":
                return body
            time.sleep(0.1)
        self.fail("job did not finish")


class Access(Base):
    def test_nothing_under_ha_opens_without_the_right_secret(self):
        self.pair()
        self.store.open_pairing()
        for method, path in (("GET", "/ha/peer/status"), ("POST", "/ha/peer/fence"), ("GET", "/ha/peer/ping"), ("POST", "/ha/pair/hello"),
                             ("POST", "/ha/pair/commit"), ("POST", "/ha/peer/receive/data"), ("GET", "/ha/peer/job")):
            for token in (None, "", "wrong", self.token):  # the updater's own token is no key either
                self.assertEqual(self.req(method, path, {} if method == "POST" else None, token=token, remote="192.168.2.20")[0], 401, f"{method} {path} {token}")
        self.assertFalse(self.called("fence"))

    def test_the_cluster_secret_is_no_key_for_the_backend_api(self):
        self.pair()
        for path in ("/status",):
            self.assertEqual(self.req("GET", path, token=CLUSTER)[0], 401)
        self.assertEqual(self.req("POST", "/update", {"version": "1.37.1"}, token=CLUSTER)[0], 401)

    def test_the_updater_token_still_works_for_its_own_api_and_unknown_ha_paths_are_closed(self):
        self.assertEqual(self.req("GET", "/status", token=self.token)[0], 200)
        for path in ("/ha/", "/ha/local/", "/ha/local/x", "/ha/peer", "/ha/x"):
            self.assertIn(self.req("GET", path, token=self.token, remote="192.168.2.20")[0], (401, 404), path)
        self.assertEqual(self.req("GET", "/ha/local/status", token=self.token)[0], 200)   # the token opens the local door (and only that one)

    def test_the_partner_must_come_from_its_paired_address(self):
        self.pair()
        self.assertEqual(self.peer("GET", "ping")[0], 200)
        for remote in ("192.168.2.99", "", None):
            self.assertEqual(self.req("GET", "/ha/peer/ping", token=CLUSTER, remote=remote)[0], 401, str(remote))

    def test_without_a_pairing_the_peer_api_is_closed(self):
        self.assertEqual(self.peer("GET", "ping")[0], 401)


class Pairing(Base):
    def setUp(self):
        super().setUp()
        self.secret = self.store.open_pairing()
        self.calls_made = []
        outer = self

        class Fake:
            result = (200, {"ok": True})

            def __init__(self, ip, port, pin, token="", timeout=30):
                outer.calls_made.append((ip, port, pin, token))

            def request(self, method, path, *a, **k):
                outer.calls_made.append((method, path))
                if isinstance(Fake.result, Exception):
                    raise Fake.result
                return Fake.result

        self.Fake = Fake
        self.real = server.ha_net.PeerClient
        server.ha_net.PeerClient = Fake
        self.addCleanup(lambda: setattr(server.ha_net, "PeerClient", self.real))

    def commit(self, body=None, secret=None, remote="192.168.2.20"):
        return self.req("POST", "/ha/pair/commit", body if body is not None else {"cluster_secret": CLUSTER, "peer_pin": PIN, "peer_port": 8445},
                        token=(secret or self.secret).hex(), remote=remote)

    def test_hello_gives_the_facts_for_the_preflight(self):
        status, body = self.req("POST", "/ha/pair/hello", {}, token=self.secret.hex(), remote="192.168.2.20")
        self.assertEqual(status, 200)
        self.assertEqual((body["api"], body["version"], body["db_revision"], body["mode"]), (server.HA_API, "1.37.0", "0034", "release"))
        self.assertIsNone(body["ha_role"])
        self.assertGreater(body["disk_free"], 0)
        self.assertEqual(body["hosts"], 3)   # the preflight compares the direction of the setup with it
        self.assertTrue(self.store.pairing_open())  # hello changes nothing, the code stays valid

    def test_an_unreadable_host_count_is_unknown_not_zero(self):
        for raw in ("", "x", "-1", "3; rm"):
            os.environ["FAKE_HOSTS"] = raw
            server._status_cache["value"] = None
            body = self.req("POST", "/ha/pair/hello", {}, token=self.secret.hex(), remote="192.168.2.20")[1]
            self.assertIsNone(body["hosts"], raw)
        os.environ.pop("FAKE_HOSTS", None)

    def test_a_commit_stores_the_pairing_calls_back_and_uses_the_code_up(self):
        status, body = self.commit()
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.store.cluster(), {"secret": CLUSTER, "peer_ip": "192.168.2.20", "peer_port": 8445, "peer_pin": PIN})
        self.assertEqual(self.calls_made, [("192.168.2.20", 8445, PIN, CLUSTER), ("GET", "/ha/peer/ping")])
        self.assertFalse(self.store.pairing_open())
        self.assertEqual(self.commit()[0], 401)  # the code is gone

    def test_a_failed_callback_undoes_the_pairing_and_keeps_the_code(self):
        for failure in ((502, {}), (401, {}), ha_net.PeerError("x"), ha_net.PinMismatch("x")):
            self.Fake.result = failure
            status, body = self.commit()
            self.assertEqual((status, body), (502, {"error": "callback_failed"}), repr(failure))
            self.assertIsNone(self.store.cluster())
        self.assertTrue(self.store.pairing_open())

    def test_bad_input_stores_nothing(self):
        for body in ({}, {"cluster_secret": "zz", "peer_pin": PIN, "peer_port": 8445}, {"cluster_secret": CLUSTER, "peer_pin": "x", "peer_port": 8445},
                     {"cluster_secret": CLUSTER, "peer_pin": PIN, "peer_port": 0}, {"cluster_secret": CLUSTER, "peer_pin": PIN, "peer_port": "abc"}):
            self.assertEqual(self.commit(body)[0], 400, str(body))
        self.assertEqual(self.commit(remote="not an ip")[0], 400)
        self.assertEqual(self.commit(remote="")[0], 400)
        self.assertIsNone(self.store.cluster())
        self.assertEqual(self.calls_made, [])

    def test_a_node_that_is_paired_or_already_in_a_pair_refuses(self):
        self.store.set_cluster("ef" * 32, "192.168.2.50", 8445, PIN)
        self.assertEqual(self.commit()[0], 409)
        self.assertEqual(self.store.cluster()["peer_ip"], "192.168.2.50")
        self.store.clear_cluster()
        (self.root / "project" / ".opnexus-ha").mkdir(exist_ok=True)
        (self.root / "project" / ".opnexus-ha" / "state").write_text("HA_ROLE=primary\n")
        self.assertEqual(self.commit()[0], 409)
        self.assertIsNone(self.store.cluster())

    def test_wrong_secrets_burn_the_code(self):
        for i in range(ha_agent.MAX_ATTEMPTS):
            self.assertEqual(self.req("POST", "/ha/pair/hello", {}, token=bytes([i + 1] * 16).hex(), remote="192.168.2.20")[0], 401)
        self.assertEqual(self.req("POST", "/ha/pair/hello", {}, token=self.secret.hex(), remote="192.168.2.20")[0], 401)

    def test_pairing_paths_take_post_only(self):
        self.assertEqual(self.req("GET", "/ha/pair/hello", token=self.secret.hex(), remote="192.168.2.20")[0], 405)


class Verbs(Base):
    def setUp(self):
        super().setUp()
        self.pair()

    def test_a_short_verb_runs_its_fixed_command_and_returns_the_output(self):
        status, body = self.peer("GET", "status")
        self.assertEqual((status, body["rc"]), (200, 0))
        self.assertIn("role=standby", body["output"])
        self.assertEqual(self.peer("POST", "fence", {})[1]["output"].strip(), "gestoppt")
        self.assertTrue(self.called("ha fence"))

    def test_a_job_runs_in_the_background_and_reports_its_result(self):
        status, body = self.peer("POST", "promote", {})
        self.assertEqual(status, 202)
        self.assertEqual(body["job"]["verb"], "promote")
        done = self.wait_job()
        self.assertEqual((done["job"]["state"], done["job"]["exit_code"]), ("succeeded", 0))
        self.assertTrue(any("arbeite promote --yes" in line for line in done["log"]))
        self.assertTrue(self.called("ha promote --yes"))

    def test_a_failing_job_is_reported_as_failed(self):
        os.environ["FAKE_EXIT"] = "7"
        self.peer("POST", "resync", {})
        done = self.wait_job()
        self.assertEqual((done["job"]["state"], done["job"]["exit_code"]), ("failed", 7))
        self.assertTrue(self.called("ha resync --yes --from-peer"))

    def test_only_one_job_at_a_time(self):
        os.environ["FAKE_SLEEP"] = "1"
        self.assertEqual(self.peer("POST", "promote", {})[0], 202)
        self.assertEqual(self.peer("POST", "resync", {}), (409, {"error": "busy"}))
        self.wait_job()
        self.assertEqual(self.calls.read_text().count("ha resync"), 0)

    def test_a_running_update_blocks_a_job_and_a_running_job_blocks_an_update(self):
        os.environ["FAKE_SLEEP"] = "1"
        self.peer("POST", "promote", {})
        self.assertEqual(self.req("POST", "/update", {"version": "1.37.1"}, token=self.token)[0], 409)
        self.wait_job()

    def test_the_version_for_the_standby_is_checked_and_passed_as_one_argument(self):
        self.assertEqual(self.peer("POST", "update-standby", {"version": "1.37.1"})[0], 202)
        self.wait_job()
        self.assertTrue(self.called("ha update-standby 1.37.1"))
        for bad in ("1.0; rm -rf /", "$(id)", "-x", "", "1.0 --force"):
            self.assertEqual(self.peer("POST", "update-standby", {"version": bad})[0], 400, bad)
        self.assertEqual(self.peer("POST", "update-standby", {})[0], 400)
        self.assertEqual(self.calls.read_text().count("update-standby"), 1)

    def test_unknown_verbs_and_wrong_methods(self):
        for name in ("shell", "status/", "receive", "../status", "dissolve/", "dissolve;id"):
            self.assertEqual(self.peer("GET", name)[0], 404, name)
        self.assertEqual(self.peer("GET", "dissolve")[0], 405)   # a known verb, but only as POST
        self.assertEqual(self.peer("GET", "fence")[0], 405)
        self.assertEqual(self.peer("POST", "status", {})[0], 405)

    def test_a_data_stream_reaches_the_command_byte_for_byte(self):
        blob = bytes(range(256)) * 2000
        status, body = self.peer("POST", "receive/data", raw=blob)
        self.assertEqual((status, body["rc"]), (200, 0))
        self.assertEqual(Path(str(self.root / "recv") + ".data").read_bytes(), blob)

    def test_a_stream_parameter_comes_from_the_header_and_is_checked(self):
        good = "a" * 64
        self.assertEqual(self.peer("POST", "receive/ca", raw=b"tar", extra={"X-HA-Param": good})[0], 200)
        self.assertTrue(self.called(f"ha receive ca {good}"))
        before = self.calls.read_text()
        for bad in ("", "A" * 64, "a" * 63, "a; id"):
            self.assertEqual(self.peer("POST", "receive/ca", raw=b"tar", extra={"X-HA-Param": bad})[0], 400, bad)
        self.assertEqual(self.calls.read_text(), before)

    def test_a_stream_is_refused_while_a_job_runs_and_needs_a_length(self):
        os.environ["FAKE_SLEEP"] = "1"
        self.peer("POST", "promote", {})
        self.assertEqual(self.peer("POST", "receive/data", raw=b"x"), (409, {"error": "busy"}))
        self.wait_job()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/ha/peer/receive/data")
        conn.putheader("Authorization", "Bearer " + CLUSTER)
        conn.putheader("X-HA-Remote", "192.168.2.20")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders(b"1\r\nx\r\n0\r\n\r\n")
        self.assertEqual(conn.getresponse().status, 411)

    def test_an_oversized_stream_is_refused_before_reading(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/ha/peer/receive/data")
        conn.putheader("Authorization", "Bearer " + CLUSTER)
        conn.putheader("X-HA-Remote", "192.168.2.20")
        conn.putheader("Content-Length", str(server.HA_MAX_STREAM + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        self.assertFalse(self.called("receive"))

    def test_the_stamp_and_the_ca_sum_are_short_verbs(self):
        self.assertEqual(self.peer("POST", "receive/stamp", {"value": "1700000000"})[1]["rc"], 0)
        self.assertEqual(Path(str(self.root / "recv") + ".stamp").read_text().strip(), "1700000000")
        self.assertEqual(self.peer("POST", "receive/stamp", {"value": "17;rm"})[0], 400)
        self.assertEqual(self.peer("GET", "receive/ca-sum")[1]["output"].strip(), "feedface")


class ReviewFixes(Base):
    def setUp(self):
        super().setUp()
        self.pair()

    def raw_post(self, path, declared, sent, extra=None):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), 10)
        head = (f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {CLUSTER}\r\nX-HA-Remote: 192.168.2.20\r\n"
                f"Content-Length: {declared}\r\n{extra or ''}\r\n").encode()
        sock.sendall(head + sent)
        sock.shutdown(socket.SHUT_WR)   # the connection ends before the announced length arrived
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
        sock.close()
        return data

    def test_a_stream_that_ends_early_is_killed_and_never_seen_as_complete(self):
        answer = self.raw_post("/ha/peer/receive/env", 100, b"POSTGRES_PASSWORD=ab")   # cut off after two characters
        self.assertIn(b"body_truncated", answer)
        self.assertFalse(Path(str(self.root / "recv") + ".env.done").exists(), "the command finished as if the body were complete")

    def test_a_complete_stream_still_finishes(self):
        body = b"x" * 50
        answer = self.raw_post("/ha/peer/receive/env", len(body), body)
        self.assertIn(b'"rc": 0', answer)
        self.assertTrue(Path(str(self.root / "recv") + ".env.done").exists())

    def test_a_job_that_a_restart_interrupted_does_not_block_the_next_one(self):
        server.write_ha_job({"id": "x", "verb": "resync", "state": "running", "started_at": "t", "finished_at": None, "exit_code": None})
        self.assertEqual(self.peer("POST", "resync", {}), (409, {"error": "busy"}))
        server.mark_interrupted()
        job = server.read_ha_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("neu gestartet", server.HA_LOG_FILE.read_text())
        self.assertEqual(self.peer("POST", "resync", {})[0], 202)
        self.wait_job()

    def test_both_kinds_of_interrupted_jobs_are_repaired(self):
        server.write_job({"id": "u", "action": "update", "state": "running"})
        server.write_ha_job({"id": "h", "verb": "promote", "state": "running"})
        server.mark_interrupted()
        self.assertEqual((server.read_job()["state"], server.read_ha_job()["state"]), ("failed", "failed"))

    def test_the_updater_of_the_standby_renews_itself_after_a_successful_standby_update_only(self):
        calls = []
        real = server.self_update
        server.self_update = lambda: calls.append(1)
        self.addCleanup(lambda: setattr(server, "self_update", real))
        self.peer("POST", "update-standby", {"version": "1.37.1"})
        self.wait_job()
        self.assertEqual(calls, [1])
        os.environ["FAKE_EXIT"] = "7"
        self.peer("POST", "update-standby", {"version": "1.37.2"})
        self.wait_job()
        os.environ.pop("FAKE_EXIT")
        self.peer("POST", "promote", {})
        self.wait_job()
        self.assertEqual(calls, [1], "only a successful update-standby renews the updater")

    def test_the_timeouts_of_the_agent_come_from_the_verb_table(self):
        self.assertEqual(ha_agent.SYNC_TIMEOUTS["fence"], 240)
        self.assertFalse(hasattr(server, "HA_SYNC_TIMEOUT"))


class ConcurrentPairing(Pairing):
    def test_two_commits_at_once_leave_exactly_one_pairing(self):
        results = []
        barrier = threading.Barrier(6)

        def attempt(i):
            barrier.wait()
            results.append(self.commit({"cluster_secret": f"{i:02x}" * 32, "peer_pin": PIN, "peer_port": 8445})[0])

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1, results)
        self.assertTrue(all(code in (200, 401, 409) for code in results), results)
        self.assertIsNotNone(self.store.cluster())


class LocalApi(Base):
    """/ha/local/*: only for the backend of this node (updater token). The gate never forwards these paths."""

    def local(self, method, path, body=None, token="__t__"):
        return self.req(method, path, body if method == "POST" else None, token=self.token if token == "__t__" else token)

    def role(self, role):
        state = self.root / "project" / ".opnexus-ha"
        state.mkdir(exist_ok=True)
        (state / "state").write_text(f"HA_ROLE={role}\n")

    def test_only_the_updater_token_opens_the_local_paths(self):
        self.pair()
        self.role("standby")
        for method, path in (("GET", "/ha/local/status"), ("POST", "/ha/local/promote")):
            for token in (None, "", "wrong", CLUSTER, "00" * 16):
                self.assertEqual(self.req(method, path, {} if method == "POST" else None, token=token, remote="192.168.2.20")[0], 401, f"{path} {token}")
        self.assertEqual(self.local("GET", "/ha/local/status")[0], 200)
        self.assertFalse(self.called("promote"))

    def test_the_status_reports_role_pairing_and_whether_the_partner_agent_answers(self):
        status, body = self.local("GET", "/ha/local/status")
        self.assertEqual((status, body["paired"], body["peer_reachable"], body["ha_role"]), (200, False, None, None))
        self.pair()
        self.role("standby")
        outer = self

        class Fake:
            answer = 200

            def __init__(self, ip, port, pin, token="", timeout=30):
                outer.args = (ip, port, pin, token, timeout)

            def request(self, method, path, *a, **k):
                if isinstance(Fake.answer, Exception):
                    raise Fake.answer
                return Fake.answer, {}

        real = server.ha_net.PeerClient
        server.ha_net.PeerClient = Fake
        self.addCleanup(lambda: setattr(server.ha_net, "PeerClient", real))
        body = self.local("GET", "/ha/local/status")[1]
        self.assertEqual((body["paired"], body["peer"], body["peer_reachable"], body["ha_role"]), (True, "192.168.2.20", True, "standby"))
        self.assertEqual(self.args[:4], ("192.168.2.20", 8445, PIN, CLUSTER))
        self.assertLessEqual(self.args[4], 5)   # a dead partner must not hold the page up
        for failure in (server.ha_net.PeerError("x"), server.ha_net.PinMismatch("x"), 401):
            Fake.answer = failure
            self.assertIs(self.local("GET", "/ha/local/status")[1]["peer_reachable"], False, repr(failure))

    def test_a_takeover_starts_the_fixed_command_with_the_actor_in_its_environment(self):
        self.role("standby")
        status, body = self.local("POST", "/ha/local/promote", {"force": False, "actor": ACCOUNT})
        self.assertEqual((status, body["job"]["verb"]), (202, "promote"))
        done = self.wait_local_job()
        self.assertEqual(done["job"]["state"], "succeeded")
        self.assertIn(f"arbeite promote --yes  actor={ACCOUNT}", "\n".join(done["log"]))
        self.assertTrue(self.called("ha promote --yes"))
        self.assertFalse(self.called("--force"))

    def test_force_adds_the_flag_and_only_force(self):
        self.role("standby")
        self.local("POST", "/ha/local/promote", {"force": True, "actor": ACCOUNT})
        self.wait_local_job()
        self.assertTrue(self.called("ha promote --yes --force"))

    def wait_local_job(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            body = self.local("GET", "/ha/local/status")[1]
            if body.get("job") and body["job"]["state"] != "running":
                return body
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_only_a_standby_takes_over(self):
        for role in (None, "primary", "fenced"):
            if role:
                self.role(role)
            self.assertEqual(self.local("POST", "/ha/local/promote", {"force": False, "actor": ACCOUNT}), (409, {"error": "not_standby"}), role)
        self.assertFalse(self.called("promote"))

    def test_actor_and_force_are_validated_before_anything_runs(self):
        self.role("standby")
        for body in ({}, {"force": False}, {"force": "yes", "actor": ACCOUNT}, {"force": 1, "actor": ACCOUNT},
                     {"force": False, "actor": ""}, {"force": False, "actor": "a b"}, {"force": False, "actor": "x;rm -rf /"},
                     {"force": False, "actor": "$(id)"}, {"force": False, "actor": "x" * 151}, {"force": False, "actor": 5},
                     {"force": False, "actor": "a\nb"}, {"force": False, "actor": "julian"}, {"force": False, "actor": "Julian Boldt"},
                     {"force": False, "actor": ACCOUNT.upper()}, {"force": False, "actor": ACCOUNT + "0"}, {"force": False, "actor": ACCOUNT + "\n"}):
            self.assertEqual(self.local("POST", "/ha/local/promote", body)[0], 400, body)
        self.assertFalse(self.called("promote"))
        for good in (ACCOUNT, str(uuid.uuid4())):   # a missing force means: not forced
            self.wait_idle()
            self.assertEqual(self.local("POST", "/ha/local/promote", {"force": False, "actor": good})[0], 202, good)

    def wait_idle(self):
        deadline = time.time() + 10
        while server._lock.locked() and time.time() < deadline:
            time.sleep(0.05)

    def test_a_running_job_makes_a_second_takeover_busy(self):
        self.role("standby")
        os.environ["FAKE_SLEEP"] = "1"
        self.assertEqual(self.local("POST", "/ha/local/promote", {"force": False, "actor": ACCOUNT})[0], 202)
        self.assertEqual(self.local("POST", "/ha/local/promote", {"force": False, "actor": ACCOUNT}), (409, {"error": "busy"}))
        self.wait_local_job()

    def test_the_gate_style_paths_do_not_reach_the_local_handlers(self):
        self.pair()
        self.role("standby")
        # with the updater token on the peer path: no (the peer path wants the cluster secret) ...
        self.assertEqual(self.req("POST", "/ha/peer/promote", {}, token=self.token, remote="192.168.2.20")[0], 401)
        # ... and the cluster secret on a local path: no
        self.assertEqual(self.req("POST", "/ha/local/promote", {"force": False, "actor": ACCOUNT}, token=CLUSTER, remote="192.168.2.20")[0], 401)


class LocalOps(Base):
    """/ha/local/suggest, prepare, init, action: what the pair card in the settings runs on this node (updater token only)."""
    CODE = "OPNX1-AHAKQ-AQVED-6RNLN-NHIFB-JOM4S-XRDPF-RMO32"

    def setUp(self):
        super().setUp()
        server.HA_GATE_DIR = self.root / "gate"
        server.HA_TIMER_LOG = self.root / "state" / "ha-timer.log"

    def role(self, role):
        state = self.root / "project" / ".opnexus-ha"
        state.mkdir(exist_ok=True)
        if role is None:
            (state / "state").unlink(missing_ok=True)
        else:
            (state / "state").write_text(f"HA_ROLE={role}\n")

    def local(self, path, body=None, method="POST", token="__t__"):
        return self.req(method, path, body if method == "POST" else None, token=self.token if token == "__t__" else token)

    def wait_job(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            body = self.local("/ha/local/status", method="GET")[1]
            if body.get("job") and body["job"]["state"] != "running":
                return body
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_every_local_operation_needs_the_updater_token(self):
        for path in ("/ha/local/suggest", "/ha/local/prepare", "/ha/local/init", "/ha/local/action"):
            for token in (None, "", "wrong", CLUSTER):
                self.assertEqual(self.local(path, {}, token=token)[0], 401, f"{path} {token}")
        self.assertFalse(self.calls.exists())

    # -- suggest ------------------------------------------------------------------------------------
    def test_suggest_returns_address_and_interface_and_validates_the_target(self):
        self.assertEqual(self.local("/ha/local/suggest", {"target": "192.168.2.20"}), (200, {"node_ip": "192.168.2.21", "dev": "eth0"}))
        self.assertTrue(self.called("ha suggest 192.168.2.20"))
        self.assertEqual(self.local("/ha/local/suggest", {})[0], 200)
        self.assertTrue(self.called("ha suggest 1.1.1.1"))
        for bad in ("x", "999.1.1.1", "1.2.3.4;id", 5, ["1.1.1.1"], "1.1.1.1\n"):
            self.assertEqual(self.local("/ha/local/suggest", {"target": bad})[0], 400, repr(bad))
        os.environ["FAKE_SUGGEST_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_SUGGEST_EXIT", None)
        self.assertEqual(self.local("/ha/local/suggest", {"target": "1.1.1.1"}), (502, {"error": "suggest_failed"}))

    # -- prepare ------------------------------------------------------------------------------------
    def test_prepare_brings_the_gate_up_and_returns_a_code_for_this_node(self):
        status, body = self.local("/ha/local/prepare", {"ip": "127.0.0.1"})
        self.assertEqual(status, 200)
        parts = ha_agent.decode_code(body["code"])
        self.assertEqual((parts["ip"], parts["port"]), ("127.0.0.1", 8445))
        self.assertEqual(parts["pin"].hex(), ha_net.cert_pin((self.root / "gate" / "cert.pem").read_text()))
        self.assertEqual((body["expires_in"], body["ip"]), (ha_agent.CODE_TTL, "127.0.0.1"))
        self.assertTrue(self.called("ha prepare --gate-only --ip 127.0.0.1"))
        self.assertTrue(self.store.pairing_open())
        self.assertTrue(self.store.check_pairing(parts["secret"]))                 # the code really is the open one
        self.assertNotIn(body["code"], self.calls.read_text())                      # never in a command line or a log
        for path in (server.HA_LOG_FILE, server.HA_TIMER_LOG):
            self.assertFalse(path.exists() and body["code"] in path.read_text())

    def test_prepare_is_refused_for_a_node_in_a_pair_and_bad_addresses_and_a_failing_gate(self):
        self.role("standby")
        self.assertEqual(self.local("/ha/local/prepare", {"ip": "127.0.0.1"}), (409, {"error": "already_in_pair"}))
        self.role(None)
        for bad in ({}, {"ip": "x"}, {"ip": "127.0.0.1;id"}, {"ip": "300.1.1.1"}, {"ip": 5}, {"ip": "127.0.0.1\n"}):
            self.assertEqual(self.local("/ha/local/prepare", bad)[0], 400, bad)
        os.environ["FAKE_PREPARE_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_PREPARE_EXIT", None)
        self.assertEqual(self.local("/ha/local/prepare", {"ip": "127.0.0.1"}), (502, {"error": "prepare_failed"}))
        self.assertFalse(self.store.pairing_open())    # no code was opened when the gate did not come up

    def test_a_second_prepare_replaces_the_first_code(self):
        first = ha_agent.decode_code(self.local("/ha/local/prepare", {"ip": "127.0.0.1"})[1]["code"])
        second = ha_agent.decode_code(self.local("/ha/local/prepare", {"ip": "127.0.0.1"})[1]["code"])
        self.assertFalse(self.store.check_pairing(first["secret"]))
        self.assertTrue(self.store.check_pairing(second["secret"]))

    # -- init ---------------------------------------------------------------------------------------
    def test_init_is_a_job_that_gets_the_code_on_stdin_and_never_on_the_command_line(self):
        status, body = self.local("/ha/local/init", {"code": self.CODE, "vip": "192.168.2.22/24", "node": "a", "peer_node": "b",
                                                     "node_ip": "192.168.2.20", "vip_dev": "eth0"})
        self.assertEqual((status, body["job"]["verb"]), (202, "init"))
        done = self.wait_job()
        self.assertEqual(done["job"]["state"], "succeeded")
        self.assertTrue(self.called("ha init --code - --vip 192.168.2.22/24 --yes --node a --peer-node b --node-ip 192.168.2.20 --vip-dev eth0"))
        self.assertNotIn(self.CODE, self.calls.read_text())
        self.assertEqual(Path(str(self.root / "recv") + ".initcode").read_text().strip(), self.CODE)
        self.assertNotIn(self.CODE, "\n".join(done["log"]))

    def test_the_check_variant_adds_check_and_is_its_own_job_name(self):
        status, body = self.local("/ha/local/init", {"code": self.CODE, "vip": "192.168.2.22/24", "check": True})
        self.assertEqual((status, body["job"]["verb"]), (202, "init-check"))
        self.wait_job()
        self.assertTrue(self.called("ha init --code - --vip 192.168.2.22/24 --yes --check"))
        self.assertFalse(self.called("--node "))

    def test_init_validates_every_input_before_anything_runs(self):
        good = {"code": self.CODE, "vip": "192.168.2.22/24"}
        for bad in ({}, {"code": self.CODE}, {"vip": "192.168.2.22/24"}, {**good, "vip": "192.168.2.22"}, {**good, "vip": "192.168.2.22/33"},
                    {**good, "code": "OPNX1-AAAA; rm -rf /"}, {**good, "code": "OPNX1\nHA"}, {**good, "code": "x"}, {**good, "code": "A" * 201},
                    {**good, "node": "Node A"}, {**good, "node": "a;b"}, {**good, "peer_node": ""}, {**good, "node_ip": "x"},
                    {**good, "vip_dev": "eth0 -x"}, {**good, "vip_dev": "a" * 40}, {**good, "check": "yes"}, {**good, "check": 1},
                    # `$` alone accepts one trailing newline: every field is matched whole
                    {**good, "code": self.CODE + "\n"}, {**good, "vip": "192.168.2.22/24\n"}, {**good, "node": "a\n"}, {**good, "peer_node": "b\n"},
                    {**good, "node_ip": "192.168.2.21\n"}, {**good, "vip_dev": "eth0\n"}):
            self.assertEqual(self.local("/ha/local/init", bad)[0], 400, bad)
        self.assertFalse(self.calls.exists())

    def test_init_is_refused_on_a_node_that_is_in_a_pair_or_busy(self):
        self.role("primary")
        self.assertEqual(self.local("/ha/local/init", {"code": self.CODE, "vip": "192.168.2.22/24"}), (409, {"error": "already_in_pair"}))
        self.role(None)
        os.environ["FAKE_SLEEP"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        self.assertEqual(self.local("/ha/local/init", {"code": self.CODE, "vip": "192.168.2.22/24"})[0], 202)
        self.assertEqual(self.local("/ha/local/init", {"code": self.CODE, "vip": "192.168.2.22/24"}), (409, {"error": "busy"}))
        self.wait_job()

    # -- actions ------------------------------------------------------------------------------------
    def test_each_action_runs_exactly_its_fixed_command_and_only_on_the_primary(self):
        self.role("primary")
        cases = {"sync": "ha sync", "switch": "ha switch", "rebuild": "ha rebuild-standby", "dissolve": "ha dissolve --yes",
                 "dissolve_local": "ha dissolve --yes --local-only"}
        for action, command in cases.items():
            self.calls.unlink(missing_ok=True)
            status, body = self.local("/ha/local/action", {"action": action})
            self.assertEqual((status, body["job"]["verb"]), (202, action), action)
            self.wait_job()
            self.assertEqual(self.calls.read_text().strip().splitlines()[-1] if False else self.calls.read_text().strip().split("\n")[0], command, action)

    def test_unknown_actions_and_extra_input_do_nothing(self):
        self.role("primary")
        for bad in ({}, {"action": "promote"}, {"action": "sync; id"}, {"action": ["sync"]}, {"action": None}, {"action": "SYNC"}, {"action": "dissolve --local-only"}):
            self.assertEqual(self.local("/ha/local/action", bad)[0], 400, bad)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.local("/ha/local/action", {"action": "sync", "args": "--evil"})[0], 202)   # extra keys are ignored, not passed on
        self.wait_job()
        self.assertEqual(self.calls.read_text().strip().split("\n")[0], "ha sync")

    def test_actions_need_a_primary(self):
        for role in (None, "standby", "fenced"):
            self.role(role)
            self.assertEqual(self.local("/ha/local/action", {"action": "dissolve"}), (409, {"error": "not_primary"}), role)
        self.assertFalse(self.calls.exists())


class Timers(Base):
    """Zeitgeber des Agenten (ersetzen Cron): VIP-Nachführung jede Minute, Abgleich alle 5 Minuten nur am Primär."""

    def setUp(self):
        super().setUp()
        server.HA_TIMER_LOG = self.root / "state" / "ha-timer.log"
        server._ha_timer.update(sync=None, vip=None)
        self.role("primary")
        self.pair()

    def role(self, role):
        state = self.root / "project" / ".opnexus-ha"
        state.mkdir(exist_ok=True)
        (state / "state").write_text(f"HA_ROLE={role}\n")

    def runs(self, verb):
        return self.calls.read_text().count(verb) if self.calls.exists() else 0

    def test_the_primary_runs_both_at_once_then_each_on_its_own_interval(self):
        self.assertEqual(server.ha_timers_step(1000.0), ["vip", "sync"])
        self.assertEqual(server.ha_timers_step(1030.0), [])
        self.assertEqual(server.ha_timers_step(1059.0), [])
        self.assertEqual(server.ha_timers_step(1060.0), ["vip"])            # exactly 60 s after the first run
        self.assertEqual(server.ha_timers_step(1120.0), ["vip"])
        self.assertEqual(server.ha_timers_step(1299.0), ["vip"])            # the sync is 1 s short of its 300 s
        self.assertEqual(server.ha_timers_step(1300.0), ["sync"])           # the vip ran one second ago
        self.assertEqual((self.runs("ha vip reconcile"), self.runs("ha sync --quiet")), (4, 2))

    def test_a_standby_or_fenced_node_only_follows_the_vip(self):
        for role in ("standby", "fenced"):
            server._ha_timer.update(sync=None, vip=None)
            self.role(role)
            self.assertEqual(server.ha_timers_step(5000.0), ["vip"], role)
        self.assertEqual(self.runs("ha sync"), 0)

    def test_nothing_runs_without_a_pairing_or_a_role(self):
        self.store.clear_cluster()
        self.assertEqual(server.ha_timers_step(1000.0), [])
        self.pair()
        (self.root / "project" / ".opnexus-ha" / "state").unlink()
        self.assertEqual(server.ha_timers_step(1000.0), [])
        self.assertFalse(self.calls.exists())

    def test_a_running_job_pauses_the_timers_without_using_up_the_interval(self):
        os.environ["FAKE_SLEEP"] = "1"
        self.peer("POST", "promote", {})
        self.assertEqual(server.ha_timers_step(1000.0), [])
        self.wait_job()
        self.assertEqual(server.ha_timers_step(1001.0), ["vip", "sync"])

    def test_the_pairing_marker_follows_the_pairing_for_the_backend_alarm(self):
        marker = self.root / "state" / "ha-paired"
        server.ha_timers_step(1000.0)
        self.assertTrue(marker.exists())                       # setUp paired the node
        self.store.clear_cluster()
        server.ha_timers_step(2000.0)
        self.assertFalse(marker.exists())                      # unpaired: no agent, no alarm
        self.pair()
        server.ha_timers_step(3000.0)
        self.assertTrue(marker.exists())
        (self.root / "project" / ".opnexus-ha" / "state").unlink()   # no role (not an HA node yet): the marker still tells "paired"
        server.ha_timers_step(4000.0)
        self.assertTrue(marker.exists())

    def test_a_failing_command_is_logged_and_the_timer_carries_on(self):
        os.environ["FAKE_VIP_EXIT"] = "3"
        os.environ["FAKE_SYNC_EXIT"] = "1"
        try:
            self.assertEqual(server.ha_timers_step(1000.0), ["vip", "sync"])
            self.assertEqual(server.ha_timers_step(1061.0), ["vip"])
        finally:
            os.environ.pop("FAKE_VIP_EXIT"), os.environ.pop("FAKE_SYNC_EXIT")
        log = server.HA_TIMER_LOG.read_text()
        self.assertIn("vip: Exit 3", log)
        self.assertIn("sync: Exit 1", log)

    def test_the_log_stays_small(self):
        server.HA_TIMER_LOG_MAX = 500
        for i in range(60):
            server._timer_log(f"zeile {i} " + "x" * 40)
        self.assertLess(server.HA_TIMER_LOG.stat().st_size, 1200)
        self.assertIn("zeile 59", server.HA_TIMER_LOG.read_text())
        server.HA_TIMER_LOG_MAX = 200_000


if __name__ == "__main__":
    unittest.main()
