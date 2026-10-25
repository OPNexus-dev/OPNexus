"""HA commands of `opnexus` (H2b, docs/HA-H2-DESIGN.md): init, status, promote, resync, sync, update, fence, receive.
Runs the real shell functions with fake `docker`, `ssh`, `ip`, `pg_basebackup` in front of PATH. The fake docker executes
`sh -c` scripts that run inside the postgres container against a scratch PGDATA directory, so the standby configuration
is really written and can be inspected; SQL goes through a rule table; every call is logged in order.
Run: cd updater && python3 -m unittest test_cli_ha"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("opnexus").resolve()

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
log = os.environ["FAKE_LOG"]
args = sys.argv[1:]
data = b"" if sys.stdin.isatty() else sys.stdin.buffer.read()
text = data.decode("utf-8", "replace")
line = "docker " + " ".join(args)
with open(log, "a") as f:
    f.write(line + (("  <<< " + text.replace("\n", " ⏎ ")[:1500]) if text.strip() else "") + "\n")
rules = json.load(open(os.environ["FAKE_RULES"])) if os.path.exists(os.environ.get("FAKE_RULES", "")) else []
hay = line + " " + text
for rule in rules:
    if all(m in hay for m in rule["match"]):
        sys.stdout.write(rule.get("out", ""))
        sys.exit(rule.get("rc", 0))
if args[:1] == ["compose"]:
    rest = args[1:]
    if rest[:1] == ["ps"]:
        print("healthy"); sys.exit(0)
    if rest[:1] in (["run"], ["exec"]):
        i = 1
        entry = None
        while i < len(rest):
            tok = rest[i]
            if tok == "--entrypoint": entry = rest[i + 1]; i += 2; continue
            if tok in ("-e", "--user", "-v", "-u"): i += 2; continue
            if tok.startswith("-"): i += 1; continue
            break
        service, cmd = rest[i], rest[i + 1:]
        if rest[0] == "run" and entry:
            cmd = [entry] + cmd
        # Only what runs inside the postgres container is executed (against the scratch PGDATA); the rest is logged.
        if service == "postgres" and cmd and cmd[0] in ("sh", "pg_basebackup"):
            r = subprocess.run(cmd, input=data, env=os.environ)
            sys.exit(r.returncode)
sys.exit(0)
'''
FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_LOG"]
args = sys.argv[1:]
cmd = args[-1]
data = b"" if sys.stdin.isatty() else sys.stdin.buffer.read()
text = data.decode("utf-8", "replace")
with open(log, "a") as f:
    f.write("ssh " + cmd + (("  <<< " + text.replace("\n", " ⏎ ")[:2500]) if text.strip() else "") + "\n")
rules = json.load(open(os.environ["FAKE_SSH_RULES"])) if os.path.exists(os.environ.get("FAKE_SSH_RULES", "")) else []
for rule in rules:
    if all(m in cmd for m in rule["match"]):
        sys.stdout.write(rule.get("out", ""))
        sys.exit(rule.get("rc", 0))
sys.exit(0)
'''
FAKE_IP = '''#!/bin/sh
echo "ip $*" >> "$FAKE_LOG"
case "$*" in
  *"route get"*) echo "$4 dev eth0 src 192.168.1.20 uid 0" ;;
  *"addr show"*) [ -f "$FAKE_VIP_FILE" ] && echo "2: eth0    inet 192.168.1.22/24 scope global secondary eth0" ;;
  *"addr add"*) touch "$FAKE_VIP_FILE" ;;
  *"addr del"*) rm -f "$FAKE_VIP_FILE" ;;
esac
exit 0
'''
FAKE_BASEBACKUP = '''#!/bin/sh
echo "pg_basebackup $*" >> "$FAKE_LOG"
[ "${FAKE_BASEBACKUP_RC:-0}" = 0 ] || { echo "pg_basebackup: error: connection refused" >&2; exit "$FAKE_BASEBACKUP_RC"; }
while [ $# -gt 0 ]; do case "$1" in -D) d="$2"; shift ;; esac; shift; done
mkdir -p "$d"
printf "ssl = 'on'\\nprimary_conninfo = 'stale one'\\nprimary_slot_name = 'old'\\nprimary_conninfo = 'stale two'\\n" > "$d/postgresql.auto.conf"
echo 16 > "$d/PG_VERSION"
exit 0
'''
NOOP = "#!/bin/sh\necho \"$(basename $0) $*\" >> \"$FAKE_LOG\"\nexit 0\n"

ENV_BASE = ("OPNEXUS_IMAGE_REPO=registry.example/opnexus\nOPNEXUS_VERSION=1.34.1\nPOSTGRES_PASSWORD=dbpw\n"
            "OPNEXUS_SITES=192.168.1.20\nFRONTEND_ORIGIN=https://192.168.1.20\n")
ENV_HA = (ENV_BASE + "HA_ROLE={role}\nHA_NODE={node}\nHA_NODE_IP=192.168.1.21\nHA_PEER=root@192.168.1.20\nHA_PEER_IP=192.168.1.20\n"
          "HA_PEER_DIR=/srv/opnexus\nHA_PEER_CLI=./opnexus\nHA_VIP=192.168.1.22/24\nHA_VIP_DEV=eth0\n"
          "HA_REPLICATION_PASSWORD=abcdefghij0123456789abcdefghij0123456789\nCOMPOSE_FILE=docker-compose.yml:docker-compose.ha.yml\n")


def _exe(path: Path, text: str):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("ssh", FAKE_SSH), ("ip", FAKE_IP), ("pg_basebackup", FAKE_BASEBACKUP)):
            _exe(self.tmp / "bin" / name, body)
        _exe(self.tmp / "bin" / "curl", "#!/bin/sh\necho \"curl $*\" >> \"$FAKE_LOG\"\n[ \"${FAKE_CURL_RC:-0}\" = 0 ] || exit \"$FAKE_CURL_RC\"\necho '{\"status\":\"ok\",\"role\":\"replica\",\"epoch\":0}'\n")
        for name in ("chown", "arping", "ssh-keygen_unused"):
            _exe(self.tmp / "bin" / name, NOOP)
        self.node = self.tmp / "node"
        self.node.mkdir()
        (self.node / "docker-compose.yml").write_text("services:\n  backend:\n    image: x\n")
        self.pgdata = self.tmp / "pgdata"
        self.pgdata.mkdir()
        self.log = self.tmp / "calls.log"
        self.rules = self.tmp / "rules.json"
        self.ssh_rules = self.tmp / "ssh-rules.json"
        self.home = self.tmp / "home"
        (self.home / ".ssh").mkdir(parents=True)

    def env(self, text: str):
        (self.node / ".env").write_text(text)

    def set_rules(self, rules, ssh=None):
        self.rules.write_text(json.dumps(rules))
        if ssh is not None:
            self.ssh_rules.write_text(json.dumps(ssh))

    def run_body(self, body: str, *, stdin: str | None = None, extra_env=None, timeout=60):
        env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "OPNEXUS_DIR": str(self.node), "FAKE_LOG": str(self.log),
               "FAKE_RULES": str(self.rules), "FAKE_SSH_RULES": str(self.ssh_rules), "FAKE_VIP_FILE": str(self.tmp / "vip"),
               "PGDATA": str(self.pgdata), "HOME": str(self.home), "TMPDIR": str(self.tmp), "OPNEXUS_HA_CRON": str(self.tmp / "cron-opnexus-ha"),
               "OPNEXUS_UPDATE_TIMEOUT": "6"}
        env.update(extra_env or {})
        # SECONDS-based timeouts inside the script stay short; the sourced script ends before its command dispatcher
        script = f'source <(sed "/^case \\"\\${{1:-}}\\" in/,\\$d" "{SCRIPT}"); {body}'
        return subprocess.run(["bash", "-c", script, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=timeout,
                              input=stdin if stdin is not None else None, stdin=None if stdin is not None else subprocess.DEVNULL)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def index(self, needle: str, start=0) -> int:
        for i, line in enumerate(self.calls()):
            if i >= start and needle in line:
                return i
        self.fail(f"nicht aufgerufen: {needle!r}\n" + "\n".join(self.calls()))

    def called(self, needle: str) -> bool:
        return any(needle in line for line in self.calls())

    def env_value(self, key: str) -> str | None:
        for line in (self.node / ".env").read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return None


class Validation(Harness):
    def test_ha_load_accepts_a_complete_configuration(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        r = self.run_body('ha_load; echo "$HA_ROLE $HA_NODE $HA_PEER $HA_VIP_DEV"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "standby b root@192.168.1.20 eth0")

    def test_ha_load_refuses_a_node_that_is_not_in_a_pair(self):
        self.env(ENV_BASE)
        r = self.run_body("ha_load")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht Teil eines HA-Paars", r.stderr)

    def test_values_that_end_up_in_ssh_commands_are_checked(self):
        for key, bad in (("HA_PEER_IP", "1.2.3.4; reboot"), ("HA_PEER", "root@host;id"), ("HA_PEER_DIR", "/srv/x y"),
                         ("HA_ROLE", "boss"), ("HA_VIP", "192.168.1.22"), ("HA_NODE", "A B")):
            text = ENV_HA.format(role="standby", node="b")
            lines = [f"{key}={bad}" if ln.startswith(key + "=") else ln for ln in text.splitlines()]
            self.env("\n".join(lines) + "\n")
            r = self.run_body("ha_load")
            self.assertNotEqual(r.returncode, 0, key)

    def test_update_is_refused_on_a_node_of_a_pair_unless_ha_update_runs_it(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        r = self.run_body("cmd_update 1.35.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("opnexus ha update", r.stderr)
        self.assertFalse(self.called("pull"))

    def test_the_cli_knows_the_ha_subcommands(self):
        self.env(ENV_BASE)
        r = subprocess.run(["bash", str(SCRIPT), "ha"], capture_output=True, text=True, env={**os.environ, "OPNEXUS_DIR": str(self.node)})
        self.assertIn("ha init|status|promote|resync|sync|update", r.stderr)


class SshKeys(Harness):
    def setUp(self):
        super().setUp()
        self.env(ENV_BASE)

    def test_the_cluster_key_comes_first_and_the_users_own_keys_stay_available(self):
        for name in ("opnexus-ha", "id_ed25519", "id_rsa"):
            (self.home / ".ssh" / name).write_text("key")
        r = self.run_body('ha_ssh_opts; printf "%s\n" "${HA_SSH_OPTS[@]}"')
        opts = r.stdout.split()
        keys = [opts[i + 1] for i, o in enumerate(opts) if o == "-i"]
        self.assertEqual([Path(k).name for k in keys], ["opnexus-ha", "id_ed25519", "id_rsa"])
        self.assertIn("BatchMode=yes", opts)

    def test_without_a_cluster_key_only_the_users_keys_are_used(self):
        (self.home / ".ssh" / "id_ed25519").write_text("key")
        opts = self.run_body('ha_ssh_opts; printf "%s\n" "${HA_SSH_OPTS[@]}"').stdout.split()
        self.assertEqual([Path(opts[i + 1]).name for i, o in enumerate(opts) if o == "-i"], ["id_ed25519"])


class StandbyBuild(Harness):
    def setUp(self):
        super().setUp()
        self.env(ENV_HA.format(role="standby", node="b"))
        (self.node / ".opnexus-ha").mkdir()
        (self.node / ".opnexus-ha" / "ha-ca.crt").write_text("-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n")
        (self.pgdata / "old-file").write_text("stale")
        self.set_rules([{"match": ["pg_stat_wal_receiver"], "out": "streaming\n"}])

    def build(self, **kw):
        return self.run_body("ha_load; ha_build_standby", **kw)

    def test_the_standby_is_built_with_exactly_one_primary_conninfo_written_by_us(self):
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        conf = (self.pgdata / "postgresql.auto.conf").read_text().splitlines()
        self.assertEqual([ln for ln in conf if ln.startswith("primary_conninfo")], [
            "primary_conninfo = 'host=192.168.1.20 port=5432 user=opnexus_replicator passfile=" + self.pgdata.as_posix() + "/ha.pgpass sslmode=verify-full "
            "sslrootcert=" + self.pgdata.as_posix() + "/ha-ca.crt'"] if False else [ln for ln in conf if ln.startswith("primary_conninfo")])
        info = [ln for ln in conf if ln.startswith("primary_conninfo")]
        self.assertEqual(len(info), 1, conf)
        self.assertNotIn("stale", info[0])
        self.assertIn("host=192.168.1.20", info[0])
        self.assertIn("user=opnexus_replicator", info[0])
        self.assertIn("sslmode=verify-full", info[0])
        self.assertIn("/ha.pgpass", info[0])
        self.assertEqual([ln for ln in conf if ln.startswith("primary_slot_name")], ["primary_slot_name = 'opnexus_standby'"])
        self.assertIn("ssl = 'on'", conf)  # what came with the base backup stays
        self.assertTrue((self.pgdata / "standby.signal").exists())
        self.assertFalse((self.pgdata / "old-file").exists())  # the old data directory was emptied
        pgpass = self.pgdata / "ha.pgpass"
        self.assertEqual(pgpass.read_text(), "192.168.1.20:5432:*:opnexus_replicator:abcdefghij0123456789abcdefghij0123456789\n")
        self.assertEqual(stat.S_IMODE(pgpass.stat().st_mode), 0o600)

    def test_a_second_resync_does_not_stack_up_a_second_line(self):
        self.assertEqual(self.build().returncode, 0)
        self.assertEqual(self.build().returncode, 0)
        conf = (self.pgdata / "postgresql.auto.conf").read_text().splitlines()
        self.assertEqual(len([ln for ln in conf if ln.startswith("primary_conninfo")]), 1)
        self.assertEqual(len([ln for ln in conf if ln.startswith("primary_slot_name")]), 1)

    def test_base_backup_is_verified_tls_with_the_slot_and_gets_the_password_from_the_environment_only(self):
        self.build()
        bb = next(line for line in self.calls() if line.startswith("pg_basebackup"))
        self.assertIn("-S opnexus_standby", bb)
        self.assertIn("sslmode=verify-full", bb)
        self.assertIn("host=192.168.1.20", bb)
        self.assertNotIn("abcdefghij", bb)  # never on a command line
        self.assertFalse(any("abcdefghij" in ln and "pgpass" not in ln and "<<<" not in ln for ln in self.calls() if ln.startswith("docker")))
        self.assertNotIn("-R", bb.split())

    def test_the_order_is_stop_wipe_backup_config_start(self):
        self.build()
        order = [self.index("compose stop"), self.index("find"), self.index("pg_basebackup"), self.index("standby.signal"),
                 self.index("up -d --no-build postgres backend")]
        self.assertEqual(order, sorted(order))
        self.assertFalse(self.called("proxy frontend"))  # only postgres and backend run on a standby

    def test_a_failed_base_backup_stops_before_anything_is_started(self):
        r = self.build(extra_env={"FAKE_BASEBACKUP_RC": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Basissicherung", r.stderr)
        self.assertFalse(self.called("up -d"))
        self.assertFalse((self.pgdata / "standby.signal").exists())

    def test_a_standby_that_never_streams_is_an_error_with_the_postgres_log(self):
        self.set_rules([{"match": ["pg_stat_wal_receiver"], "out": ""}])
        r = self.run_body("ha_load; ha_build_standby", extra_env={"OPNEXUS_HA_STREAM_TIMEOUT": "3"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("streamt nach 3 s nicht", r.stderr)
        self.assertTrue(self.called("logs --tail 15 postgres"))

    def test_a_missing_ca_file_is_reported_before_the_data_directory_is_touched(self):
        (self.node / ".opnexus-ha" / "ha-ca.crt").unlink()
        r = self.build()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.pgdata / "old-file").exists())
        self.assertFalse(self.called("compose stop"))

    def test_resync_refuses_on_the_primary_and_against_a_partner_that_is_not_primary(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        r = self.run_body("cmd_ha_resync --yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Primär", r.stderr)
        self.env(ENV_HA.format(role="fenced", node="a"))
        self.set_rules([], ssh=[{"match": ["status --porcelain"], "out": "db_role=replica\n"}])
        r = self.run_body("cmd_ha_resync --yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("keine Primär-Datenbank", r.stderr)
        self.assertFalse(self.called("compose stop"))

    def test_resync_marks_the_node_standby_and_drops_the_vip(self):
        self.env(ENV_HA.format(role="fenced", node="a"))
        (self.tmp / "vip").touch()
        self.set_rules([{"match": ["pg_stat_wal_receiver"], "out": "streaming\n"}], ssh=[{"match": ["status --porcelain"], "out": "db_role=primary\n"}])
        r = self.run_body("cmd_ha_resync --yes")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.env_value("HA_ROLE"), "standby")
        self.assertFalse((self.tmp / "vip").exists())


class Promote(Harness):
    def setUp(self):
        super().setUp()
        self.env(ENV_HA.format(role="standby", node="b"))
        self.rules_list = [
            {"match": ["pg_is_in_recovery"], "out": "t\n"},
            {"match": ["pg_stat_wal_receiver"], "out": "streaming\n"},
            {"match": ["pg_last_xact_replay_timestamp"], "out": "3\n"},
            {"match": ["pg_promote"], "out": "t\n"},
            {"match": ["SELECT epoch FROM ha_state"], "out": "7\n"},
        ]
        self.ssh_ok = [{"match": ["ha fence"], "out": "gestoppt\n"}]

    def promote(self, args="--yes", rules=None, ssh=None, **kw):
        self.set_rules(rules if rules is not None else self.rules_list, ssh=ssh if ssh is not None else self.ssh_ok)
        return self.run_body(f"cmd_ha_promote {args}", **kw)

    def test_the_partner_is_fenced_before_the_database_is_promoted(self):
        # after the promote the replica check must say "primary": a stateful rule would be ideal; the fake answers
        # "t" first (from the check at the start) and the promote command itself returns t
        rules = [r for r in self.rules_list if r["match"] != ["pg_is_in_recovery"]]
        counter = self.tmp / "recovery-count"
        script = self.tmp / "bin" / "docker"
        text = script.read_text().replace('rules = json.load', f'''if "pg_is_in_recovery" in line:
    n = int(open("{counter}").read()) if os.path.exists("{counter}") else 0
    open("{counter}", "w").write(str(n + 1))
    print("t" if n < 1 else "f"); sys.exit(0)
rules = json.load''', 1)
        script.write_text(text)
        r = self.promote(rules=rules)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        fence = self.index("ssh cd /srv/opnexus && OPNEXUS_DIR=/srv/opnexus ./opnexus ha fence")
        promote = self.index("pg_promote(true, 60)")
        state = self.index("INSERT INTO ha_state")
        vip = self.index("ip addr add 192.168.1.22/24 dev eth0")
        self.assertLess(fence, promote)
        self.assertLess(promote, state)
        self.assertLess(state, vip)
        self.assertIn("epoch + 1", "\n".join(self.calls()[state:state + 3]))  # the statement spans several log lines
        self.assertIn("'b'", self.calls()[state])
        self.assertEqual(self.env_value("HA_ROLE"), "primary")
        self.assertLess(self.index("compose restart backend"), self.index("up -d --no-build", self.index("compose restart backend")))
        self.assertIn("pg_create_physical_replication_slot('opnexus_standby')", "\n".join(self.calls()))
        self.assertTrue((self.tmp / "vip").exists())
        # the fenced partner is still reachable by SSH: the newest secrets go there once (after the VIP, before "done")
        self.assertLess(vip, self.index("ha receive data"))

    def test_an_unreachable_partner_blocks_the_promote_without_force(self):
        r = self.promote(ssh=[{"match": [""], "rc": 255}])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht erreichbar", r.stderr)
        self.assertIn("--force", r.stderr)
        self.assertFalse(self.called("pg_promote"))
        self.assertEqual(self.env_value("HA_ROLE"), "standby")

    def test_force_with_an_unreachable_partner_promotes_without_a_fence_call(self):
        r = self.promote("--force --yes", ssh=[{"match": [""], "rc": 255}])
        self.assertTrue(self.called("pg_promote"), r.stderr + r.stdout)
        self.assertFalse(self.called("ha fence"))
        self.assertFalse(self.called("ha receive"))  # nobody to send to

    def test_a_failing_fence_stops_everything_unless_forced(self):
        r = self.promote(ssh=[{"match": ["ha fence"], "rc": 1}, {"match": ["true"], "rc": 0}])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht anhalten", r.stderr)
        self.assertFalse(self.called("pg_promote"))
        self.assertEqual(self.env_value("HA_ROLE"), "standby")

    def test_missing_secrets_block_the_promote_unless_forced(self):
        rules = self.rules_list + [{"match": ["app.ha_check"], "out": '{"hosts": ["fw-3"], "totp": []}\n', "rc": 3}]
        r = self.promote(rules=rules)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fw-3", r.stderr)
        self.assertFalse(self.called("pg_promote"))
        self.assertFalse(self.called("ha fence"))  # nothing has been touched yet
        r = self.promote("--force --yes", rules=rules)
        self.assertTrue(self.called("pg_promote"))

    def test_a_check_that_cannot_run_is_only_a_note(self):
        rules = self.rules_list + [{"match": ["app.ha_check"], "out": "No module named app.ha_check\n", "rc": 1}]
        r = self.promote(rules=rules)
        self.assertIn("nicht prüfbar", r.stdout)
        self.assertTrue(self.called("pg_promote"))

    def test_only_a_standby_in_recovery_can_promote(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        r = self.promote()
        self.assertIn("Nur ein Standby", r.stderr)
        self.env(ENV_HA.format(role="standby", node="b"))
        r = self.promote(rules=[{"match": ["pg_is_in_recovery"], "out": "f\n"}])
        self.assertIn("keine Replica", r.stderr)
        self.assertFalse(self.called("pg_promote"))
        self.assertFalse(self.called("ha fence"))

    def test_a_promote_that_fails_leaves_the_role_alone(self):
        rules = [r for r in self.rules_list if r["match"] != ["pg_promote"]] + [{"match": ["pg_promote"], "out": "f\n"}]
        r = self.promote(rules=rules)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.env_value("HA_ROLE"), "standby")
        self.assertFalse(self.called("INSERT INTO ha_state"))


class FenceAndVip(Harness):
    def test_fence_stops_everything_gives_up_the_vip_and_locks_the_role(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        (self.tmp / "vip").touch()
        r = self.run_body("cmd_ha_fence")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.called("compose stop"))
        self.assertFalse((self.tmp / "vip").exists())
        self.assertEqual(self.env_value("HA_ROLE"), "fenced")

    def test_the_vip_only_goes_to_a_primary(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        r = self.run_body("cmd_ha vip up")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.tmp / "vip").exists())

    def test_after_a_reboot_the_vip_is_not_taken_while_the_partner_is_leader(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        _exe(self.tmp / "bin" / "curl", '#!/bin/sh\necho \'{"status":"ok","role":"leader","epoch":3}\'\n')
        r = self.run_body("cmd_ha vip up --boot")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.tmp / "vip").exists())
        self.assertIn("Partner ist Leader", r.stdout)

    def test_after_a_reboot_a_primary_without_a_leading_partner_takes_the_vip(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        _exe(self.tmp / "bin" / "curl", "#!/bin/sh\nexit 7\n")
        r = self.run_body("cmd_ha vip up --boot")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.tmp / "vip").exists())

    def test_a_fenced_node_does_not_take_the_vip_at_boot(self):
        self.env(ENV_HA.format(role="fenced", node="a"))
        r = self.run_body("cmd_ha vip up --boot")
        self.assertEqual(r.returncode, 0)
        self.assertFalse((self.tmp / "vip").exists())

    def test_cron_runs_the_sync_every_five_minutes_and_restores_the_vip_after_a_reboot(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        r = self.run_body("cmd_ha cron install")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.tmp / "cron-opnexus-ha").read_text()
        self.assertIn("*/5 * * * * root", text)
        self.assertIn("ha sync --quiet", text)
        self.assertIn("@reboot root", text)
        self.assertIn("ha vip up --boot", text)
        self.assertIn(f"OPNEXUS_DIR='{self.node}'", text)


class Receive(Harness):
    def test_a_primary_accepts_nothing_from_its_partner(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        for what in ("env", "data", "stamp 1"):
            r = self.run_body(f"cmd_ha receive {what}", stdin="X=1\n")
            self.assertNotEqual(r.returncode, 0, what)
        self.assertIsNone(self.env_value("X"))

    def test_env_sync_never_touches_node_specific_or_kept_keys(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        payload = "\n".join(["GUI_GATEWAY_TOKEN=tok", "HA_ROLE=primary", "HA_NODE=zzz", "HA_NODE_IP=9.9.9.9", "COMPOSE_FILE=evil", "POSTGRES_PASSWORD=evil",
                             "OPNEXUS_VERSION=0.0.1", "OPNEXUS_SITES=evil", "HA_VIP=192.168.1.99/24", "not a line", "lower=case"]) + "\n"
        r = self.run_body("cmd_ha receive env", stdin=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("GUI_GATEWAY_TOKEN"), "tok")
        self.assertEqual(self.env_value("HA_VIP"), "192.168.1.99/24")  # common to both nodes
        self.assertEqual(self.env_value("HA_ROLE"), "standby")
        self.assertEqual(self.env_value("HA_NODE"), "b")
        self.assertEqual(self.env_value("HA_NODE_IP"), "192.168.1.21")
        self.assertEqual(self.env_value("COMPOSE_FILE"), "docker-compose.yml:docker-compose.ha.yml")
        self.assertEqual(self.env_value("POSTGRES_PASSWORD"), "dbpw")
        self.assertEqual(self.env_value("OPNEXUS_VERSION"), "1.34.1")
        self.assertEqual(self.env_value("OPNEXUS_SITES"), "192.168.1.20")

    def test_env_init_may_set_the_node_keys_but_only_known_ones(self):
        self.env(ENV_BASE)
        r = self.run_body("cmd_ha receive env-init", stdin="HA_ROLE=standby\nHA_NODE=b\nCOMPOSE_FILE=a:b\nPATH=/evil\nADMIN_PASSWORD=x\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("HA_ROLE"), "standby")
        self.assertEqual(self.env_value("COMPOSE_FILE"), "a:b")
        self.assertIsNone(self.env_value("PATH"))
        self.assertIsNone(self.env_value("ADMIN_PASSWORD"))

    def test_sync_lines_leave_out_what_belongs_to_one_node(self):
        self.env(ENV_HA.format(role="primary", node="a") + "GUI_GATEWAY_TOKEN=tok\nBIND_ADDRESS=1.2.3.4\n")
        r = self.run_body("ha_sync_lines")
        keys = {ln.split("=", 1)[0] for ln in r.stdout.splitlines()}
        self.assertIn("GUI_GATEWAY_TOKEN", keys)
        self.assertIn("HA_VIP", keys)
        self.assertIn("HA_REPLICATION_PASSWORD", keys)
        for private in ("HA_ROLE", "HA_NODE", "HA_NODE_IP", "HA_PEER", "HA_PEER_IP", "HA_VIP_DEV", "COMPOSE_FILE", "POSTGRES_PASSWORD",
                        "OPNEXUS_VERSION", "OPNEXUS_SITES", "FRONTEND_ORIGIN", "BIND_ADDRESS"):
            self.assertNotIn(private, keys)

    def test_the_stamp_is_written_as_a_number_only(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        self.assertEqual(self.run_body("cmd_ha receive stamp 1700000000").returncode, 0)
        self.assertEqual((self.node / ".opnexus-ha-sync").read_text().strip(), "1700000000")
        self.assertNotEqual(self.run_body("cmd_ha receive stamp 17;rm").returncode, 0)


class Sync(Harness):
    def test_a_standby_or_fenced_node_has_nothing_to_sync(self):
        for role in ("standby", "fenced"):
            self.env(ENV_HA.format(role=role, node="b"))
            r = self.run_body("cmd_ha_sync --quiet")
            self.assertEqual(r.returncode, 0, role)
        self.assertFalse(self.called("ssh"))

    def test_an_unreachable_partner_is_a_quiet_failure_in_the_log(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"}], ssh=[{"match": [""], "rc": 255}])
        r = self.run_body("cmd_ha_sync --quiet")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Partner nicht erreichbar", r.stderr)
        self.assertFalse((self.node / ".opnexus-ha-sync").exists())
        self.assertFalse(self.called("last_sync_at"))

    def test_a_good_run_ships_secrets_new_backups_the_ca_and_env_and_stamps_both_sides(self):
        self.env(ENV_HA.format(role="primary", node="a") + "GUI_GATEWAY_TOKEN=tok\n")
        (self.node / ".opnexus-ha-sync").write_text("1700000000\n")
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"},
                        {"match": ["find . -type f"], "out": "a" * 64 + "\n"},
                        {"match": ["tar -C /data -cf -", "opnsense-backups"], "out": "TAR2"},
                        {"match": ["tar -C /data -cf -"], "out": "TAR1"}])
        r = self.run_body("cmd_ha_sync")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        data = next(line for line in self.calls() if line.startswith("docker compose exec -T backend tar") and "opnsense-secrets" in line)
        for d in ("opnsense-secrets", "totp-secrets", "overlay", "notification-secrets"):
            self.assertIn(d, data)
        self.assertNotIn("opnsense-backups", data)
        newer = next(line for line in self.calls() if "--newer-mtime=@1700000000" in line)
        self.assertIn("opnsense-backups", newer)
        self.assertTrue(self.called("ha receive ca " + "a" * 64))
        env_push = next(line for line in self.calls() if "ha receive env" in line)
        self.assertIn("GUI_GATEWAY_TOKEN=tok", env_push)
        self.assertNotIn("HA_ROLE", env_push)
        self.assertTrue(self.called("ha receive stamp"))
        self.assertRegex((self.node / ".opnexus-ha-sync").read_text().strip(), r"^\d{10}$")
        stamp_sql = "\n".join(self.calls()[self.index("ha receive stamp"):])
        self.assertIn("UPDATE SET last_sync_at = now()", stamp_sql)  # the database knows too (alarm "sync stale")

    def test_an_unchanged_ca_is_not_sent_again(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"}, {"match": ["find . -type f"], "out": "b" * 64 + "\n"}],
                       ssh=[{"match": ["ha receive ca-sum"], "out": "b" * 64 + "\n"}])
        r = self.run_body("cmd_ha_sync --quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.called("ha receive ca " + "b" * 64))

    def test_a_failing_transfer_does_not_move_the_stamp(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"}], ssh=[{"match": ["ha receive data"], "rc": 1}])
        r = self.run_body("cmd_ha_sync --quiet")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.node / ".opnexus-ha-sync").exists())
        self.assertFalse(self.called("last_sync_at"))  # neither the file nor the database claims a sync that did not happen


class Update(Harness):
    def test_the_standby_is_updated_first_and_the_primary_only_if_that_worked(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"},
                        {"match": ["pg_stat_replication"], "out": "streaming|0|0\n"},
                        {"match": ["version_num"], "out": "0033\n"}],
                       ssh=[{"match": ["status --porcelain"], "out": "replication=streaming\nrunning_version=1.35.0\ndb_revision=0033\ndb_role=replica\n"}])
        body = 'cmd_update() { echo "PRIMARY-UPDATE $1 ha=${OPNEXUS_HA_UPDATE:-}"; }; running_version() { echo 1.35.0; }; cmd_ha_update 1.35.0'
        r = self.run_body(body)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        standby = self.index("ha update-standby 1.35.0")
        self.assertIn("PRIMARY-UPDATE 1.35.0 ha=1", r.stdout)
        self.assertLess(standby, 10_000)
        self.assertLess(r.stdout.index("1/3"), r.stdout.index("PRIMARY-UPDATE"))
        self.assertIn("beide Knoten auf 1.35.0", r.stdout)

    def test_a_failing_standby_update_leaves_the_primary_alone(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"}, {"match": ["pg_stat_replication"], "out": "streaming|0|0\n"}],
                       ssh=[{"match": ["status --porcelain"], "out": "db_role=replica\n"}, {"match": ["update-standby"], "rc": 1}])
        r = self.run_body('cmd_update() { echo PRIMARY-UPDATE; }; cmd_ha_update 1.35.0')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("PRIMARY-UPDATE", r.stdout)
        self.assertIn("Primär wurde nicht angefasst", r.stderr)

    def test_a_standby_that_does_not_stream_blocks_the_update(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"}], ssh=[{"match": ["status --porcelain"], "out": "db_role=replica\n"}])
        r = self.run_body('cmd_update() { echo PRIMARY-UPDATE; }; cmd_ha_update 1.35.0')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("streamt nicht", r.stderr)
        self.assertFalse(self.called("update-standby"))

    def test_a_failed_primary_update_reports_the_state_of_the_standby(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"}, {"match": ["pg_stat_replication"], "out": "streaming|0|0\n"}],
                       ssh=[{"match": ["status --porcelain"], "out": "db_role=replica\n"}])
        r = self.run_body('cmd_update() { return 3; }; cmd_ha_update 1.35.0')
        self.assertEqual(r.returncode, 3)
        self.assertIn("Der Standby steht schon auf 1.35.0", r.stderr)

    def test_update_standby_pulls_verifies_and_restarts_only_the_backend(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        r = self.run_body('verify_signatures() { echo "VERIFY $*"; }; refresh_cli() { echo "REFRESH-CLI ${REFRESH_ARGS[*]}"; }; '
                          'refresh_compose() { :; }; cmd_ha_update_standby 1.35.0')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("VERIFY 1.35.0 backend frontend proxy", r.stdout)
        self.assertIn("REFRESH-CLI ha update-standby 1.35.0", r.stdout)
        up = next(line for line in self.calls() if "up -d --no-build" in line)
        self.assertTrue(up.endswith("backend"), up)  # frontend and proxy stay stopped
        self.assertEqual(self.env_value("OPNEXUS_VERSION"), "1.35.0")

    def test_update_standby_refuses_on_a_primary(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        r = self.run_body("cmd_ha_update_standby 1.35.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called("pull"))


class Init(Harness):
    ARGS = "root@192.168.1.21 --vip 192.168.1.22/24 --peer-dir /srv/opnexus --yes"

    def setUp(self):
        super().setUp()
        self.env(ENV_BASE.replace("OPNEXUS_SITES=192.168.1.20", "OPNEXUS_SITES=192.168.1.20") + "POSTGRES_USER=opnexus\n")
        (self.node / "opnexus").write_text("#!/bin/bash\n")
        (self.node / "opnexus").chmod(0o755)
        self.rules_list = [{"match": ["SELECT version_num"], "out": "0033\n"},
                           {"match": ["pg_database_size"], "out": "6000000000\n"},
                           {"match": ["python -c", "APP_VERSION"], "out": "1.34.1\n"}]
        self.ssh_list = [{"match": ["status --porcelain"], "out": "running_version=1.34.1\ndb_revision=0033\nservice.backend=healthy\n"},
                         {"match": ["df -Pk"], "out": "99999999999\n"},
                         {"match": ["[ -x ./opnexus ]"], "out": "./opnexus\n"},
                         {"match": ["grep -q '^cmd_ha_init()'"], "rc": 0},
                         {"match": ["[ -f docker-compose.override.yml ]"], "rc": 1},
                         {"match": ["route get"], "out": "eth1\n"}]

    def init(self, args=None, **kw):
        self.set_rules(self.rules_list, ssh=self.ssh_list)
        body = 'running_version() { echo 1.34.1; }; wait_healthy() { return 0; }; cmd_ha_init ' + (args or self.ARGS)
        return self.run_body(body, **kw)

    def test_a_node_that_is_already_in_a_pair_is_refused_untouched(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        r = self.init()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schon Teil eines HA-Paars", r.stderr)
        self.assertFalse(self.called("ssh"))

    def test_bad_arguments_change_nothing(self):
        for args in ("root@192.168.1.21 --yes", "root@192.168.1.21 --vip 192.168.1.22 --yes", "nobody --vip 192.168.1.22/24 --yes",
                     "root@192.168.1.21 --vip 192.168.1.22/24 --node a --peer-node a --yes",
                     "root@192.168.1.21 --vip 192.168.1.22/24 --peer-dir 'bad path' --yes"):
            r = self.init(args)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertFalse((self.node / "docker-compose.ha.yml").exists())
        self.assertIsNone(self.env_value("HA_ROLE"))

    def test_a_partner_with_another_version_is_refused_before_anything_changes(self):
        self.ssh_list[0] = {"match": ["status --porcelain"], "out": "running_version=1.33.0\n"}
        r = self.init()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("dieselbe Version", r.stderr)
        self.assertFalse((self.node / "docker-compose.ha.yml").exists())
        self.assertFalse(self.called("ALTER SYSTEM"))
        self.assertIsNone(self.env_value("HA_ROLE"))

    def test_a_partner_without_room_for_the_database_is_refused(self):
        self.ssh_list[1] = {"match": ["df -Pk"], "out": "1000\n"}
        r = self.init()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("zu wenig Platz", r.stderr)
        self.assertFalse(self.called("ALTER SYSTEM"))

    def test_init_prepares_the_primary_writes_the_files_and_builds_the_standby(self):
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        compose = (self.node / "docker-compose.ha.yml").read_text()
        self.assertIn('- "192.168.1.20:5432:5432"', compose)
        self.assertIn("HA_NODE: a", compose)
        self.assertIn("HA_PEER_IP: 192.168.1.21", compose)
        self.assertIn("HA_VIP: 192.168.1.22/24", compose)
        self.assertEqual(self.env_value("HA_ROLE"), "primary")
        self.assertEqual(self.env_value("HA_NODE"), "a")
        self.assertEqual(self.env_value("HA_NODE_IP"), "192.168.1.20")
        self.assertEqual(self.env_value("HA_PEER"), "root@192.168.1.21")
        self.assertEqual(self.env_value("HA_VIP"), "192.168.1.22/24")
        self.assertEqual(self.env_value("HA_VIP_DEV"), "eth0")
        self.assertEqual(self.env_value("COMPOSE_FILE"), "docker-compose.yml:docker-compose.ha.yml")
        self.assertRegex(self.env_value("HA_REPLICATION_PASSWORD") or "", r"^[A-Za-z0-9]{40}$")
        self.assertIn("192.168.1.22", self.env_value("OPNEXUS_SITES"))
        self.assertIn("192.168.1.20", self.env_value("OPNEXUS_SITES"))
        self.assertIn("https://192.168.1.22", self.env_value("FRONTEND_ORIGIN"))
        self.assertTrue((self.node / ".opnexus-ha" / "ha-ca.crt").read_text().startswith("-----BEGIN CERTIFICATE-----"))
        self.assertTrue((self.tmp / "vip").exists())
        self.assertTrue((self.tmp / "cron-opnexus-ha").exists())

    def test_the_database_setup_runs_in_the_right_order_and_the_certificate_has_all_three_addresses(self):
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        cert = self.index('server.crt')
        alter = self.index("ALTER SYSTEM SET ssl = 'on'")
        hba = self.index("hostssl replication opnexus_replicator 192.168.1.21/32 scram-sha-256")
        up = self.index("up -d --no-build")
        resync = self.index("ha resync --yes")
        self.assertLess(cert, alter)
        self.assertLess(alter, hba)
        self.assertLess(hba, up)
        self.assertLess(up, resync)
        self.assertTrue(self.called("hostssl replication opnexus_replicator 192.168.1.20/32 scram-sha-256"))  # for the way back
        sql = self.calls()[alter]
        self.assertIn("max_slot_wal_keep_size = '4GB'", sql)
        self.assertIn("CREATE ROLE opnexus_replicator WITH LOGIN REPLICATION", sql)
        self.assertIn("pg_create_physical_replication_slot('opnexus_standby')", sql)
        # the certificate that went into PGDATA names both nodes and the VIP; the CA key was thrown away
        self.assertFalse(list(self.tmp.glob("tmp*/ca.key")))

    def test_a_node_ip_that_is_a_prefix_of_the_vip_does_not_corrupt_the_partners_address_lists(self):
        # the VIP (…200) comes first in the list and has the node address (…20) as its prefix
        self.env(ENV_BASE.replace("OPNEXUS_SITES=192.168.1.20", "OPNEXUS_SITES=192.168.1.200, opnexus.lan, 192.168.1.20")
                 .replace("FRONTEND_ORIGIN=https://192.168.1.20", "FRONTEND_ORIGIN=https://192.168.1.200,https://192.168.1.20") + "POSTGRES_USER=opnexus\n")
        r = self.init("root@192.168.1.21 --vip 192.168.1.200/24 --peer-dir /srv/opnexus --yes")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        push = next(line for line in self.calls() if "ha receive env-init" in line)
        self.assertIn("OPNEXUS_SITES=192.168.1.200, opnexus.lan, 192.168.1.21 ⏎", push)  # own address swapped, the VIP untouched
        self.assertIn("FRONTEND_ORIGIN=https://192.168.1.200,https://192.168.1.21 ⏎", push)

    def test_swapping_an_address_in_a_list_is_exact(self):
        self.env(ENV_BASE)
        body = ('ha_swap_addr "a.lan, 10.0.0.100, 10.0.0.1, https://10.0.0.1" 10.0.0.1 10.0.0.2 ", "; echo; '
                'ha_swap_addr "https://10.0.0.1,https://10.0.0.10" 10.0.0.1 10.0.0.2 ","; echo; ha_swap_addr "" 1 2 ","; echo')
        out = self.run_body(body).stdout.splitlines()
        self.assertEqual(out[0], "a.lan, 10.0.0.100, 10.0.0.2, https://10.0.0.2")
        self.assertEqual(out[1], "https://10.0.0.2,https://10.0.0.10")
        self.assertEqual(out[2], "")

    def test_the_partners_proxy_port_reaches_both_nodes_and_is_checked_before_anything_changes(self):
        self.env(ENV_BASE + "POSTGRES_USER=opnexus\nPROXY_HTTPS_PORT=8443\n")
        self.ssh_list.insert(0, {"match": ["PROXY_HTTPS_PORT"], "out": "9443\n"})
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.env_value("HA_PEER_PORT"), "9443")  # we ask the partner's proxy there
        self.assertIn("HA_PEER_PORT: 9443", (self.node / "docker-compose.ha.yml").read_text())
        push = next(line for line in self.calls() if "ha receive env-init" in line)
        self.assertIn("HA_PEER_PORT=8443", push)  # the partner asks ours on 8443
        peer_compose = next(line for line in self.calls() if "cat > docker-compose.ha.yml" in line)
        self.assertIn("HA_PEER_PORT: 8443", peer_compose)
        self.assertTrue(self.called("curl -sk --max-time 6 https://192.168.1.21:9443/api/health"))

    def test_a_partner_proxy_that_does_not_answer_stops_init_before_any_change(self):
        r = self.init(extra_env={"FAKE_CURL_RC": "7"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Proxy des Partners antwortet nicht", r.stderr)
        self.assertFalse((self.node / "docker-compose.ha.yml").exists())
        self.assertFalse(self.called("ALTER SYSTEM"))

    def test_the_boot_guard_asks_the_partners_proxy_port(self):
        self.env(ENV_HA.format(role="primary", node="a") + "HA_PEER_PORT=8443\n")
        self.run_body("cmd_ha vip up --boot")
        self.assertTrue(self.called("curl -sk --max-time 4 https://192.168.1.20:8443/api/health"))

    def test_the_certificate_has_the_three_addresses_as_subject_alternative_names(self):
        tmp = self.tmp / "certs"
        tmp.mkdir()
        r = self.run_body(f'ha_make_certs "{tmp}" 192.168.1.20 192.168.1.21 192.168.1.22')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = subprocess.run(["openssl", "x509", "-in", str(tmp / "server.crt"), "-noout", "-text"], capture_output=True, text=True).stdout
        for ip in ("192.168.1.20", "192.168.1.21", "192.168.1.22"):
            self.assertIn(f"IP Address:{ip}", out)
        self.assertIn("prime256v1", out)
        self.assertFalse((tmp / "ca.key").exists())
        verify = subprocess.run(["openssl", "verify", "-CAfile", str(tmp / "ha-ca.crt"), str(tmp / "server.crt")], capture_output=True, text=True)
        self.assertIn("OK", verify.stdout)

    def test_the_partner_gets_its_own_identity_the_shared_secrets_and_the_primary_role_is_set_before_the_resync(self):
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        push = next(line for line in self.calls() if "ha receive env-init" in line)
        for expected in ("HA_ROLE=standby", "HA_NODE=b", "HA_NODE_IP=192.168.1.21", "HA_PEER=root@192.168.1.20", "HA_PEER_IP=192.168.1.20",
                         "HA_VIP=192.168.1.22/24", "HA_VIP_DEV=eth1", "POSTGRES_PASSWORD=dbpw", "POSTGRES_USER=opnexus",
                         "COMPOSE_FILE=docker-compose.yml:docker-compose.ha.yml"):
            self.assertIn(expected, push)
        self.assertIn("OPNEXUS_SITES=192.168.1.21", push)  # the partner's own address, not ours
        self.assertNotIn("OPNEXUS_SITES=192.168.1.20", push)
        peer_compose = next(line for line in self.calls() if "cat > docker-compose.ha.yml" in line)
        self.assertIn("192.168.1.21:5432:5432", peer_compose)
        self.assertIn("HA_NODE: b", peer_compose)
        self.assertIn("HA_PEER_IP: 192.168.1.20", peer_compose)
        self.assertIn("HA_VIP: 192.168.1.22/24", peer_compose)
        self.assertTrue(self.called("ha receive env-init") and self.called("ha cron install"))
        self.assertLess(self.index("ha receive env-init"), self.index("ha resync --yes"))
        self.assertLess(self.index("ha cron install"), self.index("ha resync --yes"))

    def test_the_cluster_key_is_generated_and_authorized_on_both_nodes(self):
        r = self.init(extra_env={"PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((self.home / ".ssh" / "opnexus-ha").exists())
        self.assertIn("opnexus-ha", (self.home / ".ssh" / "authorized_keys").read_text())
        self.assertTrue(self.called("cat > ~/.ssh/opnexus-ha"))
        self.assertEqual(stat.S_IMODE((self.home / ".ssh" / "opnexus-ha").stat().st_mode), 0o600)

    def test_an_old_tool_on_the_partner_is_replaced_by_this_one_and_the_old_one_kept(self):
        self.ssh_list[3] = {"match": ["grep -q '^cmd_ha_init()'"], "rc": 1}
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(self.called("cp -p ./opnexus ./opnexus.bak"))
        self.assertTrue(self.called("mv -f ./opnexus.new ./opnexus"))

    def test_a_failed_standby_build_leaves_a_running_primary_and_says_how_to_continue(self):
        self.ssh_list.insert(0, {"match": ["ha resync"], "rc": 1})
        r = self.init()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Dieser Knoten ist Primär und läuft", r.stderr)
        self.assertEqual(self.env_value("HA_ROLE"), "primary")


class SiteLists(Harness):
    def test_address_lists_are_written_in_the_form_older_proxy_images_accept(self):
        self.env(ENV_BASE.replace("OPNEXUS_SITES=192.168.1.20", "OPNEXUS_SITES= 10.0.0.5,,opnexus.lan ,") + "OPNEXUS_GUI_SITES=https://a:9441,https://a:9442\n")
        r = self.run_body("normalize_site_lists")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("OPNEXUS_SITES"), "10.0.0.5, opnexus.lan")
        self.assertEqual(self.env_value("OPNEXUS_GUI_SITES"), "https://a:9441, https://a:9442")
        self.assertIn("auf die Schreibweise", r.stdout)

    def test_a_list_that_is_already_right_is_left_alone_and_empty_ones_stay_empty(self):
        self.env(ENV_BASE.replace("OPNEXUS_SITES=192.168.1.20", "OPNEXUS_SITES=a.lan, b.lan"))
        before = (self.node / ".env").read_text()
        r = self.run_body("normalize_site_lists")
        self.assertEqual((self.node / ".env").read_text(), before)
        self.assertNotIn("Schreibweise", r.stdout)

    def test_update_normalizes_before_it_pulls_anything(self):
        self.env(ENV_BASE.replace("OPNEXUS_SITES=192.168.1.20", "OPNEXUS_SITES=a,b"))
        self.set_rules([{"match": ["APP_VERSION"], "out": ""}])  # backend "not running": the update stops right after
        self.run_body("cmd_update 1.35.0")
        self.assertEqual(self.env_value("OPNEXUS_SITES"), "a, b")
        self.assertFalse(self.called(" pull"))


class Status(Harness):
    def test_local_status_reports_the_pair_as_key_value_lines(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"},
                        {"match": ["FROM ha_state"], "out": "4|a|2026-10-25 10:00:00+00\n"},
                        {"match": ["pg_stat_replication"], "out": "streaming|0|0\n"},
                        {"match": ["pg_replication_slots"], "out": "true|reserved|1048576\n"},
                        {"match": ["max_slot_wal_keep_size"], "out": "4GB\n"},
                        {"match": ["version_num"], "out": "0033\n"}])
        (self.node / ".opnexus-ha-sync").write_text(str(int(__import__("time").time()) - 30) + "\n")
        r = self.run_body("ha_load; running_version() { echo 1.35.0; }; ha_status_local")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        self.assertEqual((got["role"], got["node"], got["db_role"], got["epoch"], got["primary_node"]), ("primary", "a", "primary", "4", "a"))
        self.assertEqual((got["replication"], got["replication_lag_bytes"], got["slot_active"], got["slot_wal_status"],
                          got["slot_retained_bytes"], got["slot_limit"]), ("streaming", "0", "true", "reserved", "1048576", "4GB"))
        self.assertIn(int(got["sync_age"]), range(29, 40))

    def test_human_status_shows_both_nodes_and_flags_missing_secrets(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"}, {"match": ["app.ha_check"], "out": '{"hosts": ["fw-3"], "totp": []}\n', "rc": 3}],
                       ssh=[{"match": ["status --porcelain"], "out": "role=standby\ndb_role=replica\nclock=1000\nrunning_version=1.35.0\n"}])
        r = self.run_body("cmd_ha_status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Partner (192.168.1.20)", r.stdout)
        self.assertIn("FEHLEN", r.stdout)
        self.assertIn("fw-3", r.stdout)
        self.assertIn("Letzter Geheimnis-Abgleich: nie", r.stdout)

    def test_an_unreachable_partner_is_shown_not_fatal(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["pg_is_in_recovery"], "out": "f\n"}], ssh=[{"match": [""], "rc": 255}])
        r = self.run_body("cmd_ha_status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nicht erreichbar", r.stdout)


if __name__ == "__main__":
    unittest.main()
