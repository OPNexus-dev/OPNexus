"""Kern des HA-Agenten (ha_agent.py): Kopplungscode, Einmal-Code, Clustergeheimnis, Anfrageprüfung, feste Verben.
Run: cd updater && python3 -m unittest test_ha_agent"""
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path

import ha_agent as agent

SECRET = bytes(range(16))
PIN = bytes(range(32, 64))
CLUSTER = "ab" * 32
PIN_HEX = "cd" * 32


class Code(unittest.TestCase):
    def test_roundtrip_keeps_every_part(self):
        code = agent.encode_code("192.168.2.21", 8445, SECRET, PIN)
        self.assertTrue(code.startswith("OPNX1-"))
        out = agent.decode_code(code)
        self.assertEqual((out["ip"], out["port"], out["secret"], out["pin"]), ("192.168.2.21", 8445, SECRET, PIN))

    def test_case_spaces_and_dashes_do_not_matter(self):
        code = agent.encode_code("10.0.0.5", 443, SECRET, PIN)
        mangled = " " + code.replace("-", " ").lower().replace("opnx1", "OPNX1") + "\n"
        self.assertEqual(agent.decode_code(mangled)["ip"], "10.0.0.5")

    def test_a_single_wrong_character_is_caught(self):
        code = agent.encode_code("10.0.0.5", 443, SECRET, PIN)
        body = code[len("OPNX1-"):]
        for i in (0, 7, len(body) // 2, len(body) - 3):
            if body[i] == "-":
                continue
            swapped = "A" if body[i] != "A" else "B"
            with self.assertRaises(agent.CodeError, msg=f"position {i}"):
                agent.decode_code("OPNX1-" + body[:i] + swapped + body[i + 1:])

    def test_wrong_prefix_length_alphabet_and_version_are_refused(self):
        good = agent.encode_code("10.0.0.5", 443, SECRET, PIN)
        for bad in ("", "hello", "OPNX2-" + good[6:], good[:-6], good + "AAAA", "OPNX1-!!!!", "OPNX1-" + "A" * 91):
            with self.assertRaises(agent.CodeError, msg=bad):
                agent.decode_code(bad)

    def test_a_foreign_version_byte_is_refused_even_with_a_valid_checksum(self):
        import base64, binascii
        body = bytes([2]) + bytes([10, 0, 0, 5]) + (443).to_bytes(2, "big") + SECRET + PIN
        raw = body + binascii.crc_hqx(body, 0xFFFF).to_bytes(2, "big")
        with self.assertRaises(agent.CodeError) as ctx:
            agent.decode_code("OPNX1-" + base64.b32encode(raw).decode().rstrip("="))
        self.assertIn("anderen Version", str(ctx.exception))

    def test_invalid_components_are_refused_when_encoding(self):
        for args in (("999.1.1.1", 443, SECRET, PIN), ("10.0.0.5", 0, SECRET, PIN), ("10.0.0.5", 70000, SECRET, PIN),
                     ("10.0.0.5", 443, SECRET[:-1], PIN), ("10.0.0.5", 443, SECRET, PIN[:-1])):
            with self.assertRaises(ValueError, msg=str(args)):
                agent.encode_code(*args)

    def test_two_codes_for_the_same_node_differ(self):
        self.assertNotEqual(agent.encode_code("10.0.0.5", 443, SECRET, PIN), agent.encode_code("10.0.0.5", 443, bytes(16), PIN))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = agent.Store(self.tmp / "agent")

    def test_a_cluster_record_takes_only_exact_forms(self):
        good = ("ab" * 32, "10.0.0.2", 8445, "cd" * 32)
        self.store.set_cluster(*good)
        for secret, pin in (("ab" * 32 + "\n", "cd" * 32), ("ab" * 32, "cd" * 32 + "\n"), ("AB" * 32, "cd" * 32), ("ab" * 31, "cd" * 32)):
            with self.assertRaises(ValueError, msg=repr((secret, pin))):
                self.store.set_cluster(secret, "10.0.0.2", 8445, pin)

    # Einmal-Code
    def test_only_the_hash_of_the_secret_is_stored_private(self):
        secret = self.store.open_pairing(now=1000)
        text = self.store.pairing_file.read_text()
        self.assertNotIn(secret.hex(), text)
        self.assertEqual(stat.S_IMODE(self.store.pairing_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.pairing_file.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.store.pairing_file.parent.iterdir()], ["pairing.json"])

    def test_the_right_secret_passes_and_a_wrong_one_does_not(self):
        secret = self.store.open_pairing(now=1000)
        self.assertTrue(self.store.check_pairing(secret, now=1001))
        self.assertTrue(self.store.check_pairing(secret, now=1002))  # prüfen verbraucht nicht, erst close_pairing
        self.assertFalse(self.store.check_pairing(bytes(16), now=1003))

    def test_the_code_expires(self):
        secret = self.store.open_pairing(now=1000)
        self.assertTrue(self.store.check_pairing(secret, now=1000 + agent.CODE_TTL - 1))
        self.assertFalse(self.store.check_pairing(secret, now=1000 + agent.CODE_TTL))
        self.assertFalse(self.store.pairing_file.exists())

    def test_five_wrong_attempts_burn_the_code_even_for_the_right_secret(self):
        secret = self.store.open_pairing(now=1000)
        for i in range(agent.MAX_ATTEMPTS):
            self.assertFalse(self.store.check_pairing(bytes([i + 1]) * 16, now=1001))
        self.assertFalse(self.store.check_pairing(secret, now=1002))
        self.assertFalse(self.store.pairing_open(now=1002))

    def test_failed_attempts_are_counted_across_processes(self):
        secret = self.store.open_pairing(now=1000)
        for i in range(agent.MAX_ATTEMPTS - 1):
            agent.Store(self.tmp / "agent").check_pairing(bytes([i + 1]) * 16, now=1001)
        self.assertTrue(self.store.pairing_open(now=1002))
        self.store.check_pairing(bytes(16), now=1003)
        self.assertFalse(self.store.check_pairing(secret, now=1004))

    def test_a_new_code_replaces_the_old_one(self):
        old = self.store.open_pairing(now=1000)
        new = self.store.open_pairing(now=1001)
        self.assertFalse(self.store.check_pairing(old, now=1002))
        self.assertTrue(self.store.check_pairing(new, now=1002)) if self.store.pairing_file.exists() else None

    def test_no_code_open_means_nothing_passes(self):
        self.assertFalse(self.store.check_pairing(SECRET, now=1))
        self.assertFalse(self.store.pairing_open(now=1))

    def test_closing_removes_the_code(self):
        secret = self.store.open_pairing(now=1000)
        self.store.close_pairing()
        self.assertFalse(self.store.check_pairing(secret, now=1001))
        self.store.close_pairing()  # a second call is harmless

    def test_a_corrupt_file_is_no_open_code(self):
        self.store.open_pairing(now=1000)
        self.store.pairing_file.write_text("{not json")
        self.assertFalse(self.store.check_pairing(SECRET, now=1001))

    # Kopplung
    def test_cluster_data_roundtrip_private_and_validated(self):
        self.assertIsNone(self.store.cluster())
        self.store.set_cluster(CLUSTER, "192.168.2.20", 8445, PIN_HEX)
        self.assertEqual(self.store.cluster(), {"secret": CLUSTER, "peer_ip": "192.168.2.20", "peer_port": 8445, "peer_pin": PIN_HEX})
        self.assertEqual(stat.S_IMODE(self.store.cluster_file.stat().st_mode), 0o600)
        for bad in (("zz", "192.168.2.20", 8445, PIN_HEX), (CLUSTER, "nope", 8445, PIN_HEX), (CLUSTER, "192.168.2.20", 0, PIN_HEX),
                    (CLUSTER, "192.168.2.20", 8445, "xyz")):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.store.set_cluster(*bad)
        self.store.clear_cluster()
        self.assertIsNone(self.store.cluster())

    def test_an_incomplete_cluster_file_counts_as_not_paired(self):
        self.store.set_cluster(CLUSTER, "192.168.2.20", 8445, PIN_HEX)
        data = json.loads(self.store.cluster_file.read_text())
        del data["peer_pin"]
        self.store.cluster_file.write_text(json.dumps(data))
        self.assertIsNone(self.store.cluster())


class Authorization(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = agent.Store(self.tmp / "agent")

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"} if token is not None else {}

    def test_an_unpaired_node_trusts_nobody(self):
        self.assertFalse(agent.authorize_peer(self.store, self.headers(CLUSTER), "192.168.2.20"))

    def test_the_partner_needs_secret_and_address(self):
        self.store.set_cluster(CLUSTER, "192.168.2.20", 8445, PIN_HEX)
        self.assertTrue(agent.authorize_peer(self.store, self.headers(CLUSTER), "192.168.2.20"))
        self.assertFalse(agent.authorize_peer(self.store, self.headers("00" * 32), "192.168.2.20"))
        self.assertFalse(agent.authorize_peer(self.store, self.headers(CLUSTER), "192.168.2.99"))
        self.assertFalse(agent.authorize_peer(self.store, self.headers(CLUSTER), ""))
        self.assertFalse(agent.authorize_peer(self.store, self.headers(""), "192.168.2.20"))
        self.assertFalse(agent.authorize_peer(self.store, {}, "192.168.2.20"))
        self.assertFalse(agent.authorize_peer(self.store, {"Authorization": CLUSTER}, "192.168.2.20"))  # no Bearer

    def test_pairing_secret_is_hex_of_the_right_length(self):
        secret = self.store.open_pairing()
        self.assertTrue(agent.authorize_pairing(self.store, self.headers(secret.hex())))
        for bad in (None, "", "zz", secret.hex()[:-2], secret.hex() + "00", "00" * 16):
            self.assertFalse(agent.authorize_pairing(self.store, self.headers(bad)), str(bad))

    def test_the_cluster_secret_is_no_pairing_secret_and_vice_versa(self):
        secret = self.store.open_pairing()
        self.store.set_cluster(CLUSTER, "192.168.2.20", 8445, PIN_HEX)
        self.assertFalse(agent.authorize_pairing(self.store, self.headers(CLUSTER)))
        self.assertFalse(agent.authorize_peer(self.store, self.headers(secret.hex()), "192.168.2.20"))

    def test_new_cluster_secrets_are_long_and_unique(self):
        a, b = agent.new_cluster_secret(), agent.new_cluster_secret()
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")


class Verbs(unittest.TestCase):
    def test_a_known_verb_gives_exactly_its_fixed_command(self):
        verb, argv = agent.build_argv("/ha/peer/fence", "POST", {})
        self.assertEqual((verb.kind, argv), ("sync", ["ha", "fence"]))
        self.assertEqual(agent.build_argv("/ha/peer/resync", "POST", None)[1], ["ha", "resync", "--yes", "--from-peer"])

    def test_the_one_parameter_is_validated_and_appended(self):
        self.assertEqual(agent.build_argv("/ha/peer/update-standby", "POST", {"version": "1.37.0"})[1], ["ha", "update-standby", "1.37.0"])
        self.assertEqual(agent.build_argv("/ha/peer/receive/stamp", "POST", {"value": "1700000000"})[1], ["ha", "receive", "stamp", "1700000000"])
        self.assertEqual(agent.build_argv("/ha/peer/receive/ca", "POST", {"sum": "a" * 64})[1], ["ha", "receive", "ca", "a" * 64])

    def test_injection_attempts_in_parameters_are_refused(self):
        for value in ("1.0; rm -rf /", "1.0 --force", "$(id)", "`id`", "-x", "", "a" * 200, "1.0\nha fence", "../etc", 5, None, ["1"]):
            with self.assertRaises(agent.VerbError, msg=repr(value)):
                agent.build_argv("/ha/peer/update-standby", "POST", {"version": value})
        for value in ("A" * 64, "a" * 63, "a" * 65, "a" * 63 + ";", "g" * 64, "a" * 64 + "\n"):   # (`$` alone accepts a trailing newline)
            with self.assertRaises(agent.VerbError, msg=value):
                agent.build_argv("/ha/peer/receive/ca", "POST", {"sum": value})
        for value in ("17;rm", "1700000000\n"):
            with self.assertRaises(agent.VerbError, msg=repr(value)):
                agent.build_argv("/ha/peer/receive/stamp", "POST", {"value": value})

    def test_the_standby_update_takes_only_a_fixed_release_version(self):
        for value in ("latest", "1.37", "1.37.0-rc1", "v1.37.0", "1.37.0.1", "1.37.x", "01.37.0.", "10000.1.1", " 1.37.0", "1.37.0\n"):
            with self.assertRaises(agent.VerbError, msg=value):
                agent.build_argv("/ha/peer/update-standby", "POST", {"version": value})
        self.assertEqual(agent.build_argv("/ha/peer/update-standby", "POST", {"version": "1.37.12"})[1], ["ha", "update-standby", "1.37.12"])

    def test_the_client_waits_longer_than_the_agent_lets_a_short_verb_run(self):
        self.assertGreater(agent.SYNC_TIMEOUTS["fence"], agent.SYNC_TIMEOUTS["status"])
        for name in agent.SYNC_TIMEOUTS:
            self.assertEqual(agent.VERBS[name].kind, "sync", name)

    def test_extra_or_missing_parameters_are_refused(self):
        with self.assertRaises(agent.VerbError):
            agent.build_argv("/ha/peer/fence", "POST", {"x": "1"})
        with self.assertRaises(agent.VerbError):
            agent.build_argv("/ha/peer/update-standby", "POST", {"version": "1.0", "extra": "x"})
        with self.assertRaises(agent.VerbError):
            agent.build_argv("/ha/peer/update-standby", "POST", {})

    def test_unknown_paths_and_wrong_methods(self):
        for path in ("/ha/peer/", "/ha/peer/shell", "/ha/peer/../status", "/ha/peer/status/", "/ha/pair/hello", "/ha/peer/receive", "/update", "ha/peer/status", "/ha/peer/STATUS", "/ha/peer/dissolve/"):
            with self.assertRaises(agent.VerbError, msg=path) as ctx:
                agent.build_argv(path, "GET", None)
            self.assertEqual(ctx.exception.status, 404, path)
        with self.assertRaises(agent.VerbError) as ctx:
            agent.build_argv("/ha/peer/fence", "GET", None)
        self.assertEqual(ctx.exception.status, 405)
        with self.assertRaises(agent.VerbError) as ctx:
            agent.build_argv("/ha/peer/status", "POST", None)
        self.assertEqual(ctx.exception.status, 405)

    def test_every_verb_is_a_plain_ha_command_with_nothing_shell_like(self):
        allowed = {"status", "fence", "promote", "resync", "update-standby", "receive", "dissolve"}
        for name, verb in agent.VERBS.items():
            if verb.kind == "local":
                self.assertEqual((verb.argv, verb.method), ((), "GET"), name)  # answered by the agent itself, never a command
                continue
            self.assertEqual(verb.argv[0], "ha", name)
            self.assertIn(verb.argv[1], allowed, name)
            for part in verb.argv:
                self.assertRegex(part, r"^[A-Za-z0-9-]+$", name)
            self.assertIn(verb.method, ("GET", "POST"))
            self.assertIn(verb.kind, ("sync", "job", "stream", "local"))
            self.assertEqual(verb.param is None, verb.check is None, name)

    def test_nothing_that_wipes_or_starts_is_reachable_without_a_job(self):
        for name in ("promote", "resync", "update-standby", "dissolve"):
            self.assertEqual(agent.VERBS[name].kind, "job")
        for name, verb in agent.VERBS.items():
            if verb.kind == "stream":
                self.assertEqual(verb.argv[1], "receive")


if __name__ == "__main__":
    unittest.main()
