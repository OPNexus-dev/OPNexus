"""ensure_host_socket / cmd_updater in `opnexus`: Rootless-Docker-Socket automatisch erkennen und
DOCKER_HOST_SOCKET in die .env eintragen — ein bestehender Wert bleibt verbindlich, ohne Fund
ändert sich nichts. Läuft mit nachgebautem `docker` vor PATH (echte Socket-Dateien, gefälschte
Context-Endpunkte).
Run: cd updater && python3 -m unittest test_cli_socket"""
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("opnexus").resolve()

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1" in
  context) echo "$FAKE_ENDPOINT" ;;
  compose) exit 0 ;;
  *) exit 1 ;;
esac
"""


def _exe(path: Path, text: str):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _sock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.close()
    return path


def _truncated_script(tmp: Path) -> Path:
    """Das Skript bis zur Haupt-Verzweigung: Funktionen ja, Kommando-Ausführung nein."""
    text = SCRIPT.read_text().splitlines(True)
    for i, line in enumerate(text):
        if line.startswith('case "${1:-}" in'):
            out = tmp / "script.sh"
            out.write_text("".join(text[:i]))
            return out
    raise AssertionError("Haupt-Verzweigung nicht gefunden")


class EnsureHostSocket(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        _exe(self.tmp / "bin" / "docker", FAKE_DOCKER)
        self.envfile = self.tmp / ".env"
        self.envfile.write_text("OPNEXUS_IMAGE_REPO=registry.example/opnexus\n")
        self.log = self.tmp / "calls.log"
        self.script = _truncated_script(self.tmp)

    def env_value(self, key):
        for line in self.envfile.read_text().splitlines():
            if line.startswith(key + "="):
                return line[len(key) + 1:]
        return None

    def run_body(self, body, **extra):
        env = {**os.environ, "PATH": f"{self.tmp}/bin:/usr/bin:/bin", "OPNEXUS_DIR": str(self.tmp),
               "FAKE_LOG": str(self.log), "FAKE_ENDPOINT": "unix:///nirgendwo/docker.sock",
               "DOCKER_HOST": "", "XDG_RUNTIME_DIR": str(self.tmp / "run"), "TMPDIR": str(self.tmp)}
        env.update(extra)
        return subprocess.run(["bash", "-c", f'source "{self.script}"; {body}', str(SCRIPT)],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_vorhandener_wert_ist_verbindlich(self):
        self.envfile.write_text("DOCKER_HOST_SOCKET=/eigener/pfad/docker.sock\n")
        r = self.run_body("ensure_host_socket")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("DOCKER_HOST_SOCKET"), "/eigener/pfad/docker.sock")
        self.assertFalse(self.log.exists())  # nicht einmal nachsehen: der Wert gilt.

    def test_im_updater_container_laueft_nichts(self):
        r = self.run_body("ensure_host_socket", OPNEXUS_UPDATER="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.env_value("DOCKER_HOST_SOCKET"))
        self.assertFalse(self.log.exists())

    def test_rootless_socket_wird_erkannt_und_eingetragen(self):
        sock = _sock_file(self.tmp / "run" / "docker.sock")
        r = self.run_body("ensure_host_socket")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("DOCKER_HOST_SOCKET"), str(sock))
        self.assertIn("DOCKER_HOST_SOCKET", r.stdout)

    def test_kontext_endpunkt_wird_uebernommen(self):
        sock = _sock_file(self.tmp / "kontext" / "docker.sock")
        r = self.run_body("ensure_host_socket", FAKE_ENDPOINT=f"unix://{sock}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("DOCKER_HOST_SOCKET"), str(sock))

    def test_docker_host_hat_vorrang_vor_dem_kontext(self):
        sock = _sock_file(self.tmp / "dhost" / "docker.sock")
        other = _sock_file(self.tmp / "run" / "docker.sock")
        r = self.run_body("ensure_host_socket", DOCKER_HOST=f"unix://{sock}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("DOCKER_HOST_SOCKET"), str(sock))
        self.assertNotEqual(self.env_value("DOCKER_HOST_SOCKET"), str(other))

    def test_ohne_fund_bleibt_alles_bei_compose_standard(self):
        r = self.run_body("ensure_host_socket")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.env_value("DOCKER_HOST_SOCKET"))

    def test_enable_erkennt_socket_und_aktiviert_das_profil(self):
        sock = _sock_file(self.tmp / "run" / "docker.sock")
        r = self.run_body("cmd_updater enable")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "updater")
        self.assertEqual(self.env_value("DOCKER_HOST_SOCKET"), str(sock))
        self.assertIn("compose up -d --no-build updater", self.log.read_text())

    def test_disable_entfernt_profil_und_container(self):
        self.envfile.write_text("OPNEXUS_IMAGE_REPO=registry.example/opnexus\nCOMPOSE_PROFILES=updater,gui\n")
        r = self.run_body("cmd_updater disable")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env_value("COMPOSE_PROFILES"), "gui")
        self.assertIn("compose rm -sf updater", self.log.read_text())

    def test_ohne_argument_gibt_es_nur_den_hinweis(self):
        r = self.run_body("cmd_updater")
        self.assertEqual(r.returncode, 1)
        self.assertIn("enable|disable", r.stderr)


if __name__ == "__main__":
    unittest.main()
