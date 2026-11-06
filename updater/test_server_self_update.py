"""Tests für self_update() in updater/server.py — nur Standardbibliothek:

    python3 -m unittest discover -s updater -p 'test_*.py'

`docker` wird durch eine Aufzeichnung der Aufrufe ersetzt. Geprüft wird, dass
der kurzlebige Selbst-Update-Container den Docker-Socket mit dem PFAD AUF DEM
HOST mountet, den der eigene Mount tatsächlich hat (Rootless Docker:
/run/user/<UID>/docker.sock statt /var/run/docker.sock) — und dass der
klassische Pfad bleibt, wenn sich die Quelle nicht ermitteln lässt.
"""
import tempfile
import unittest
from pathlib import Path

import server


class _Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class SelfUpdateTest(unittest.TestCase):
    CURRENT_IMAGE = "reg.test/opnexus/opnexus-updater:1.14.0"
    NEW_IMAGE = "reg.test/opnexus/opnexus-updater:1.15.0"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name) / "state"
        state.mkdir()
        server.STATE_DIR = state
        server.LOG_FILE = state / "job.log"
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        real_project = server.PROJECT_DIR
        server.PROJECT_DIR = self.project
        self.addCleanup(lambda: setattr(server, "PROJECT_DIR", real_project))
        self.calls = []
        self._real_docker = server._docker
        self.addCleanup(lambda: setattr(server, "_docker", self._real_docker))

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_docker(self, socket_result, images=None):
        """socket_result: Quellpfad des Socket-Mounts, "" für leer, None für gescheitertes inspect."""
        def run(*args, timeout=120):
            self.calls.append(list(args))
            if args[0] == "inspect":
                if ".Config.Image" in args[3]:
                    return _Completed(f"{self.CURRENT_IMAGE}|{self.project}")
                if socket_result is None:
                    return _Completed("", returncode=1)
                return _Completed(socket_result)
            if args[0] == "compose":
                return _Completed("\n".join(images or [self.NEW_IMAGE, "reg.test/opnexus/opnexus-backend:1.15.0"]))
            return _Completed()
        return run

    def _run_call(self):
        for args in self.calls:
            if args[:2] == ["run", "-d"]:
                return args
        return None

    def test_rootless_socket_mounted_with_host_path(self):
        server._docker = self._fake_docker("/run/user/1000/docker.sock")
        server.self_update()
        run_args = self._run_call()
        self.assertIsNotNone(run_args, "Hilfscontainer wurde nicht gestartet")
        self.assertIn("-v", run_args)
        self.assertIn(f"/run/user/1000/docker.sock:{server.DOCKER_SOCKET}", run_args)
        self.assertIn(f"{self.project}:{self.project}", run_args)
        self.assertIn("-w", run_args)
        self.assertIn(str(self.project), run_args)

    def test_classic_path_when_socket_mount_has_empty_source(self):
        server._docker = self._fake_docker("")
        server.self_update()
        run_args = self._run_call()
        self.assertIsNotNone(run_args, "Hilfscontainer wurde nicht gestartet")
        self.assertIn(f"{server.DOCKER_SOCKET}:{server.DOCKER_SOCKET}", run_args)

    def test_classic_path_when_socket_inspect_fails(self):
        server._docker = self._fake_docker(None)
        server.self_update()
        run_args = self._run_call()
        self.assertIsNotNone(run_args, "Hilfscontainer wurde nicht gestartet")
        self.assertIn(f"{server.DOCKER_SOCKET}:{server.DOCKER_SOCKET}", run_args)

    def test_no_helper_container_when_image_unchanged(self):
        server._docker = self._fake_docker("/run/user/1000/docker.sock", images=[self.CURRENT_IMAGE])
        server.self_update()
        self.assertIsNone(self._run_call(), "bei unverändertem Image darf kein Hilfscontainer starten")


if __name__ == "__main__":
    unittest.main()
