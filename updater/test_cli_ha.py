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
    if "call resync" in line:   # what the partner would see of us when it asks for our state
        state = os.path.join(os.environ.get("OPNEXUS_DIR", "."), ".opnexus-ha", "state")
        f.write("STATE@RESYNC " + (open(state).read().replace("\n", " ⏎ ") if os.path.exists(state) else "(none)") + "\n")
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
  *"addr show"*) for a in ${FAKE_LOCAL_ADDRS-192.168.1.20 192.168.2.21}; do echo "2: eth0    inet $a/24 scope global eth0"; done
                 [ -f "$FAKE_VIP_FILE" ] && echo "2: eth0    inet 192.168.1.22/24 scope global secondary eth0"
                 # a long tail after the hit: a pipe into `grep -q` would give `ip` a SIGPIPE, which pipefail turns into "not there"
                 i=0; while [ $i -lt ${FAKE_ADDR_TAIL-2000} ]; do echo "9: br-$i    inet 10.$((i / 250)).$((i % 250)).1/24 scope global br-$i"; i=$((i + 1)); done ;;
  *"neigh show"*) [ -z "$FAKE_NEIGH" ] || echo "192.168.1.22 dev eth0 lladdr aa:bb:cc:dd:ee:ff $([ "$FAKE_NEIGH" = 1 ] && echo REACHABLE || echo "$FAKE_NEIGH")" ;;
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
    import pwd as _pwd
    whoami = _pwd.getpwuid(os.getuid()).pw_name   # `ha init` names the partner's SSH target after the user it runs as (`id -un`); not always root

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("ssh", FAKE_SSH), ("ip", FAKE_IP), ("pg_basebackup", FAKE_BASEBACKUP)):
            _exe(self.tmp / "bin" / name, body)
        _exe(self.tmp / "bin" / "curl", "#!/bin/sh\necho \"curl $*\" >> \"$FAKE_LOG\"\n[ \"${FAKE_CURL_RC:-0}\" = 0 ] || exit \"$FAKE_CURL_RC\"\nif [ -n \"$FAKE_CURL_OUT\" ]; then echo \"$FAKE_CURL_OUT\"; else echo '{\"status\":\"ok\",\"role\":\"replica\",\"epoch\":0}'; fi\n")
        _exe(self.tmp / "bin" / "ping", "#!/bin/sh\necho \"ping $*\" >> \"$FAKE_LOG\"\nexit \"${FAKE_PING_RC:-1}\"\n")
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

    STATE_KEYS = ("HA_ROLE", "HA_NODE", "HA_NODE_IP", "HA_PEER", "HA_PEER_IP", "HA_PEER_PORT", "HA_PEER_DIR", "HA_PEER_CLI", "HA_VIP", "HA_VIP_DEV",
                  "HA_REPLICATION_PASSWORD")

    def env(self, text: str):
        """Write the node's configuration the way the current tool does: HA_* into .opnexus-ha/state, the rest into the .env (plus the
        HA_ROLE mirror line that older tools read)."""
        shutil.rmtree(self.node / ".opnexus-ha" / "state", ignore_errors=True)
        (self.node / ".opnexus-ha" / "state").unlink(missing_ok=True)
        state, env = [], []
        for line in text.splitlines():
            key = line.split("=", 1)[0]
            if key in self.STATE_KEYS:
                state.append(line)
            if key not in self.STATE_KEYS or key == "HA_ROLE":
                env.append(line)
        (self.node / ".env").write_text("\n".join(env) + "\n")
        if state:
            (self.node / ".opnexus-ha").mkdir(exist_ok=True)
            (self.node / ".opnexus-ha" / "state").write_text("\n".join(state) + "\n")

    def env_legacy(self, text: str):
        """The .env of a version before the state file: every HA_* line in the .env."""
        shutil.rmtree(self.node / ".opnexus-ha", ignore_errors=True)
        (self.node / ".env").write_text(text)

    def state_value(self, key: str) -> str | None:
        try:
            for line in (self.node / ".opnexus-ha" / "state").read_text().splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
        except OSError:
            pass
        return None

    def set_rules(self, rules, ssh=None):
        self.rules.write_text(json.dumps(rules))
        if ssh is not None:
            self.ssh_rules.write_text(json.dumps(ssh))

    def run_body(self, body: str, *, stdin: str | None = None, extra_env=None, timeout=60):
        env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "OPNEXUS_DIR": str(self.node), "FAKE_LOG": str(self.log),
               "FAKE_RULES": str(self.rules), "FAKE_SSH_RULES": str(self.ssh_rules), "FAKE_VIP_FILE": str(self.tmp / "vip"),
               "PGDATA": str(self.pgdata), "HOME": str(self.home), "TMPDIR": str(self.tmp), "OPNEXUS_HA_CRON": str(self.tmp / "cron-opnexus-ha"),
               "OPNEXUS_UPDATE_TIMEOUT": "6", "OPNEXUS_HA_PROBE_RETRY_WAIT": "0"}
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
        self.assertIn("ha prepare|init|status|promote|resync|sync|update", r.stderr)


class StateFile(Harness):
    """U1 (docs/DESIGN-HA-UI.md 3.6): the HA state lives in .opnexus-ha/state, not in the .env."""

    def test_a_write_creates_the_file_private_and_mirrors_only_the_role(self):
        self.env(ENV_BASE)
        r = self.run_body('ha_state_set HA_NODE a; ha_state_set HA_VIP 192.168.1.22/24; ha_state_set HA_ROLE primary')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state_value("HA_NODE"), "a")
        self.assertEqual(self.state_value("HA_VIP"), "192.168.1.22/24")
        self.assertEqual(self.env_value("HA_ROLE"), "primary")
        for key in ("HA_NODE", "HA_VIP"):
            self.assertIsNone(self.env_value(key), key)
        self.assertEqual(stat.S_IMODE((self.node / ".opnexus-ha" / "state").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.node / ".opnexus-ha").stat().st_mode), 0o700)
        self.assertEqual([p.name for p in (self.node / ".opnexus-ha").iterdir()], ["state"])  # no temp file left behind

    def test_a_second_write_replaces_the_value_and_keeps_the_others(self):
        self.env(ENV_BASE)
        r = self.run_body('ha_state_set HA_NODE a; ha_state_set HA_ROLE standby; ha_state_set HA_ROLE primary')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = (self.node / ".opnexus-ha" / "state").read_text().splitlines()
        self.assertEqual(sorted(lines), ["HA_NODE=a", "HA_ROLE=primary"])
        self.assertEqual(self.env_value("HA_ROLE"), "primary")

    def test_only_known_keys_and_single_line_values_are_accepted(self):
        self.env(ENV_BASE)
        for body in ("ha_state_set COMPOSE_PROFILES updater", "ha_state_set HA_EVIL x", 'ha_state_set HA_NODE $\'a\\nHA_ROLE=primary\'', "ha_state_set 'HA_NODE;x' a"):
            r = self.run_body(body)
            self.assertNotEqual(r.returncode, 0, body)
        self.assertFalse((self.node / ".opnexus-ha" / "state").exists())
        self.assertIsNone(self.env_value("COMPOSE_PROFILES"))

    def test_the_state_file_is_read_as_text_and_never_executed(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        marker = self.tmp / "pwned"
        with (self.node / ".opnexus-ha" / "state").open("a") as f:
            f.write(f"HA_NODE=$(touch {marker})\n")
        r = self.run_body("ha_load")
        self.assertNotEqual(r.returncode, 0)  # not a valid node name
        self.assertFalse(marker.exists())

    def test_an_older_env_is_moved_once_and_keeps_only_the_role_line(self):
        self.env_legacy(ENV_HA.format(role="standby", node="b"))
        r = self.run_body('ha_load; echo "$HA_ROLE $HA_NODE $HA_PEER $HA_VIP_DEV $HA_PEER_PORT"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("standby b root@192.168.1.20 eth0 443", r.stdout)
        self.assertIn("übernommen", r.stdout)
        env = (self.node / ".env").read_text()
        for key in self.STATE_KEYS:
            if key != "HA_ROLE":
                self.assertNotIn(key + "=", env, key)
        self.assertEqual(self.env_value("HA_ROLE"), "standby")
        self.assertIn("COMPOSE_FILE=docker-compose.yml:docker-compose.ha.yml", env)  # stays in the .env
        self.assertIn("POSTGRES_PASSWORD=dbpw", env)
        self.assertEqual(self.state_value("HA_REPLICATION_PASSWORD"), "abcdefghij0123456789abcdefghij0123456789")
        # the second call does not migrate again
        r = self.run_body('ha_load; echo "$HA_ROLE"')
        self.assertNotIn("übernommen", r.stdout)

    def test_addresses_must_be_real_ipv4_addresses(self):
        self.env(ENV_BASE)
        for good in ("0.0.0.0", "10.20.30.34", "192.168.2.255", "255.255.255.255"):
            self.assertEqual(self.run_body(f"ha_valid {good} \"$RE_IP\" X").returncode, 0, good)
        for bad in ("256.1.1.1", "1.1.1.300", "999.1.1.1", "1.2.3", "1.2.3.4.5", "01.2.3.4x", "1.2.3.4;id", "a.b.c.d", "", "1.2.3.-4"):
            self.assertNotEqual(self.run_body(f"ha_valid '{bad}' \"$RE_IP\" X").returncode, 0, bad)
        for good in ("192.168.2.22/24", "10.0.0.1/32", "10.0.0.1/0"):
            self.assertEqual(self.run_body(f"ha_valid {good} \"$RE_CIDR\" X").returncode, 0, good)
        for bad in ("192.168.2.22/33", "192.168.2.22/", "192.168.2.22", "300.1.1.1/24", "192.168.2.22/024"):
            self.assertNotEqual(self.run_body(f"ha_valid '{bad}' \"$RE_CIDR\" X").returncode, 0, bad)

    def test_the_state_file_wins_over_a_stale_env_line(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        (self.node / ".env").write_text((self.node / ".env").read_text().replace("HA_ROLE=standby", "HA_ROLE=primary"))
        r = self.run_body('ha_load; echo "$HA_ROLE"; ha_role_get')
        self.assertEqual(r.stdout.split()[-2:], ["standby", "standby"])

    def test_a_click_free_update_is_blocked_by_the_state_file_alone(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        (self.node / ".env").write_text("\n".join(line for line in (self.node / ".env").read_text().splitlines() if not line.startswith("HA_ROLE=")) + "\n")
        r = self.run_body('[ -n "$(ha_role_get)" ] && echo blocked')
        self.assertEqual(r.stdout.strip(), "blocked")

    def test_the_role_changes_of_promote_resync_and_fence_go_to_the_state_file_and_the_mirror(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.assertEqual(self.run_body("ha_load; cmd_ha_fence").returncode, 0)
        self.assertEqual((self.state_value("HA_ROLE"), self.env_value("HA_ROLE")), ("fenced", "fenced"))

    def test_env_init_puts_ha_keys_into_the_state_file_and_the_rest_into_the_env(self):
        self.env(ENV_BASE)
        payload = "HA_ROLE=standby\nHA_NODE=b\nHA_VIP=192.168.1.22/24\nHA_EVIL=1\nCOMPOSE_FILE=a:b\nPOSTGRES_USER=u\nPATH=/evil\n"
        r = self.run_body("cmd_ha receive env-init", stdin=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.state_value("HA_NODE"), self.state_value("HA_VIP"), self.state_value("HA_ROLE")), ("b", "192.168.1.22/24", "standby"))
        self.assertEqual(self.env_value("COMPOSE_FILE"), "a:b")
        self.assertEqual(self.env_value("POSTGRES_USER"), "u")
        self.assertIsNone(self.env_value("HA_NODE"))
        self.assertIsNone(self.env_value("HA_EVIL"))
        self.assertIsNone(self.state_value("HA_EVIL"))
        self.assertIsNone(self.env_value("PATH"))


class AgentPath(Harness):
    """Transport über den Agenten, Dienstlisten, Host-Befehle, `ha prepare` und `ha init --code` (docs/DESIGN-HA-UI.md)."""

    def setUp(self):
        super().setUp()
        _exe(self.tmp / "bin" / "python", "#!/bin/sh\necho \"python $*\" >> \"$FAKE_LOG\"\ncase \"$*\" in *' status'*) exit 0 ;; *' call ping'*) exit \"${FAKE_PING_AGENT_RC:-0}\" ;; *' gate '*) echo pin=abababababababababababababababababababababababababababababababab ;; esac\n")
        _exe(self.tmp / "bin" / "hostname", "#!/bin/sh\necho abc123\n")

    def pair(self):
        (self.node / ".opnexus-ha" / "agent").mkdir(parents=True, exist_ok=True)
        (self.node / ".opnexus-ha" / "agent" / "cluster.json").write_text("{}")

    # -- Transport -------------------------------------------------------------
    def test_without_a_pairing_the_old_ssh_path_is_used(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([], ssh=[{"match": ["status --porcelain"], "out": "db_role=replica\n"}])
        r = self.run_body("ha_load; ha_peer ha status --porcelain")
        self.assertIn("db_role=replica", r.stdout)
        self.assertTrue(self.called("ssh "))
        self.assertFalse(self.called("ha_cli.py"))

    def test_with_a_pairing_every_allowed_call_goes_through_the_agent_as_a_fixed_verb(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.pair()
        cases = [("ha status --porcelain", "call status"), ("ha fence", "call fence"), ("ha resync --yes", "call resync"),
                 ("ha update-standby 1.37.0", "call update-standby --param 1.37.0"), ("ha receive data", "call receive/data"),
                 ("ha receive env", "call receive/env"), ("ha receive env-init", "call receive/env-init"), ("ha receive ha-ca", "call receive/ha-ca"),
                 ("ha receive ca " + "a" * 64, "call receive/ca --param " + "a" * 64), ("ha receive ca-sum", "call receive/ca-sum"),
                 ("ha receive stamp 1700000000", "call receive/stamp --param 1700000000")]
        for call, verb in cases:
            self.run_body(f"ha_load; ha_peer {call}", stdin="x\n")
            self.assertTrue(self.called("python /app/ha_cli.py " + verb) or self.called("exec -T updater python /app/ha_cli.py " + verb), verb)
        self.assertFalse(self.called("ssh "))

    def test_anything_else_is_refused_at_the_cli_too(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.pair()
        for call in ("ha cron install", "ha promote", "ha promote --force", "ha promote --yes --force", "ha resync", "ha dissolve", "status --porcelain", "ha receive evil", "rm -rf /"):
            r = self.run_body(f"ha_load; ha_peer {call}")
            self.assertNotEqual(r.returncode, 0, call)
        self.assertFalse(self.called("ha_cli.py call"))

    def test_the_agent_runs_in_the_updater_container_on_the_host_and_directly_inside_it(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.pair()
        self.run_body("ha_load; ha_peer ha fence")
        self.assertTrue(self.called("compose exec -T updater python /app/ha_cli.py call fence"))
        self.log.unlink()
        self.run_body("ha_load; ha_peer ha fence", extra_env={"OPNEXUS_UPDATER": "1"})
        self.assertTrue(self.called("python /app/ha_cli.py call fence"))
        self.assertFalse(self.called("compose exec"))

    def test_reachability_asks_the_agent_when_paired_and_ssh_otherwise(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.assertEqual(self.run_body("ha_load; ha_reach").returncode, 0)
        self.assertTrue(self.called("ssh "))
        self.pair()
        self.log.unlink()
        self.set_rules([{"match": ["call ping"], "rc": 4}])
        self.assertNotEqual(self.run_body("ha_load; ha_reach").returncode, 0)
        self.assertTrue(self.called("call ping"))
        self.assertFalse(self.called("ssh "))

    def test_an_agent_pair_needs_no_ssh_settings(self):
        self.env("\n".join(l for l in ENV_HA.format(role="standby", node="b").splitlines() if not l.startswith(("HA_PEER=", "HA_PEER_DIR=", "HA_PEER_CLI="))) + "\n")
        r = self.run_body("ha_load")
        self.assertNotEqual(r.returncode, 0)  # without the agent the SSH settings are still required
        self.pair()
        r = self.run_body('ha_load; echo "$HA_ROLE"')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "standby"))

    # -- Dienste und Host-Befehle ------------------------------------------------
    def test_stopping_and_starting_leave_out_the_updater_and_stopping_also_the_gate(self):
        self.env(ENV_BASE)
        self.set_rules([{"match": ["config --services"], "out": "postgres\nbackend\nfrontend\nproxy\nupdater\nha-gate\n"}])
        self.run_body("ha_stop_apps; ha_up_apps")
        stop = next(l for l in self.calls() if "compose stop" in l)
        up = next(l for l in self.calls() if "compose up -d" in l)
        self.assertEqual(stop.split("compose stop ")[1].split(), ["postgres", "backend", "frontend", "proxy"])
        # on the host the updater is renewed along with the rest (a promote must not leave an old agent behind) ...
        self.assertEqual(up.split("--no-build ")[1].split(), ["postgres", "backend", "frontend", "proxy", "updater", "ha-gate"])
        # ... inside the updater container it cannot recreate itself
        self.log.unlink()
        self.run_body("ha_up_apps", extra_env={"OPNEXUS_UPDATER": "1"})
        up = next(l for l in self.calls() if "compose up -d" in l)
        self.assertEqual(up.split("--no-build ")[1].split(), ["postgres", "backend", "frontend", "proxy", "ha-gate"])

    def test_without_a_service_list_everything_is_stopped_as_before(self):
        self.env(ENV_BASE)
        self.run_body("ha_stop_apps; ha_up_apps")
        self.assertTrue(any(l.strip() == "docker compose stop" for l in self.calls()))
        self.assertTrue(any("compose up -d --no-build" in l and l.rstrip().endswith("--no-build") for l in self.calls()))

    def test_host_commands_run_directly_on_the_host_and_in_a_helper_container_inside_the_updater(self):
        self.env(ENV_BASE)
        self.run_body("ha_host_exec ip addr show")
        self.assertTrue(self.called("ip addr show"))
        self.assertFalse(self.called("docker run"))
        self.log.unlink()
        self.set_rules([{"match": ["inspect"], "out": "reg.test/opnexus/opnexus-updater:1.37.0\n"}])
        self.run_body("ha_host_exec ip addr add 192.168.2.22/24 dev eth0", extra_env={"OPNEXUS_UPDATER": "1", "HOSTNAME": "deadbeef"})
        run = next(l for l in self.calls() if l.startswith("docker run"))
        self.assertIn("--network host", run)
        self.assertIn("--cap-drop ALL --cap-add NET_ADMIN --cap-add NET_RAW", run)
        self.assertIn("--entrypoint ip reg.test/opnexus/opnexus-updater:1.37.0 addr add 192.168.2.22/24 dev eth0", run)
        self.assertIn("inspect deadbeef", " ".join(self.calls()))
        self.assertNotIn("--privileged", run)

    def test_the_vip_is_set_and_released_through_the_host_helper_inside_the_updater(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["inspect"], "out": "img:1\n"}])
        r = self.run_body("ha_load; ha_vip_up", extra_env={"OPNEXUS_UPDATER": "1"})
        self.assertTrue(any("--entrypoint ip img:1 addr add 192.168.1.22/24 dev eth0" in l for l in self.calls()), r.stderr)

    # -- Empfang auf dem Standby ---------------------------------------------------
    PEM = "-----BEGIN CERTIFICATE-----\nMIIBkTCCATigAwIBAgIUabc+/=\n-----END CERTIFICATE-----\n"

    def test_the_replication_certificate_is_taken_only_as_a_certificate(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        self.assertEqual(self.run_body("cmd_ha receive ha-ca", stdin=self.PEM).returncode, 0)
        self.assertEqual((self.node / ".opnexus-ha" / "ha-ca.crt").read_text(), self.PEM)
        for bad in ("", "hello\n", self.PEM + "$(touch /tmp/x)\n", "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n", "a b c\n"):
            (self.node / ".opnexus-ha" / "ha-ca.crt").unlink(missing_ok=True)
            self.assertNotEqual(self.run_body("cmd_ha receive ha-ca", stdin=bad).returncode, 0, repr(bad))
            self.assertFalse((self.node / ".opnexus-ha" / "ha-ca.crt").exists(), repr(bad))

    PAYLOAD = ("HA_ROLE=standby\nHA_NODE=b\nHA_NODE_IP=192.168.2.21\nHA_PEER_IP=192.168.2.20\nHA_PEER_PORT=443\nHA_VIP=192.168.2.22/24\n"
               "HA_REPLICATION_PASSWORD=abcdefghij0123456789abcdefghij0123456789\nPOSTGRES_USER=opnexus\nPOSTGRES_PASSWORD=dbpw\nPOSTGRES_DB=opnexus\n"
               "OPNEXUS_SITES=192.168.2.21, 192.168.2.22\nFRONTEND_ORIGIN=https://192.168.2.21,https://192.168.2.22\n")

    def agent_node(self):
        self.env(ENV_BASE)
        self.pair()

    def test_the_setup_package_of_an_agent_pair_writes_state_env_and_a_compose_file_of_our_own(self):
        self.agent_node()
        r = self.run_body("cmd_ha receive env-init", stdin=self.PAYLOAD + "COMPOSE_FILE=/etc/evil.yml:/tmp/x.yml\nHA_PEER=root@evil\nHA_PEER_DIR=/x\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.state_value("HA_ROLE"), self.state_value("HA_NODE"), self.state_value("HA_VIP")), ("standby", "b", "192.168.2.22/24"))
        self.assertIsNone(self.state_value("HA_PEER"))  # no SSH details in the agent path
        self.assertEqual(self.env_value("COMPOSE_FILE"), "docker-compose.yml:docker-compose.ha.yml")  # computed here, never taken from the sender
        compose = (self.node / "docker-compose.ha.yml").read_text()
        self.assertIn('"192.168.2.21:5432:5432"', compose)
        self.assertIn("HA_NODE: b", compose)
        self.assertEqual(self.env_value("POSTGRES_PASSWORD"), "dbpw")
        self.assertEqual(self.state_value("HA_VIP_DEV"), "eth0")  # found by this node itself (route to the partner)

    def test_a_bad_value_in_the_setup_package_writes_nothing_at_all(self):
        self.agent_node()
        before = (self.node / ".env").read_text()
        for bad in ("HA_NODE=b\nHA_VIP=192.168.2.22/24\nHA_NODE_IP=1.2.3.4\nHA_PEER_IP=1.2.3.5\nHA_PEER_PORT=443\nHA_REPLICATION_PASSWORD=abcdefghij0123456789\nHA_ROLE=standby\nPOSTGRES_PASSWORD=a\tb",
                    "HA_NODE=B\n", "HA_ROLE=primary\n", "HA_NODE_IP=1.2.3.4;x\n", "HA_VIP=1.2.3.4\n", "OPNEXUS_SITES=a;b\n", "POSTGRES_USER=x'; drop\n",
                    "HA_NODE=b\nPOSTGRES_PASSWORD=bad\x01pw\n", "HA_NODE=b\nHA_ROLE=standby\nCOMPOSE_FILE=a\nHA_EVIL=1\n"):
            r = self.run_body("cmd_ha receive env-init", stdin=bad + ("\n" if not bad.endswith("\n") else ""))
            if "HA_EVIL" in bad:
                continue   # lines with unknown keys are skipped by the pattern, not an error
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertEqual((self.node / ".env").read_text(), before)
        self.assertFalse((self.node / ".opnexus-ha" / "state").exists())
        self.assertFalse((self.node / "docker-compose.ha.yml").exists())

    def test_keys_outside_the_list_never_get_through(self):
        self.agent_node()
        self.run_body("cmd_ha receive env-init", stdin=self.PAYLOAD + "COMPOSE_PROFILES=updater\nOPNEXUS_IMAGE_REPO=evil.example\nPATH=/evil\nOPNEXUS_RUN_AS_ROOT=1\n")
        for key in ("COMPOSE_PROFILES", "PATH", "OPNEXUS_RUN_AS_ROOT"):
            self.assertIsNone(self.env_value(key), key)
        self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), "registry.example/opnexus")  # unchanged

    def test_a_primary_takes_no_setup_package(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.pair()
        self.assertNotEqual(self.run_body("cmd_ha receive env-init", stdin=self.PAYLOAD).returncode, 0)

    def test_a_long_address_list_is_accepted_and_nothing_uses_a_repetition_musl_cannot_compile(self):
        # the updater image is Alpine (musl): a regex repetition above {255} does not compile there, glibc (these tests) accepts it
        self.agent_node()
        sites = ", ".join(f"10.0.{i}.{j}" for i in range(4) for j in range(1, 20))
        self.assertGreater(len(sites), 500)
        r = self.run_body("cmd_ha receive env-init", stdin=self.PAYLOAD.replace("OPNEXUS_SITES=192.168.2.21, 192.168.2.22", "OPNEXUS_SITES=" + sites))
        self.assertEqual(r.returncode, 0, r.stderr)
        import re as _re
        for n, line in enumerate(SCRIPT.read_text().splitlines(), 1):
            for m in _re.finditer(r"\{(\d+)(?:,(\d*))?\}", line):
                if "${" in line[max(0, m.start() - 12):m.start() + 1]:
                    continue   # shell parameter expansions like ${#x}, not regex repetitions
                self.assertLessEqual(int(m.group(2) or m.group(1) or 0), 255, f"opnexus:{n}: {line.strip()[:100]}")
        r = self.run_body("cmd_ha receive env-init", stdin=self.PAYLOAD.replace("OPNEXUS_SITES=192.168.2.21, 192.168.2.22", "OPNEXUS_SITES=" + "a" * 2001))
        self.assertNotEqual(r.returncode, 0)

    def test_a_half_finished_pairing_is_dropped_by_prepare_and_by_init_but_a_real_pair_is_not(self):
        for fn, args in (("cmd_ha_prepare", "--ip 192.168.2.21"), ("cmd_ha_init_agent", "--code OPNX1-AAAAA --vip 192.168.2.22/24 --yes")):
            self.env(ENV_BASE)
            self.pair()
            self.log.unlink(missing_ok=True)
            self.set_rules(self.PREPARE_RULES + [{"match": ["ha_cli.py", " parse "], "out": "ip=192.168.1.21\nport=8445\n"}])
            self.run_body(f'running_version() {{ echo 1.34.1; }}; {fn} {args}')
            self.assertTrue(self.called("ha_cli.py unpair"), fn)
        self.env(ENV_HA.format(role="primary", node="a"))
        self.pair()
        self.log.unlink(missing_ok=True)
        self.run_body("ha_agent_up() { :; }; ha_drop_half_pairing")
        self.assertFalse(self.called("ha_cli.py unpair"))

    # -- ha prepare ------------------------------------------------------------------
    PREPARE_RULES = [{"match": ["ha_cli.py", " gate "], "out": "pin=" + "ab" * 32 + "\nport=8445\n"},
                     {"match": ["ha_cli.py", " prepare "], "out": "code=OPNX1-AAAAA-BBBBB\npin=" + "ab" * 32 + "\nport=8445\nexpires_in=1800\n"}]

    def test_prepare_starts_agent_and_gate_adds_the_profiles_and_prints_the_code(self):
        self.env(ENV_BASE + "COMPOSE_PROFILES=gui\n")
        self.set_rules(self.PREPARE_RULES)
        r = self.run_body("ha_load() { :; }; cmd_ha_prepare --ip 192.168.2.21")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("OPNX1-AAAAA-BBBBB", r.stdout)
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "gui,updater,ha")
        order = [self.index("up -d --no-build updater"), self.index("ha_cli.py gate --ip 192.168.2.21"), self.index("up -d --no-build ha-gate"),
                 self.index("ha_cli.py prepare --ip 192.168.2.21 --port 8445")]
        self.assertEqual(order, sorted(order))

    def test_prepare_twice_does_not_duplicate_the_profiles(self):
        self.env(ENV_BASE)
        self.set_rules(self.PREPARE_RULES)
        for _ in range(2):
            self.assertEqual(self.run_body("cmd_ha_prepare --ip 192.168.2.21").returncode, 0)
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "updater,ha")

    def test_messages_name_the_way_to_the_partner_that_is_really_used(self):
        """Review R20: the agent path still said "per SSH" (and printed an empty name in parentheses)."""
        self.env(ENV_HA.format(role="fenced", node="a"))
        self.assertEqual(self.run_body("ha_link_name").stdout.strip(), "per SSH")
        self.pair()
        self.assertIn("Agent", self.run_body("ha_link_name").stdout)
        self.set_rules([{"match": ["ha_cli.py", " call status"], "rc": 4}])
        r = self.run_body("ha_load; cmd_ha_resync --from-peer --yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("über den Agenten", r.stderr)
        self.assertNotIn("SSH", r.stderr)

    def test_the_gate_listens_only_on_this_nodes_own_address(self):
        """Review 2026-11-03 (R16): the gate's port was published on every address; now only on the one the partner reaches."""
        self.env(ENV_BASE)
        self.set_rules(self.PREPARE_RULES)
        self.assertEqual(self.run_body("cmd_ha_prepare --ip 192.168.2.21 --gate-only").returncode, 0)
        self.assertEqual(self.env_value("HA_GATE_BIND"), "192.168.2.21")
        self.assertEqual(self.env_value("COMPOSE_PROFILES").count("ha"), 1)

    def test_an_address_that_is_not_this_hosts_leaves_the_default_instead_of_breaking_the_container(self):
        self.env(ENV_BASE)
        self.set_rules(self.PREPARE_RULES)
        r = self.run_body("cmd_ha_prepare --ip 192.168.2.99 --gate-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.env_value("HA_GATE_BIND"))
        r = self.run_body("cmd_ha_prepare --ip 192.168.2.21 --gate-only", extra_env={"FAKE_LOCAL_ADDRS": ""})   # address list unreadable
        self.assertIsNone(self.env_value("HA_GATE_BIND"))

    def test_changing_the_address_follows_and_dissolving_the_primary_removes_the_setting(self):
        self.env(ENV_BASE)
        self.set_rules(self.PREPARE_RULES)
        self.run_body("cmd_ha_prepare --ip 192.168.2.21 --gate-only")
        self.run_body("cmd_ha_prepare --ip 192.168.1.20 --gate-only")
        self.assertEqual(self.env_value("HA_GATE_BIND"), "192.168.1.20")
        self.run_body("ha_drop_gate_profile")
        self.assertIsNone(self.env_value("HA_GATE_BIND"))

    def test_prepare_refuses_a_node_that_is_already_in_a_pair_but_gate_only_is_fine(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules(self.PREPARE_RULES)
        r = self.run_body("cmd_ha_prepare --ip 192.168.2.21")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schon Teil", r.stderr)
        self.assertFalse(self.called("ha_cli.py prepare"))
        self.assertEqual(self.run_body("cmd_ha_prepare --ip 192.168.2.21 --gate-only").returncode, 0)
        self.assertFalse(self.called("ha_cli.py prepare"))

    def test_prepare_rejects_a_bad_address_before_touching_anything(self):
        self.env(ENV_BASE)
        for bad in ("999.1.1.1", "x", "1.2.3.4;id"):
            r = self.run_body(f"cmd_ha_prepare --ip '{bad}'", extra_env={"FAKE_NOROUTE": "1"})
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertIsNone(self.env_value("COMPOSE_PROFILES"))
        self.assertFalse(self.called("up -d"))

    def test_inside_the_updater_prepare_does_not_try_to_restart_the_updater(self):
        self.env(ENV_BASE)
        self.set_rules(self.PREPARE_RULES)
        self.run_body("cmd_ha_prepare --ip 192.168.2.21", extra_env={"OPNEXUS_UPDATER": "1"})
        self.assertFalse(self.called("up -d --no-build updater"))
        self.assertTrue(self.called("up -d --no-build ha-gate"))


class InitWithCode(Harness):
    """`ha init --code`: Einrichten über den Agenten, ohne SSH."""
    ARGS = "--code OPNX1-AAAAA-BBBBB --vip 192.168.1.22/24 --yes"
    FACTS = "api=1\nmode=release\nversion=1.34.1\nha_role=\ndisk_free=99999999999\nproxy_port=443\ndb_revision=0034\n"

    def setUp(self):
        super().setUp()
        self.env(ENV_BASE + "POSTGRES_USER=opnexus\n")
        (self.node / "opnexus").write_text("#!/bin/bash\n")
        (self.node / "opnexus").chmod(0o755)
        (self.node / ".opnexus-ha" / "agent").mkdir(parents=True)
        (self.node / ".opnexus-ha" / "agent" / "cluster.json").write_text("{}")   # what `pair` would have written
        self.rules_list = [
            {"match": ["pg_database_size"], "out": "6000000000\n"},
            {"match": ["ha_cli.py", " parse "], "out": "ip=192.168.1.21\nport=8445\n"},
            {"match": ["ha_cli.py", " hello "], "out": self.FACTS},
            {"match": ["ha_cli.py", " probe "], "out": '{"status":"ok","role":"replica","epoch":0}\n'},
            {"match": ["ha_cli.py", " gate "], "out": "pin=" + "ab" * 32 + "\nport=8445\n"},
        ]

    def init(self, args=None, rules=None, **kw):
        self.set_rules(rules if rules is not None else self.rules_list)
        body = 'running_version() { echo 1.34.1; }; wait_healthy() { return 0; }; cmd_ha_init_agent ' + (args or self.ARGS)
        return self.run_body(body, **kw)

    def test_the_whole_setup_runs_in_order_without_a_single_ssh_call(self):
        r = self.init()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(self.called("ssh "))
        order = [self.index("ha_cli.py parse"), self.index("ha_cli.py hello"), self.index("ha_cli.py gate"), self.index("ha_cli.py pair"),
                 self.index("ALTER SYSTEM SET ssl"), self.index("call receive/ha-ca"), self.index("call receive/env-init"), self.index("call resync")]
        self.assertEqual(order, sorted(order), "\n".join(self.calls()))
        self.assertLess(self.index("call resync"), self.index("call receive/stamp"))
        # the partner asks us for our state while it rebuilds: the role must already be primary then
        self.assertIn("HA_ROLE=primary", next(l for l in self.calls() if l.startswith("STATE@RESYNC")))
        self.assertIn("--own-pin " + "ab" * 32 + " --own-port 8445", " ".join(self.calls()))

    def test_this_node_ends_as_primary_with_its_state_in_the_state_file(self):
        self.init()
        self.assertEqual([self.state_value(k) for k in ("HA_ROLE", "HA_NODE", "HA_NODE_IP", "HA_PEER_IP", "HA_VIP", "HA_VIP_DEV")],
                         ["primary", "a", "192.168.1.20", "192.168.1.21", "192.168.1.22/24", "eth0"])
        self.assertIsNotNone(self.state_value("HA_REPLICATION_PASSWORD"))
        for key in ("HA_NODE", "HA_PEER_IP", "HA_VIP", "HA_REPLICATION_PASSWORD", "HA_PEER"):
            self.assertIsNone(self.env_value(key), key)
        self.assertIn("docker-compose.ha.yml", self.env_value("COMPOSE_FILE"))
        self.assertTrue((self.node / "docker-compose.ha.yml").exists())

    def test_the_package_for_the_partner_has_what_it_needs_and_nothing_it_must_not_choose(self):
        self.init()
        line = next(l for l in self.calls() if "call receive/env-init" in l)
        for want in ("HA_ROLE=standby", "HA_NODE=b", "HA_NODE_IP=192.168.1.21", "HA_PEER_IP=192.168.1.20", "HA_VIP=192.168.1.22/24",
                     "POSTGRES_PASSWORD=dbpw", "OPNEXUS_SITES=", "FRONTEND_ORIGIN="):
            self.assertIn(want, line)
        for must_not in ("COMPOSE_FILE", "HA_PEER=", "HA_PEER_DIR", "HA_PEER_CLI", "COMPOSE_PROFILES", "OPNEXUS_IMAGE_REPO"):
            self.assertNotIn(must_not, line)

    def test_a_certificate_that_does_not_match_the_code_stops_before_anything_is_sent_or_changed(self):
        r = self.init(rules=[{"match": ["ha_cli.py", " hello "], "rc": 3}] + self.rules_list)   # the first matching rule wins
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("passt nicht zum Code", r.stderr)
        self.assertIn("Nichts wurde gesendet", r.stderr)
        for later in ("ha_cli.py pair", "ALTER SYSTEM", "call receive", "call resync"):
            self.assertFalse(self.called(later), later)

    def test_unreachable_partner_and_rejected_code_have_their_own_messages(self):
        for rc, text in ((4, "antwortet nicht"), (5, "lehnt den Code ab")):
            self.log.unlink(missing_ok=True)
            r = self.init(rules=[x for x in self.rules_list if " hello " not in "".join(x["match"])] + [{"match": ["ha_cli.py", " hello "], "rc": rc}])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(text, r.stderr, rc)
            self.assertFalse(self.called("ha_cli.py pair"))

    def refused(self, facts=None, extra_rules=(), args=None, message=""):
        self.log.unlink(missing_ok=True)
        rules = [x for x in self.rules_list if not (facts and " hello " in "".join(x["match"]))]
        if facts is not None:
            rules.append({"match": ["ha_cli.py", " hello "], "out": facts})
        r = self.init(args=args, rules=list(extra_rules) + rules)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(message, r.stderr)
        for later in ("ha_cli.py pair", "ALTER SYSTEM", "call receive", "call resync"):
            self.assertFalse(self.called(later), later)

    def test_the_setup_from_the_empty_server_towards_one_with_firewalls_is_refused(self):
        """Wrong direction (lab review 2026-11-03): nothing but a warning text stopped overwriting the server that holds the data."""
        with_hosts = self.FACTS + "hosts=3\n"
        self.refused(with_hosts, extra_rules=[{"match": ["opnsense_hosts"], "out": "0\n"}], message="Falsche Richtung")
        for facts, mine, why in ((with_hosts, "2\n", "both have firewalls: the operator knows what is overwritten"),
                                 (self.FACTS + "hosts=0\n", "0\n", "both empty"), (self.FACTS + "hosts=\n", "0\n", "partner count unknown"),
                                 (with_hosts, "", "own count unreadable"), (self.FACTS, "0\n", "an older partner reports nothing")):
            self.log.unlink(missing_ok=True)
            self.setUp()   # a run that succeeded leaves the node as a primary: every case starts from a fresh node
            rules = [{"match": ["opnsense_hosts"], "out": mine}] + [x for x in self.rules_list if " hello " not in "".join(x["match"])] \
                + [{"match": ["ha_cli.py", " hello "], "out": facts}]
            self.assertEqual(self.init(rules=rules).returncode, 0, why)

    def test_the_preflight_refuses_what_would_go_wrong(self):
        self.refused(self.FACTS.replace("version=1.34.1", "version=1.34.0"), message="dieselbe Version")
        self.refused(self.FACTS.replace("api=1", "api=2"), message="Agent-Schnittstelle")
        self.refused(self.FACTS.replace("mode=release", "mode=local"), message="Release-Images")
        self.refused(self.FACTS.replace("ha_role=", "ha_role=standby"), message="schon Teil eines HA-Paars")
        self.refused(self.FACTS.replace("disk_free=99999999999", "disk_free=1000"), message="zu wenig Platz")
        self.refused(extra_rules=[{"match": ["ha_cli.py", " probe "], "rc": 4, "out": ""}], message="Der Proxy des Partners antwortet nicht")

    def test_a_vip_that_answers_is_refused_before_the_partner_is_even_asked(self):
        r = self.init(extra_env={"FAKE_PING_RC": "0"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("antwortet im Netz schon jemand", r.stderr)
        self.assertFalse(self.called("ha_cli.py hello"))

    def test_a_failed_pairing_changes_nothing(self):
        r = self.init(rules=[{"match": ["ha_cli.py", " pair "], "rc": 5}] + self.rules_list)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called("ALTER SYSTEM"))
        self.assertIsNone(self.state_value("HA_ROLE"))
        self.assertFalse((self.node / "docker-compose.ha.yml").exists())

    def test_bad_input_and_double_setup_are_refused(self):
        for args in ("--code OPNX1-AAAAA --vip 999.1.1.1/24 --yes", "--code OPNX1-AAAAA --vip 192.168.1.22 --yes", "--vip 192.168.1.22/24 --yes",
                     "--code OPNX1-AAAAA --vip 192.168.1.22/24 --node a --peer-node a --yes", "--code OPNX1-AAAAA --vip 192.168.1.22/24 --bogus"):
            self.assertNotEqual(self.init(args=args).returncode, 0, args)
        self.assertFalse(self.called("ha_cli.py hello"))
        self.env(ENV_HA.format(role="primary", node="a"))
        r = self.init()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schon Teil eines HA-Paars", r.stderr)

    def test_without_yes_nothing_happens_when_there_is_no_answer(self):
        r = self.init(args="--code OPNX1-AAAAA --vip 192.168.1.22/24")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called("ha_cli.py pair"))

    def test_the_dispatcher_picks_the_agent_path_only_with_a_code(self):
        r = self.run_body('cmd_ha_init_agent() { echo AGENT; }; cmd_ha_init() { echo SSH; }; cmd_ha init --code X --vip Y; cmd_ha init root@h --vip Y')
        self.assertEqual(r.stdout.split(), ["AGENT", "SSH"])


class Lifecycle(Harness):
    """U4/U5: what the pair card in the settings runs: suggest, check-only init, switch, rebuild the standby, dissolve."""
    CODE = "OPNX1-AAAAA-BBBBB"

    def setUp(self):
        super().setUp()
        _exe(self.tmp / "bin" / "python", "#!/bin/sh\necho \"python $*\" >> \"$FAKE_LOG\"\nexit \"${FAKE_PYTHON_RC:-0}\"\n")

    def agent(self):
        (self.node / ".opnexus-ha" / "agent").mkdir(parents=True, exist_ok=True)
        (self.node / ".opnexus-ha" / "agent" / "cluster.json").write_text("{}")

    def node_as(self, role, paired=True):
        self.env(ENV_HA.format(role=role, node="a") + "COMPOSE_PROFILES=updater,ha,gui\n")
        (self.node / "docker-compose.ha.yml").write_text("services: {}\n")
        if paired:
            self.agent()
        else:
            (self.node / ".opnexus-ha" / "agent" / "cluster.json").unlink(missing_ok=True)

    STREAMING = [{"match": ["pg_is_in_recovery"], "out": "f\n"}, {"match": ["pg_stat_replication"], "out": "streaming|0|0\n"}]

    # -- suggest ---------------------------------------------------------------------------
    def test_suggest_names_the_own_address_and_interface_towards_a_target(self):
        self.env(ENV_BASE)
        r = self.run_body("cmd_ha_suggest 192.168.1.21")
        self.assertEqual(r.stdout.split(), ["node_ip=192.168.1.20", "dev=eth0"])
        self.assertEqual(self.run_body("cmd_ha_suggest").returncode, 0)
        for bad in ("999.1.1.1", "x;id", "1.2.3"):
            self.assertNotEqual(self.run_body(f"cmd_ha_suggest '{bad}'").returncode, 0, bad)

    # -- init: the code stays out of the process list, --check changes nothing --------------------------------
    def init_env(self):
        self.env(ENV_BASE + "POSTGRES_USER=opnexus\n")
        (self.node / "opnexus").write_text("#!/bin/bash\n")
        (self.node / "opnexus").chmod(0o755)
        self.agent()
        self.set_rules([
            {"match": ["pg_database_size"], "out": "6000000000\n"},
            {"match": ["ha_cli.py", " parse "], "out": "ip=192.168.1.21\nport=8445\n"},
            {"match": ["ha_cli.py", " hello "], "out": InitWithCode.FACTS},
            {"match": ["ha_cli.py", " probe "], "out": '{"status":"ok","role":"replica","epoch":0}\n'},
            {"match": ["ha_cli.py", " gate "], "out": "pin=" + "ab" * 32 + "\nport=8445\n"},
        ])

    def run_init(self, args, stdin=None):
        return self.run_body('running_version() { echo 1.34.1; }; wait_healthy() { return 0; }; cmd_ha_init_agent ' + args, stdin=stdin)

    def test_the_code_reaches_the_agent_only_through_stdin(self):
        self.init_env()
        r = self.run_init("--code - --vip 192.168.1.22/24 --yes", stdin=self.CODE + "\n")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        for verb in ("parse", "hello", "pair"):
            line = next(l for l in self.calls() if f"ha_cli.py {verb} -" in l)
            self.assertIn(self.CODE, line.split("<<<")[1], verb)            # on stdin ...
            self.assertNotIn(self.CODE, line.split("<<<")[0], verb)         # ... never on the command line
        self.assertFalse(any(self.CODE in l.split("<<<")[0] for l in self.calls()))

    def test_even_a_code_given_as_an_argument_is_handed_on_through_stdin(self):
        self.init_env()
        self.run_init(f"--code {self.CODE} --vip 192.168.1.22/24 --yes")
        self.assertFalse(any(self.CODE in l.split("<<<")[0] for l in self.calls()))

    def test_check_only_stops_after_the_preflight_and_leaves_everything_alone(self):
        self.init_env()
        r = self.run_init("--code - --vip 192.168.1.22/24 --check", stdin=self.CODE + "\n")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("Vorprüfung bestanden", r.stdout)
        for later in ("ha_cli.py pair", "ALTER SYSTEM", "call receive", "call resync", " gate "):
            self.assertFalse(self.called(later), later)
        self.assertIsNone(self.state_value("HA_ROLE"))
        self.assertFalse((self.node / "docker-compose.ha.yml").exists())

    def test_check_only_still_refuses_what_the_real_run_would_refuse(self):
        self.init_env()
        self.set_rules([{"match": ["ha_cli.py", " hello "], "out": InitWithCode.FACTS.replace("version=1.34.1", "version=1.30.0")},
                        {"match": ["ha_cli.py", " parse "], "out": "ip=192.168.1.21\nport=8445\n"}])
        r = self.run_init("--code - --vip 192.168.1.22/24 --check", stdin=self.CODE + "\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("dieselbe Version", r.stderr)

    # -- switch and rebuild --------------------------------------------------------------------------
    def test_the_primary_asks_the_partner_to_take_over(self):
        self.node_as("primary")
        self.set_rules(self.STREAMING)
        r = self.run_body("cmd_ha_switch")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(self.called("ha_cli.py call ping"))
        self.assertTrue(self.called("ha_cli.py call promote"))
        self.assertLess(self.index("call ping"), self.index("call promote"))

    def test_a_switch_is_refused_unless_every_condition_holds(self):
        stream = self.STREAMING
        for label, role, paired, rules, message in (
                ("standby", "standby", True, stream, "Nur der Primär"),
                ("ssh pair", "primary", False, stream, "Agenten-Kopplung"),
                ("partner silent", "primary", True, [{"match": ["call ping"], "rc": 4}] + stream, "antwortet nicht"),
                ("not streaming", "primary", True, [{"match": ["pg_is_in_recovery"], "out": "f\n"}, {"match": ["pg_stat_replication"], "out": "|||\n"}], "streamt nicht")):
            self.node_as(role, paired)
            self.log.unlink(missing_ok=True)
            self.set_rules(rules)
            r = self.run_body("cmd_ha_switch")
            self.assertNotEqual(r.returncode, 0, label)
            self.assertIn(message, r.stderr, label)
            self.assertFalse(self.called("call promote"), label)

    def test_rebuilding_the_standby_goes_through_the_agent_and_only_from_the_primary(self):
        self.node_as("primary")
        self.assertEqual(self.run_body("cmd_ha_rebuild_standby").returncode, 0)
        self.assertTrue(self.called("ha_cli.py call resync"))
        self.node_as("standby")
        self.log.unlink()
        self.assertNotEqual(self.run_body("cmd_ha_rebuild_standby").returncode, 0)
        self.assertFalse(self.called("call resync"))
        self.node_as("primary")
        self.set_rules([{"match": ["call ping"], "rc": 4}])
        r = self.run_body("cmd_ha_rebuild_standby")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called("call resync"))

    # -- dissolve: the partner's side -------------------------------------------------------------------
    def test_the_partner_side_wipes_the_copy_and_comes_back_as_an_empty_installation(self):
        self.node_as("standby")
        (self.tmp / "vip").touch()
        self.set_rules([{"match": ["config --services"], "out": "postgres\nbackend\nfrontend\nproxy\nupdater\nha-gate\n"}])
        r = self.run_body("cmd_ha_dissolve --partner-side")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        calls = self.calls()
        stop = next(l for l in calls if "compose stop" in l)
        self.assertEqual(stop.split("compose stop ")[1].split(), ["postgres", "backend", "frontend", "proxy"])   # not the updater, not the gate
        wipe_db = next(i for i, l in enumerate(calls) if 'find "$PGDATA" -mindepth 1 -delete' in l)
        wipe_secrets = next(i for i, l in enumerate(calls) if "/data/$d" in l and "totp-secrets" in l and "opnsense-secrets" in l and "opnsense-backups" in l)
        up = next(i for i, l in enumerate(calls) if "compose up -d" in l)
        self.assertLess(calls.index(stop), wipe_db)
        self.assertLess(wipe_db, up)
        self.assertLess(wipe_secrets, up)
        self.assertTrue(any("rm -rf /data/caddy/pki/authorities/local" in l for l in calls))   # the primary's CA is no longer ours
        self.assertFalse((self.tmp / "vip").exists())
        for gone in ("docker-compose.ha.yml", ".opnexus-ha/state", ".opnexus-ha/ha-ca.crt"):
            self.assertFalse((self.node / gone).exists(), gone)
        env = (self.node / ".env").read_text()
        self.assertNotIn("HA_ROLE=", env)
        self.assertNotIn("COMPOSE_FILE=", env)
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "updater,ha,gui")   # the gate stays: the primary follows this job through it
        self.assertTrue((self.node / ".opnexus-ha" / "agent" / "cluster.json").exists())   # and the pairing stays until it has seen the end

    def test_a_primary_is_never_wiped_on_the_partners_request(self):
        self.node_as("primary")
        r = self.run_body("cmd_ha_dissolve --partner-side")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Primär", r.stderr)
        self.assertFalse(self.called("find"))
        self.assertFalse(self.called("compose stop"))
        self.assertTrue((self.node / "docker-compose.ha.yml").exists())

    # -- dissolve: the primary's side ---------------------------------------------------------------------
    def test_the_primary_has_the_partner_wiped_first_and_then_becomes_a_single_installation(self):
        self.node_as("primary")
        (self.tmp / "vip").touch()
        r = self.run_body("cmd_ha_dissolve --yes")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        calls = self.calls()
        partner = next(i for i, l in enumerate(calls) if "ha_cli.py call dissolve" in l)
        slot = next(i for i, l in enumerate(calls) if "pg_drop_replication_slot" in l)
        up = max(i for i, l in enumerate(calls) if "compose up -d" in l)
        self.assertLess(partner, slot)
        self.assertLess(slot, up)
        text = "\n".join(calls)
        for sql in ("DROP ROLE IF EXISTS opnexus_replicator", "ALTER SYSTEM RESET max_slot_wal_keep_size", "DELETE FROM ha_state"):
            self.assertIn(sql, text)
        self.assertIn("pg_hba.conf", text)
        self.assertTrue(any("compose stop ha-gate" in l for l in calls))
        self.assertTrue(self.called("ha_cli.py unpair"))
        self.assertFalse((self.tmp / "vip").exists())
        for gone in ("docker-compose.ha.yml", ".opnexus-ha/state"):
            self.assertFalse((self.node / gone).exists(), gone)
        env = (self.node / ".env").read_text()
        self.assertNotIn("HA_ROLE=", env)
        self.assertNotIn("COMPOSE_FILE=", env)
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "updater,gui")      # the gate profile goes, the others stay

    SERVICES = "postgres\nbackend\nfrontend\nproxy\nupdater\nha-gate\npostgres-backup\n"

    def ups(self):
        return [l for l in self.calls() if "compose up -d" in l]

    def test_dissolving_inside_the_updater_never_recreates_the_updater_itself(self):
        # Lab 2026-11-03: `up -d` for everything made the updater replace itself in the middle of its own job
        for side, args in (("primary", "--yes"), ("standby", "--partner-side")):
            self.node_as(side)
            self.log.unlink(missing_ok=True)
            self.set_rules([{"match": ["config --services"], "out": self.SERVICES}])
            r = self.run_body(f"cmd_ha_dissolve {args}", extra_env={"OPNEXUS_UPDATER": "1"})
            self.assertEqual(r.returncode, 0, side + r.stderr + r.stdout)
            ups = self.ups()
            self.assertTrue(ups, side)
            for line in ups:
                services = line.split("--no-build")[1].split()
                self.assertTrue(services, f"{side}: a bare `up -d` also starts the updater: {line}")
                self.assertNotIn("updater", services, f"{side}: {line}")

    def test_on_the_host_the_updater_is_renewed_with_the_rest(self):
        self.node_as("primary")
        self.set_rules([{"match": ["config --services"], "out": self.SERVICES}])
        self.assertEqual(self.run_body("cmd_ha_dissolve --yes").returncode, 0)
        self.assertIn("updater", self.ups()[-1].split("--no-build")[1].split())

    def test_a_partner_that_cannot_be_wiped_leaves_the_primary_untouched(self):
        self.node_as("primary")
        for rules in ([{"match": ["call ping"], "rc": 4}], [{"match": ["call dissolve"], "rc": 1}]):
            self.log.unlink(missing_ok=True)
            self.set_rules(rules)
            r = self.run_body("cmd_ha_dissolve --yes")
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse(self.called("pg_drop_replication_slot"))
            self.assertEqual(self.state_value("HA_ROLE"), "primary")
            self.assertTrue((self.node / "docker-compose.ha.yml").exists())
            self.assertFalse(self.called("ha_cli.py unpair"))

    def test_local_only_dissolves_without_asking_the_partner(self):
        self.node_as("primary")
        self.set_rules([{"match": ["call ping"], "rc": 4}])
        r = self.run_body("cmd_ha_dissolve --yes --local-only")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(self.called("call dissolve"))
        self.assertTrue(self.called("pg_drop_replication_slot"))
        self.assertIsNone(self.state_value("HA_ROLE"))

    def test_only_the_primary_dissolves_and_without_yes_nothing_happens_when_there_is_no_answer(self):
        for role in ("standby", "fenced"):
            self.node_as(role)
            self.assertNotEqual(self.run_body("cmd_ha_dissolve --yes").returncode, 0, role)
        self.node_as("primary")
        self.log.unlink(missing_ok=True)
        r = self.run_body("cmd_ha_dissolve")           # no tty: the question gets no answer
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called("call dissolve"))
        self.assertFalse(self.called("pg_drop_replication_slot"))

    def test_the_dispatcher_knows_the_new_commands(self):
        self.env(ENV_BASE)
        r = self.run_body("cmd_ha_switch() { echo SWITCH; }; cmd_ha_rebuild_standby() { echo REBUILD; }; cmd_ha_dissolve() { echo DISSOLVE \"$@\"; }; "
                          "cmd_ha_suggest() { echo SUGGEST; }; cmd_ha switch; cmd_ha rebuild-standby; cmd_ha dissolve --yes; cmd_ha suggest")
        self.assertEqual(r.stdout.split(), ["SWITCH", "REBUILD", "DISSOLVE", "--yes", "SUGGEST"])


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
        (self.node / ".opnexus-ha").mkdir(exist_ok=True)
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

    def test_the_standby_runs_the_full_stack_but_not_the_primarys_extras(self):
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        streaming = self.index("pg_stat_wal_receiver")
        web = self.index("up -d --no-build frontend proxy")
        self.assertGreater(web, streaming)   # only after the replication check passed
        for primary_only in ("wg-hub", "gui-gateway", "postgres-backup"):
            self.assertFalse(self.called(primary_only), primary_only)
        self.assertEqual(sum(1 for l in calls if "up -d" in l), 2)

    def test_a_web_stack_that_will_not_start_does_not_fail_the_rebuild(self):
        self.set_rules([{"match": ["pg_stat_wal_receiver"], "out": "streaming\n"}, {"match": ["up -d --no-build frontend proxy"], "rc": 1}])
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Proxy und Frontend ließen sich nicht starten", r.stdout)

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


class ResyncFromPeer(Harness):
    """H2-8: the old primary of a forced promote still says HA_ROLE=primary (the .env of a dead node cannot be changed)."""

    def setUp(self):
        super().setUp()
        (self.node / ".opnexus-ha").mkdir(exist_ok=True)
        (self.node / ".opnexus-ha" / "ha-ca.crt").write_text("-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n")

    def resync(self, partner="db_role=primary\nepoch=5\nprimary_node=b\n", own_epoch="3\n", args="--from-peer --yes"):
        self.env(ENV_HA.format(role="primary", node="a"))
        (self.tmp / "vip").touch()
        self.set_rules([{"match": ["pg_stat_wal_receiver"], "out": "streaming\n"}, {"match": ["SELECT epoch FROM ha_state"], "out": own_epoch}],
                       ssh=[{"match": ["status --porcelain"], "out": partner}])
        return self.run_body(f"cmd_ha_resync {args}")

    def test_without_the_flag_the_refusal_names_the_way_out(self):
        r = self.resync(args="--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--from-peer", r.stderr)
        self.assertFalse(self.called("compose stop"))

    def test_a_partner_with_a_higher_epoch_turns_the_old_primary_into_the_standby(self):
        r = self.resync()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.env_value("HA_ROLE"), "standby")
        self.assertFalse((self.tmp / "vip").exists())

    def test_an_equal_or_lower_partner_epoch_is_refused_untouched(self):
        for partner_epoch in ("3", "2", ""):
            r = self.resync(partner=f"db_role=primary\nepoch={partner_epoch}\nprimary_node=b\n")
            self.assertNotEqual(r.returncode, 0, partner_epoch)
            self.assertIn("höhere Epoche", r.stderr)
        self.assertFalse(self.called("compose stop"))
        self.assertEqual(self.env_value("HA_ROLE"), "primary")

    def test_an_unreadable_own_epoch_is_refused_untouched(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["SELECT epoch FROM ha_state"], "out": ""}], ssh=[{"match": ["status --porcelain"], "out": "db_role=primary\nepoch=5\nprimary_node=b\n"}])
        r = self.run_body("cmd_ha_resync --from-peer --yes", extra_env={"OPNEXUS_HA_DB_WAIT": "2"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nicht lesbar", r.stderr)
        self.assertFalse(self.called("compose stop"))
        self.assertEqual(self.env_value("HA_ROLE"), "primary")

    def test_a_stopped_database_is_started_to_read_the_epoch(self):
        # the first read fails (stack stopped), the database comes up on `up -d postgres`, then the epoch is readable
        marker = self.tmp / "db-up"
        script = self.tmp / "bin" / "docker"
        script.write_text(script.read_text().replace('rules = json.load', f'''if "up -d --no-build postgres" in line and "backend" not in line:
    open("{marker}", "w").write("1")
if "SELECT epoch FROM ha_state" in line and not os.path.exists("{marker}"):
    sys.exit(1)
rules = json.load''', 1))
        r = self.resync()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(self.called("up -d --no-build postgres"))
        self.assertEqual(self.env_value("HA_ROLE"), "standby")

    def test_a_partner_that_names_this_node_as_primary_is_refused(self):
        r = self.resync(partner="db_role=primary\nepoch=5\nprimary_node=a\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("widersprüchlich", r.stderr)
        self.assertFalse(self.called("compose stop"))

    def test_a_partner_database_that_is_not_primary_is_refused(self):
        r = self.resync(partner="db_role=replica\nepoch=5\nprimary_node=b\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called("compose stop"))


class ReviewFixes(Harness):
    """Befunde R2-R18 des Opus-Reviews von U0 bis U2 (docs/DESIGN-HA-UI.md 9b)."""

    # R2 -------------------------------------------------------------------------
    def test_the_overlay_and_gui_profiles_and_their_ports_travel_to_the_partner(self):
        self.env(ENV_HA.format(role="primary", node="a") + "COMPOSE_PROFILES=updater,ha,gui,overlay\nGUI_PORTS=9441-9444\nOVERLAY_PORT=51820\n")
        lines = self.run_body("ha_sync_lines").stdout.splitlines()
        self.assertIn("HA_SYNC_PROFILES=overlay,gui", lines)
        self.assertIn("GUI_PORTS=9441-9444", lines)
        self.assertIn("OVERLAY_PORT=51820", lines)
        self.assertFalse([l for l in lines if l.startswith("COMPOSE_PROFILES")])   # the raw line (updater, ha) never goes

    def test_no_overlay_or_gui_on_the_primary_sends_an_empty_list_so_the_partner_drops_them_too(self):
        self.env(ENV_HA.format(role="primary", node="a") + "COMPOSE_PROFILES=updater,ha\n")
        self.assertIn("HA_SYNC_PROFILES=", self.run_body("ha_sync_lines").stdout.splitlines())

    def test_the_partner_takes_only_overlay_and_gui_and_keeps_updater_and_ha(self):
        self.env(ENV_HA.format(role="standby", node="b") + "COMPOSE_PROFILES=updater,ha,gui\n")
        self.assertEqual(self.run_body("cmd_ha receive env", stdin="HA_SYNC_PROFILES=overlay\n").returncode, 0)
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "updater,ha,overlay")
        self.run_body("cmd_ha receive env", stdin="HA_SYNC_PROFILES=\n")
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "updater,ha")
        for evil in ("updater", "ha", "overlay,updater", "gui;x", "overlay,ha"):
            self.run_body("cmd_ha receive env", stdin=f"HA_SYNC_PROFILES={evil}\n")
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "updater,ha")

    # R3 -------------------------------------------------------------------------
    TRICKY = 'p+a/s=s!\\n$HOME"q&|#x y'

    def test_a_free_form_database_password_is_taken_verbatim_by_the_setup_package(self):
        self.env(ENV_BASE)
        self.pair()
        payload = AgentPath.PAYLOAD.replace("POSTGRES_PASSWORD=dbpw", "POSTGRES_PASSWORD=" + self.TRICKY)
        r = self.run_body("cmd_ha receive env-init", stdin=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("POSTGRES_PASSWORD"), self.TRICKY)

    def test_env_set_literal_keeps_backslashes_and_ampersands(self):
        self.env(ENV_BASE)
        self.run_body("env_set_literal POSTGRES_PASSWORD \"$1\"".replace("$1", self.TRICKY.replace('"', '\\"').replace("$", "\\$")))
        self.assertEqual(self.env_value("POSTGRES_PASSWORD"), self.TRICKY)

    def test_control_characters_in_the_password_are_refused(self):
        self.env(ENV_BASE)
        self.pair()
        for bad in ("a\x01b", "a\tb", ""):
            r = self.run_body("cmd_ha receive env-init", stdin=AgentPath.PAYLOAD.replace("POSTGRES_PASSWORD=dbpw", "POSTGRES_PASSWORD=" + bad))
            self.assertNotEqual(r.returncode, 0, repr(bad))

    def pair(self):
        (self.node / ".opnexus-ha" / "agent").mkdir(parents=True, exist_ok=True)
        (self.node / ".opnexus-ha" / "agent" / "cluster.json").write_text("{}")

    def test_a_password_the_partner_would_refuse_stops_the_setup_before_anything_happens(self):
        self.env(ENV_BASE.replace("POSTGRES_PASSWORD=dbpw", "POSTGRES_PASSWORD=bad\x01pw"))
        self.set_rules([])
        r = self.run_body("cmd_ha_init_agent --code OPNX1-AAAAA --vip 192.168.1.22/24 --yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("POSTGRES_PASSWORD", r.stderr)
        self.assertFalse(self.called("ha_cli.py"))

    # R4 -------------------------------------------------------------------------
    def stale_primary(self):
        (self.node / ".opnexus-ha").mkdir(exist_ok=True)
        (self.node / ".opnexus-ha" / "ha-ca.crt").write_text("-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n")
        self.env(ENV_HA.format(role="primary", node="a"))
        (self.tmp / "vip").touch()
        self.set_rules([{"match": ["pg_stat_wal_receiver"], "out": "streaming\n"}, {"match": ["SELECT epoch FROM ha_state"], "out": "3\n"}],
                       ssh=[{"match": ["status --porcelain"], "out": "db_role=primary\nepoch=5\nprimary_node=b\n"}])

    def test_a_failed_rebuild_leaves_the_old_primary_fenced_without_the_vip_and_a_retry_works(self):
        self.stale_primary()
        r = self.run_body("cmd_ha_resync --from-peer --yes", extra_env={"FAKE_BASEBACKUP_RC": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.state_value("HA_ROLE"), self.env_value("HA_ROLE")), ("fenced", "fenced"))
        self.assertFalse((self.tmp / "vip").exists())
        r = self.run_body("cmd_ha_resync --from-peer --yes")   # no epoch check is needed any more: it is no primary
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.state_value("HA_ROLE"), "standby")

    def test_the_old_primary_is_fenced_before_the_data_directory_is_wiped(self):
        self.stale_primary()
        (self.pgdata / "old-file").write_text("stale")
        self.run_body("cmd_ha_resync --from-peer --yes", extra_env={"FAKE_BASEBACKUP_RC": "1"})
        self.assertFalse((self.pgdata / "old-file").exists())   # the wipe did happen ...
        self.assertEqual(self.state_value("HA_ROLE"), "fenced")  # ... after the role changed

    # R6 / R18 ---------------------------------------------------------------------
    def standby_update(self, profiles, extra_env=None):
        self.env(ENV_HA.format(role="standby", node="b") + f"COMPOSE_PROFILES={profiles}\nOPNEXUS_IMAGE_REPO=reg.test/opnexus\n")
        _exe(self.tmp / "bin" / "cosign", NOOP)
        (self.node / "cosign.pub").write_text("key")
        self.set_rules([{"match": ["service ps backend"], "out": "healthy\n"}, {"match": ["--format {{.Health}}"], "out": "healthy\n"}])
        return self.run_body("refresh_cli() { :; }; refresh_compose() { :; }; service_state() { echo healthy; }; cmd_ha update-standby 1.37.1", extra_env=extra_env)

    def test_the_standby_update_also_pulls_and_checks_the_updater_and_restarts_the_gate(self):
        r = self.standby_update("updater,ha")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        pull = next(l for l in self.calls() if "compose pull" in l)
        self.assertEqual(pull.split("compose pull ")[1].split(), ["backend", "frontend", "proxy", "updater"])
        self.assertTrue(any("cosign verify" in l and "opnexus-updater:1.37.1" in l for l in self.calls()))
        self.assertTrue(any("up -d --no-build backend frontend proxy ha-gate" in l for l in self.calls()))

    def test_without_the_agent_profiles_nothing_changes_for_a_plain_pair(self):
        r = self.standby_update("")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(next(l for l in self.calls() if "compose pull" in l).split("compose pull ")[1].split(), ["backend", "frontend", "proxy"])
        self.assertTrue(any(l.rstrip().endswith("up -d --no-build backend frontend proxy") for l in self.calls()))

    def test_an_unsigned_check_is_not_skipped_for_a_partner_job(self):
        self.env(ENV_HA.format(role="standby", node="b") + "OPNEXUS_IMAGE_REPO=reg.test/opnexus\n")   # no cosign.pub, no cosign
        r = self.run_body("refresh_cli() { :; }; cmd_ha update-standby 1.37.1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("kein Image", r.stderr)
        self.assertFalse(self.called("up -d"))
        self.assertIsNone(self.env_value("OPNEXUS_VERSION") if self.env_value("OPNEXUS_VERSION") == "1.37.1" else None)

    def test_the_plain_update_still_skips_the_check_without_cosign(self):
        self.env(ENV_BASE)
        r = self.run_body("verify_signatures 1.37.1 backend")
        self.assertEqual(r.returncode, 0)
        self.assertIn("übersprungen", r.stdout)

    def test_ha_update_over_the_agent_wants_a_fixed_version(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        (self.node / ".opnexus-ha" / "agent").mkdir(parents=True)
        (self.node / ".opnexus-ha" / "agent" / "cluster.json").write_text("{}")
        for target in ("latest", "1.37", "1.37.1-rc1"):
            r = self.run_body(f"cmd_ha_update {target}")
            self.assertNotEqual(r.returncode, 0, target)
            self.assertIn("feste Version", r.stderr, target)
        self.assertFalse(self.called("ha_cli.py call"))

    # R12 ------------------------------------------------------------------------
    def test_a_restore_does_not_take_the_ha_state_or_the_gate_profile_from_the_backup(self):
        self.env(ENV_BASE + "COMPOSE_PROFILES=gui\n")
        backup = self.tmp / "backup.env"
        backup.write_text(ENV_HA.format(role="primary", node="a") + "COMPOSE_PROFILES=updater,ha,gui\nGUI_PORTS=9441-9444\n")
        r = self.run_body(f"merge_env_from_backup '{backup}' 1")
        self.assertEqual(r.returncode, 0, r.stderr)
        for key in ("HA_ROLE", "HA_NODE", "HA_VIP", "COMPOSE_FILE"):
            self.assertIsNone(self.env_value(key), key)
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "updater,gui")   # `ha` is dropped, the rest is the old behaviour
        self.assertEqual(self.env_value("GUI_PORTS"), "9441-9444")

    # R14 ------------------------------------------------------------------------
    def test_the_state_file_is_written_in_one_step_so_a_failure_leaves_the_env_untouched_and_a_retry_works(self):
        self.env_legacy(ENV_HA.format(role="standby", node="b"))
        r = self.run_body("mv() { return 1; }; ha_state_migrate")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.node / ".opnexus-ha" / "state").exists())
        env = (self.node / ".env").read_text()
        for key in ("HA_NODE", "HA_VIP", "HA_REPLICATION_PASSWORD", "HA_ROLE"):
            self.assertIn(key + "=", env, key)
        self.assertEqual([p.name for p in (self.node / ".opnexus-ha").iterdir()], [])   # no temp file left behind
        r = self.run_body("ha_load; echo $HA_NODE")
        self.assertEqual(r.stdout.split()[-1], "b")
        self.assertEqual(self.state_value("HA_REPLICATION_PASSWORD"), "abcdefghij0123456789abcdefghij0123456789")

    # R13 ------------------------------------------------------------------------
    def test_openssl_is_checked_before_the_pairing(self):
        text = SCRIPT.read_text()
        body = text[text.index("cmd_ha_init_agent() {"):text.index("\ncmd_ha() {")]
        self.assertLess(body.index("command -v openssl"), body.index("ha_agent_py pair"))

    # R17 ------------------------------------------------------------------------
    def make_tar(self, name, entries):
        import io, tarfile
        path = self.tmp / name
        with tarfile.open(path, "w") as tar:
            for entry in entries:
                info = tarfile.TarInfo(entry["name"])
                if entry.get("link"):
                    info.type = tarfile.SYMTYPE if entry["link"] == "sym" else tarfile.LNKTYPE
                    info.linkname = entry.get("target", "x")
                    tar.addfile(info)
                else:
                    data = b"x\n"
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
        return path

    def test_the_ca_archive_must_be_plain_files_below_its_own_directory(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        good = self.make_tar("good.tar", [{"name": "local/root.crt"}, {"name": "local/intermediate.key"}])
        r = self.run_body(f"cmd_ha receive ca {'a' * 64} < '{good}'")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.called("rm -rf /data/caddy/pki/authorities/local"))
        # a valid CA directory with one stray member must not be accepted either (the earlier cases lack `local/`, which restore_ca refuses by itself)
        for label, stray in (("valid plus dotdot", "../evil"), ("valid plus absolute", "/tmp/evil"), ("valid plus nested dotdot", "local/../../evil")):
            self.log.unlink(missing_ok=True)
            bad = self.make_tar(f"{label.replace(' ', '_')}.tar", [{"name": "local/root.crt"}, {"name": stray}])
            r = self.run_body(f"cmd_ha receive ca {'a' * 64} < '{bad}'")
            self.assertNotEqual(r.returncode, 0, label)
            self.assertFalse(self.called("rm -rf /data/caddy"), label)
        for label, entries in (("dotdot", [{"name": "../evil"}]), ("nested dotdot", [{"name": "local/../../evil"}]), ("absolute", [{"name": "/etc/evil"}]),
                               ("symlink", [{"name": "local", "link": "sym", "target": "/etc"}]), ("hardlink", [{"name": "local/x", "link": "hard", "target": "local/y"}])):
            self.log.unlink(missing_ok=True)
            bad = self.make_tar(f"{label}.tar", entries)
            r = self.run_body(f"cmd_ha receive ca {'a' * 64} < '{bad}'")
            self.assertNotEqual(r.returncode, 0, label)
            self.assertFalse(self.called("rm -rf /data/caddy"), label)

    def test_garbage_instead_of_an_archive_is_refused_too(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        r = self.run_body(f"cmd_ha receive ca {'a' * 64}", stdin="this is not a tar\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called("rm -rf /data/caddy"))


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

    def flip_recovery(self):
        """The first pg_is_in_recovery says replica, every later one primary (the database was promoted in between)."""
        counter = self.tmp / "recovery-count"
        counter.unlink(missing_ok=True)
        script = self.tmp / "bin" / "docker"
        script.write_text(script.read_text().replace('rules = json.load', f'''if "pg_is_in_recovery" in line:
    n = int(open("{counter}").read()) if os.path.exists("{counter}") else 0
    open("{counter}", "w").write(str(n + 1))
    print("t" if n < 1 else "f"); sys.exit(0)
rules = json.load''', 1))
        return [r for r in self.rules_list if r["match"] != ["pg_is_in_recovery"]]

    def test_the_person_who_took_over_is_written_to_the_audit_trail_of_the_new_primary(self):
        rules = self.flip_recovery()
        r = self.promote("--yes", rules=rules, extra_env={"OPNEXUS_HA_ACTOR": "3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b"})
        self.assertEqual(r.returncode, 0, r.stderr)
        audit = self.index("python -m app.ha_audit promote 3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b normal")
        self.assertGreater(audit, self.index("pg_promote"))
        self.assertGreater(audit, self.index("restart backend"))   # after the backend came back as a writer
        self.log.unlink()
        self.env(ENV_HA.format(role="standby", node="b"))
        rules = self.flip_recovery()
        r = self.promote("--force --yes", rules=rules, ssh=[{"match": [""], "rc": 255}], extra_env={"OPNEXUS_HA_ACTOR": "3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.called("python -m app.ha_audit promote 3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b force"))

    def test_without_an_actor_nothing_is_audited_and_a_failing_audit_does_not_undo_the_takeover(self):
        rules = self.flip_recovery()
        self.promote("--yes", rules=rules)
        self.assertFalse(self.called("ha_audit"))
        self.log.unlink()
        self.env(ENV_HA.format(role="standby", node="b"))
        (self.tmp / "recovery-count").unlink()
        self.set_rules([{"match": ["app.ha_audit"], "rc": 1}] + rules, ssh=self.ssh_ok)
        r = self.run_body("cmd_ha_promote --yes", extra_env={"OPNEXUS_HA_ACTOR": "3f2b8c1e-5d4a-4e6f-9a7b-0c1d2e3f4a5b"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Audit-Eintrag", r.stdout)
        self.assertEqual(self.state_value("HA_ROLE"), "primary")

    def test_force_is_refused_with_yes_when_the_partner_still_answers_as_leader(self):
        # only SSH is down: the partner's service lives, a promote would make two primaries
        r = self.promote("--force --yes", ssh=[{"match": [""], "rc": 255}],
                         extra_env={"FAKE_CURL_OUT": '{"status":"ok","role":"leader","epoch":4}'})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("kein Promote mit --yes", r.stderr)
        self.assertFalse(self.called("pg_promote"))
        self.assertEqual(self.env_value("HA_ROLE"), "standby")

    def test_force_without_yes_needs_the_typed_sentence_when_the_partner_answers_as_leader(self):
        r = self.promote("--force", ssh=[{"match": [""], "rc": 255}], extra_env={"FAKE_CURL_OUT": '{"role":"leader"}'})  # no tty: no answer
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called("pg_promote"))

    def test_force_still_promotes_when_the_partner_answers_as_fenced_or_not_at_all(self):
        for out, rc in (('{"role":"fenced","epoch":4}', "0"), ("", "7")):
            r = self.promote("--force --yes", ssh=[{"match": [""], "rc": 255}], extra_env={"FAKE_CURL_OUT": out, "FAKE_CURL_RC": rc})
            self.assertTrue(self.called("pg_promote"), out + r.stderr)
            self.log.unlink()
            self.env(ENV_HA.format(role="standby", node="b"))

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
        _exe(self.tmp / "bin" / "curl", '#!/bin/sh\necho \'{"status":"ok","role":"leader","epoch":9}\'\n')
        self.set_rules([{"match": ["SELECT epoch FROM ha_state"], "out": "8\n"}])
        r = self.run_body("cmd_ha vip up --boot")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.tmp / "vip").exists())
        self.assertIn("Partner ist Leader mit höherer Epoche", r.stdout)

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


class VipReconcile(Harness):
    """`ha vip reconcile` (Zeitgeber des Agenten): die VIP gehört dem Primär und nur ihm."""

    def reconcile(self, role, vip_present, partner=None, own_epoch=None, backend_role=""):
        self.log.unlink(missing_ok=True)
        self.env(ENV_HA.format(role=role, node="a"))
        rules = [{"match": ["urlopen", "api/health"], "out": backend_role + "\n"}]
        if own_epoch is not None:
            rules.append({"match": ["SELECT epoch FROM ha_state"], "out": f"{own_epoch}\n"})
        self.set_rules(rules)
        vipfile = self.tmp / "vip"
        vipfile.unlink(missing_ok=True)
        if vip_present:
            vipfile.touch()
        extra = {"FAKE_CURL_OUT": partner} if partner else {}
        r = self.run_body("cmd_ha vip reconcile", extra_env=extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return vipfile.exists()

    def test_a_primary_without_the_vip_takes_it(self):
        self.assertTrue(self.reconcile("primary", False))
        self.assertTrue(self.called("ip addr add 192.168.1.22/24 dev eth0"))

    def test_a_primary_that_has_it_does_nothing(self):
        self.assertTrue(self.reconcile("primary", True))
        self.assertFalse(self.called("addr add"))
        self.assertFalse(self.called("addr del"))

    LEADER9 = '{"status":"ok","role":"leader","epoch":9}'
    LEADER7 = '{"status":"ok","role":"leader","epoch":7}'

    def test_a_stale_primary_does_not_take_the_vip_from_a_leader_with_a_higher_epoch(self):
        self.assertFalse(self.reconcile("primary", False, partner=self.LEADER9, own_epoch=8))
        self.assertFalse(self.called("addr add"))

    def test_a_stale_primary_gives_the_vip_back_when_the_partner_turns_out_to_be_leader(self):
        # a partition heals while the old primary still holds the address: it must let go, not keep it every minute
        self.assertFalse(self.reconcile("primary", True, partner=self.LEADER9, own_epoch=8))
        self.assertTrue(self.called("addr del"))

    def test_the_real_primary_keeps_and_takes_the_vip_against_a_zombie_with_a_lower_epoch(self):
        self.assertTrue(self.reconcile("primary", True, partner=self.LEADER7, own_epoch=8))
        self.assertFalse(self.called("addr del"))
        self.assertTrue(self.reconcile("primary", False, partner=self.LEADER7, own_epoch=8))
        self.assertTrue(self.called("addr add"))

    def test_an_unreadable_own_epoch_changes_nothing_either_way(self):
        self.assertTrue(self.reconcile("primary", True, partner=self.LEADER9))
        self.assertFalse(self.called("addr del"))
        self.assertFalse(self.reconcile("primary", False, partner=self.LEADER9))
        self.assertFalse(self.called("addr add"))

    def test_a_backend_that_the_partner_guard_fenced_never_holds_the_vip(self):
        self.assertFalse(self.reconcile("primary", True, partner="", own_epoch=8, backend_role="fenced"))
        self.assertTrue(self.called("addr del"))
        self.assertFalse(self.reconcile("primary", False, partner="", own_epoch=8, backend_role="fenced"))
        self.assertFalse(self.called("addr add"))

    def test_a_silent_partner_is_asked_twice_before_it_counts_as_no_leader(self):
        _exe(self.tmp / "bin" / "curl", '#!/bin/sh\necho "curl $*" >> "$FAKE_LOG"\nexit 7\n')
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["urlopen", "api/health"], "out": "leader\n"}])
        r = self.run_body("cmd_ha vip reconcile")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sum(1 for l in self.calls() if l.startswith("curl")), 2)
        self.assertTrue((self.tmp / "vip").exists())   # still taken: nobody objected

    def test_a_primary_takes_it_when_the_partner_is_silent_or_not_a_leader(self):
        self.assertTrue(self.reconcile("primary", False, partner='{"role":"replica"}'))
        self.assertTrue(self.reconcile("primary", False, partner='{"role":"fenced"}'))

    def test_a_standby_or_fenced_node_gives_the_vip_up(self):
        for role in ("standby", "fenced"):
            self.assertFalse(self.reconcile(role, True), role)
            self.assertTrue(self.called("addr del"), role)

    def test_a_node_without_the_role_has_nothing_to_give_up(self):
        self.assertFalse(self.reconcile("standby", False))
        self.assertFalse(self.called("addr del"))
        self.assertFalse(self.called("addr add"))

    def test_the_peer_probe_replaces_curl_where_curl_is_missing(self):
        self.env(ENV_HA.format(role="primary", node="a"))
        self.set_rules([{"match": ["ha_cli.py", " probe "], "out": '{"role":"leader"}\n'}])
        os.symlink("/usr/bin/true", self.tmp / "bin" / "notcurl")
        (self.tmp / "bin" / "curl").unlink()
        r = self.run_body("ha_have_curl() { return 1; }; ha_load; ha_peer_health")   # this machine may have a real curl
        self.assertIn('"role":"leader"', r.stdout)
        self.assertTrue(self.called("ha_cli.py probe 192.168.1.20 443"))


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
        self.assertEqual(self.state_value("HA_VIP"), "192.168.1.22/24")  # the VIP is state, never taken from a sync
        self.assertEqual(self.env_value("HA_ROLE"), "standby")
        self.assertEqual(self.state_value("HA_NODE"), "b")
        self.assertEqual(self.state_value("HA_NODE_IP"), "192.168.1.21")
        self.assertEqual(self.env_value("COMPOSE_FILE"), "docker-compose.yml:docker-compose.ha.yml")
        self.assertEqual(self.env_value("POSTGRES_PASSWORD"), "dbpw")
        self.assertEqual(self.env_value("OPNEXUS_VERSION"), "1.34.1")
        self.assertEqual(self.env_value("OPNEXUS_SITES"), "192.168.1.20")

    def test_env_sync_accepts_only_the_allowlist(self):
        self.env(ENV_HA.format(role="standby", node="b") + "COMPOSE_PROFILES=\nOPNEXUS_IMAGE_REPO=registry.example/opnexus\n")
        payload = "\n".join(["COMPOSE_PROFILES=updater", "OPNEXUS_IMAGE_REPO=evil.example/x", "PROXY_HTTPS_PORT=1", "BIND_ADDRESS=0.0.0.0",
                             "OPNEXUS_RUN_AS_ROOT=1", "UPDATER_URL=http://evil", "COMPOSE_PROJECT_NAME=x", "COOKIE_SECURE=true", "UPDATE_CHECK_URL=https://feed.example/latest.json"]) + "\n"
        r = self.run_body("cmd_ha receive env", stdin=payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("COOKIE_SECURE"), "true")
        self.assertEqual(self.env_value("UPDATE_CHECK_URL"), "https://feed.example/latest.json")
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "")
        self.assertEqual(self.env_value("OPNEXUS_IMAGE_REPO"), "registry.example/opnexus")
        for key in ("PROXY_HTTPS_PORT", "BIND_ADDRESS", "OPNEXUS_RUN_AS_ROOT", "UPDATER_URL", "COMPOSE_PROJECT_NAME"):
            self.assertIsNone(self.env_value(key), key)

    def test_env_init_may_set_the_node_keys_but_only_known_ones(self):
        self.env(ENV_BASE)
        r = self.run_body("cmd_ha receive env-init", stdin="HA_ROLE=standby\nHA_NODE=b\nCOMPOSE_FILE=a:b\nPATH=/evil\nADMIN_PASSWORD=x\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("HA_ROLE"), "standby")
        self.assertEqual(self.env_value("COMPOSE_FILE"), "a:b")
        self.assertIsNone(self.env_value("PATH"))
        self.assertIsNone(self.env_value("ADMIN_PASSWORD"))

    def test_sync_lines_leave_out_what_belongs_to_one_node(self):
        self.env(ENV_HA.format(role="primary", node="a") + "GUI_GATEWAY_TOKEN=tok\nBIND_ADDRESS=1.2.3.4\nCOMPOSE_PROFILES=updater\nOPNEXUS_IMAGE_REPO=r\nPROXY_HTTPS_PORT=8443\nCOOKIE_SECURE=true\n")
        r = self.run_body("ha_sync_lines")
        keys = {ln.split("=", 1)[0] for ln in r.stdout.splitlines()}
        self.assertIn("GUI_GATEWAY_TOKEN", keys)
        for private in ("HA_VIP", "HA_REPLICATION_PASSWORD", "HA_ROLE", "HA_NODE", "HA_NODE_IP", "HA_PEER", "HA_PEER_IP", "HA_VIP_DEV", "COMPOSE_FILE", "POSTGRES_PASSWORD",
                        "OPNEXUS_VERSION", "OPNEXUS_SITES", "FRONTEND_ORIGIN", "BIND_ADDRESS", "COMPOSE_PROFILES", "OPNEXUS_IMAGE_REPO", "PROXY_HTTPS_PORT"):
            self.assertNotIn(private, keys)
        self.assertIn("COOKIE_SECURE", keys)

    def test_a_synced_ca_makes_a_running_proxy_reload_and_a_stopped_one_is_left_alone(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        good = self.tmp / "ca.tar"
        import io, tarfile
        with tarfile.open(good, "w") as tar:
            info = tarfile.TarInfo("local/root.crt"); info.size = 2; tar.addfile(info, io.BytesIO(b"x\n"))
        self.set_rules([{"match": ["ps -q proxy"], "out": "abc123\n"}])
        self.assertEqual(self.run_body(f"cmd_ha receive ca {'a' * 64} < '{good}'").returncode, 0)
        self.assertTrue(self.called("restart proxy"))
        self.log.unlink()
        self.set_rules([{"match": ["ps -q proxy"], "out": ""}])
        self.assertEqual(self.run_body(f"cmd_ha receive ca {'a' * 64} < '{good}'").returncode, 0)
        self.assertFalse(self.called("restart proxy"))

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

    def test_update_standby_pulls_verifies_and_restarts_backend_frontend_and_proxy(self):
        self.env(ENV_HA.format(role="standby", node="b"))
        r = self.run_body('verify_signatures() { echo "VERIFY $*"; }; refresh_cli() { echo "REFRESH-CLI ${REFRESH_ARGS[*]}"; }; '
                          'refresh_compose() { :; }; cmd_ha_update_standby 1.35.0')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("VERIFY 1.35.0 backend frontend proxy", r.stdout)
        self.assertIn("REFRESH-CLI ha update-standby 1.35.0", r.stdout)
        up = next(line for line in self.calls() if "up -d --no-build" in line)
        self.assertTrue(up.endswith("backend frontend proxy"), up)   # the standby serves the /standby page: same version as its backend
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

    def test_a_vip_that_answers_in_the_network_is_refused_before_anything_changes(self):
        for extra in ({"FAKE_PING_RC": "0"}, {"FAKE_NEIGH": "1"}):
            r = self.init(extra_env=extra)
            self.assertNotEqual(r.returncode, 0, extra)
            self.assertIn("antwortet im Netz schon jemand", r.stderr)
            self.assertFalse(self.called("ssh "), extra)
            self.assertFalse(self.called("up -d"), extra)

    def test_a_stale_neighbour_entry_is_a_leftover_not_an_answer(self):
        # Lab 2026-11-03: the address had lain on another node a moment earlier; its ARP entry (STALE) blocked the setup although nobody answered
        self.env(ENV_BASE)
        probe = 'ha_vip_in_use 192.168.1.22 eth0 && echo used || echo free'
        for state in ("STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE"):
            self.assertEqual(self.run_body(probe, extra_env={"FAKE_NEIGH": state}).stdout.strip(), "free", state)
        self.assertEqual(self.run_body(probe, extra_env={"FAKE_NEIGH": "1"}).stdout.strip(), "used")           # REACHABLE: somebody answered the ARP request
        self.assertEqual(self.run_body(probe, extra_env={"FAKE_PING_RC": "0"}).stdout.strip(), "used")        # a ping reply
        self.assertEqual(self.run_body(probe).stdout.strip(), "free")

    def test_the_vip_on_this_node_itself_is_no_conflict_for_a_repeated_init(self):
        (self.tmp / "vip").touch()
        r = self.init(extra_env={"FAKE_PING_RC": "0"})
        self.assertNotIn("antwortet im Netz schon jemand", r.stderr)

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
        self.assertEqual(self.state_value("HA_NODE"), "a")
        self.assertEqual(self.state_value("HA_NODE_IP"), "192.168.1.20")
        self.assertEqual(self.state_value("HA_PEER"), "root@192.168.1.21")
        self.assertEqual(self.state_value("HA_VIP"), "192.168.1.22/24")
        self.assertEqual(self.state_value("HA_VIP_DEV"), "eth0")
        self.assertEqual(self.env_value("COMPOSE_FILE"), "docker-compose.yml:docker-compose.ha.yml")
        self.assertRegex(self.state_value("HA_REPLICATION_PASSWORD") or "", r"^[A-Za-z0-9]{40}$")
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
        self.assertEqual(self.state_value("HA_PEER_PORT"), "9443")  # we ask the partner's proxy there
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
        for expected in ("HA_ROLE=standby", "HA_NODE=b", "HA_NODE_IP=192.168.1.21", "HA_PEER=" + self.whoami + "@192.168.1.20", "HA_PEER_IP=192.168.1.20",
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


class MigrateAgent(Harness):
    """U5: an existing SSH pair is switched to the agent over the SSH connection it already trusts."""
    SECRET = "5e" * 32
    PIN_HERE = "ab" * 32
    PIN_THERE = "cd" * 32

    def setUp(self):
        super().setUp()
        _exe(self.tmp / "bin" / "python", "#!/bin/sh\necho \"python $*\" >> \"$FAKE_LOG\"\nexit 0\n")
        self.env(ENV_HA.format(role="primary", node="a") + "COMPOSE_PROFILES=updater\n")
        (self.node / "docker-compose.ha.yml").write_text("services: {}\n")
        (self.tmp / "cron-opnexus-ha").write_text("*/5 * * * * root true\n")
        self.set_rules(self.docker_rules(), ssh=self.ssh_rules_ok())

    def docker_rules(self, ping_rc=0, gate=True):
        return [
            # the gate comes first and a long tail follows: `grep -q` in a pipe would quit early and pipefail would turn the SIGPIPE into a refusal (lab 2026-11-03)
            {"match": ["config", "--services"], "out": ("ha-gate\n" if gate else "") + "backend\nupdater\n" + "padding\n" * 300000},
            {"match": ["ha_cli.py", " newsecret"], "out": self.SECRET + "\n"},
            {"match": ["ha_cli.py", " gate "], "out": f"pin={self.PIN_HERE}\nport=8445\n"},
            {"match": ["ha_cli.py", " call ping"], "rc": ping_rc},
        ]

    def ssh_rules_ok(self, version="1.34.1"):
        return [
            {"match": ["status", "--porcelain"], "out": f"running_version={version}\n"},
            {"match": ["migrate-agent", "--partner-side", "--ip"], "out": f"pin={self.PIN_THERE}\n"},
        ]

    def migrate(self, args="--yes", **kw):
        return self.run_body("running_version() { echo 1.34.1; }; cmd_ha_migrate_agent " + args, **kw)

    def test_the_pair_is_switched_and_the_secret_travels_only_through_stdin(self):
        r = self.migrate()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = self.calls()
        remote = next(l for l in lines if l.startswith("ssh ") and "--partner-side --ip" in l)
        self.assertIn(f"--peer-pin {self.PIN_HERE}", remote)
        self.assertIn(f"<<< {self.SECRET}", remote)                               # stdin of the SSH call
        for line in lines:
            self.assertNotIn(self.SECRET, line.split("<<<")[0], f"the secret appears in a command line: {line}")   # never in an argument
        adopt = next(l for l in lines if " adopt " in l)
        self.assertIn(f"--ip 192.168.1.20 --port 8445 --pin {self.PIN_THERE}", adopt)
        self.assertIn(f"<<< {self.SECRET}", adopt)
        self.assertLess(self.index("call ping"), self.index("cron remove"))       # the cron only goes once the agent answers
        self.assertFalse((self.tmp / "cron-opnexus-ha").exists())
        self.assertTrue(self.called("ha-gate"))

    def test_roles_and_state_stay_untouched(self):
        before = (self.node / ".opnexus-ha" / "state").read_text()
        self.assertEqual(self.migrate().returncode, 0)
        self.assertEqual((self.node / ".opnexus-ha" / "state").read_text(), before)

    def test_a_failing_partner_leaves_the_ssh_pair_as_it_was(self):
        self.set_rules(self.docker_rules(), ssh=[
            {"match": ["status", "--porcelain"], "out": "running_version=1.34.1\n"},
            {"match": ["migrate-agent", "--partner-side", "--ip"], "rc": 1},
        ])
        r = self.migrate()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called(" adopt "))
        self.assertTrue(self.called("--partner-side --abort"))
        self.assertTrue((self.tmp / "cron-opnexus-ha").exists())

    def test_a_partner_without_a_certificate_is_rolled_back(self):
        self.set_rules(self.docker_rules(), ssh=[
            {"match": ["status", "--porcelain"], "out": "running_version=1.34.1\n"},
            {"match": ["migrate-agent", "--partner-side", "--ip"], "out": "pin=nothex\n"},
        ])
        r = self.migrate()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called(" adopt "))
        self.assertTrue(self.called("--partner-side --abort"))

    def test_a_local_adopt_that_fails_takes_the_already_switched_partner_back(self):
        """The partner is switched first; if this node then cannot record the pairing, the partner must not stay on the agent alone."""
        self.set_rules([{"match": ["ha_cli.py", " adopt "], "rc": 1}] + self.docker_rules(), ssh=self.ssh_rules_ok())
        r = self.migrate()
        self.assertNotEqual(r.returncode, 0)
        remote_adopt = self.index("--partner-side --ip")
        self.assertGreater(self.index("--partner-side --abort", remote_adopt), remote_adopt)
        self.assertFalse(self.called("call ping"))
        self.assertTrue((self.tmp / "cron-opnexus-ha").exists())
        self.assertIn("SSH-Kopplung gilt weiter", r.stdout + r.stderr)

    def test_a_silent_gate_undoes_both_sides_and_keeps_the_cron(self):
        self.set_rules(self.docker_rules(ping_rc=4), ssh=self.ssh_rules_ok())
        r = self.migrate()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.called("ha_cli.py unpair"))
        self.assertTrue(self.called("--partner-side --abort"))
        self.assertTrue((self.tmp / "cron-opnexus-ha").exists())
        self.assertIn("SSH-Kopplung gilt weiter", r.stdout + r.stderr)

    def test_refusals_change_nothing(self):
        cases = {
            "other version": dict(ssh=self.ssh_rules_ok(version="1.35.0"), docker=self.docker_rules()),
            "partner without ssh": dict(ssh=[{"match": [""], "rc": 255}], docker=self.docker_rules()),
            "no gate in this release": dict(ssh=self.ssh_rules_ok(), docker=self.docker_rules(gate=False)),
        }
        for name, c in cases.items():
            self.log.unlink(missing_ok=True)
            self.set_rules(c["docker"], ssh=c["ssh"])
            r = self.migrate()
            self.assertNotEqual(r.returncode, 0, name)
            self.assertFalse(self.called(" adopt "), name)
            self.assertFalse(self.called(" newsecret"), name)
            self.assertTrue((self.tmp / "cron-opnexus-ha").exists(), name)

    def test_a_pair_that_already_runs_on_the_agent_is_refused(self):
        (self.node / ".opnexus-ha" / "agent").mkdir(parents=True, exist_ok=True)
        (self.node / ".opnexus-ha" / "agent" / "cluster.json").write_text("{}")
        r = self.migrate()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schon über den Agenten", r.stdout + r.stderr)

    def test_without_yes_a_no_answer_stops_it(self):
        r = self.migrate(args="", stdin="n\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.called(" newsecret"))

    # -- the partner's side ----------------------------------------------------------------
    def partner(self, args, stdin):
        return self.run_body("cmd_ha_migrate_agent --partner-side " + args, stdin=stdin)

    def test_the_partner_side_reads_the_secret_first_and_hands_it_to_adopt(self):
        r = self.partner(f"--ip 192.168.1.20 --peer-ip 192.168.1.21 --peer-pin {self.PIN_HERE}", self.SECRET + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"pin={self.PIN_HERE}", r.stdout)
        adopt = next(l for l in self.calls() if " adopt " in l)
        self.assertIn(f"<<< {self.SECRET}", adopt)
        self.assertNotIn(self.SECRET, adopt.split("<<<")[0])

    def test_the_partner_side_validates_what_lands_in_commands(self):
        for args in ("--ip 1.2.3.4 --peer-ip 1.2.3.5 --peer-pin xyz", "--ip 1.2.3.4;id --peer-ip 1.2.3.5 --peer-pin " + "ab" * 32,
                     "--ip 1.2.3.4 --peer-ip x --peer-pin " + "ab" * 32):
            self.assertNotEqual(self.partner(args, self.SECRET + "\n").returncode, 0, args)
        self.assertFalse(self.called(" adopt "))

    def test_the_partner_side_refuses_when_it_is_paired_and_abort_unpairs(self):
        (self.node / ".opnexus-ha" / "agent").mkdir(parents=True, exist_ok=True)
        (self.node / ".opnexus-ha" / "agent" / "cluster.json").write_text("{}")
        self.assertNotEqual(self.partner(f"--ip 1.2.3.4 --peer-ip 1.2.3.5 --peer-pin {self.PIN_HERE}", self.SECRET + "\n").returncode, 0)
        self.assertEqual(self.partner("--abort", "").returncode, 0)
        self.assertTrue(self.called("ha_cli.py unpair"))


if __name__ == "__main__":
    unittest.main()
