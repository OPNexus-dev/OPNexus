"""The ``wg-hub`` container: WireGuard interface + a CONNECT proxy for the management overlay.

Runs from the backend image (``python -m app.overlay_hub``) with CAP_NET_ADMIN and the WireGuard UDP port
published. Everything it needs comes from ``<overlay_dir>/hub.json`` (written by the backend, see
services/overlay_service.py); it reports back through ``status.json``. It never talks to the database.

* WireGuard: wg0 with the hub address, peers synced with ``wg syncconf`` whenever hub.json changes. Inbound
  NEW connections from the tunnel are dropped (iptables), so a compromised firewall cannot reach anything on this
  container; OPNexus' own requests to the firewalls and their replies are unaffected.
* Proxy: an HTTP CONNECT proxy (port 8888, compose network only). It is not an open proxy: it only connects to
  IPv4 addresses inside the overlay subnet from hub.json.
"""
import asyncio
import ipaddress
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger("overlay_hub")
INTERFACE = "wg0"
PROXY_PORT = 8888
APPLY_EVERY = 5


def run(*cmd: str, stdin: str | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(list(cmd), input=stdin, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def wg_conf(config: dict) -> str:
    lines = ["[Interface]", f"PrivateKey = {config['private_key']}", f"ListenPort = {int(config['listen_port'])}"]
    for peer in config.get("peers", []):
        lines += ["", "[Peer]", f"PublicKey = {peer['public_key']}", f"PresharedKey = {peer['psk']}", f"AllowedIPs = {peer['ip']}/32"]
    return "\n".join(lines) + "\n"


def parse_dump(text: str) -> list[dict]:
    """`wg show <if> dump`: first line is the interface, then one tab-separated line per peer."""
    peers = []
    for line in text.splitlines()[1:]:
        f = line.split("\t")
        if len(f) >= 7:
            peers.append({"public_key": f[0], "endpoint": None if f[2] == "(none)" else f[2], "handshake_at": int(f[4] or 0),
                          "rx": int(f[5] or 0), "tx": int(f[6] or 0)})
    return peers


class Hub:
    def __init__(self, directory: Path, runner=run):
        self.dir, self.run = directory, runner
        self.subnet: ipaddress.IPv4Network | None = None
        self._applied_mtime = 0.0

    def read_config(self) -> dict | None:
        try:
            config = json.loads((self.dir / "hub.json").read_text())
            return config if isinstance(config, dict) else None
        except (OSError, ValueError):
            return None

    def apply(self, config: dict) -> str | None:
        """Bring the interface in line with the config. Returns an error text or None."""
        self.subnet = ipaddress.ip_network(config["subnet"]) if config.get("enabled") and config.get("subnet") else None
        exists = self.run("ip", "link", "show", INTERFACE)[0] == 0
        if not config.get("enabled"):
            if exists:
                self.run("ip", "link", "del", INTERFACE)
            return None
        steps = []
        if not exists:
            steps.append(("ip", "link", "add", INTERFACE, "type", "wireguard"))
        steps += [("ip", "address", "replace", config["address"], "dev", INTERFACE), ("ip", "link", "set", INTERFACE, "up")]
        for step in steps:
            code, out = self.run(*step)
            if code != 0:
                return f"{' '.join(step[:3])}: {out}"[:300]
        conf = self.dir / ".wg0.conf"
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(wg_conf(config))
        try:
            code, out = self.run("wg", "syncconf", INTERFACE, str(conf))
        finally:
            conf.unlink(missing_ok=True)
        if code != 0:
            return f"wg syncconf: {out}"[:300]
        # Nothing may start a connection INTO this container from the tunnel; replies to ours still pass.
        rule = ("INPUT", "-i", INTERFACE, "-m", "conntrack", "--ctstate", "NEW", "-j", "DROP")
        if self.run("iptables", "-C", *rule)[0] != 0:
            code, out = self.run("iptables", "-I", *rule)
            if code != 0:
                return f"iptables: {out}"[:300]
        return None

    def status(self, error: str | None) -> dict:
        code, out = self.run("wg", "show", INTERFACE, "dump")
        return {"updated_at": time.time(), "up": code == 0, "peers": parse_dump(out) if code == 0 else [], "error": error}

    def write_status(self, error: str | None) -> None:
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps(self.status(error)))
        os.replace(tmp, self.dir / "status.json")

    def tick(self, last_error: str | None) -> str | None:
        """One round: re-apply if hub.json changed, then report. Returns the current error."""
        config = self.read_config()
        if config is not None:
            mtime = (self.dir / "hub.json").stat().st_mtime
            if mtime != self._applied_mtime or last_error:
                last_error = self.apply(config)
                self._applied_mtime = mtime
        self.write_status(last_error)
        return last_error

    # ── CONNECT proxy ────────────────────────────────────────────────────
    def allowed(self, host: str) -> bool:
        try:
            return self.subnet is not None and ipaddress.ip_address(host) in self.subnet
        except ValueError:
            return False

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            host, _, port_text = target.rpartition(":")
            if method != "CONNECT" or not port_text.isdigit() or not 0 < int(port_text) < 65536 or not self.allowed(host.strip("[]")):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            try:
                up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, int(port_text)), 8)
            except (OSError, asyncio.TimeoutError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError, OSError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            dst.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    directory = Path(settings.overlay_dir)
    directory.mkdir(parents=True, exist_ok=True)
    hub = Hub(directory)
    server = await asyncio.start_server(hub.handle_client, "0.0.0.0", PROXY_PORT)
    logger.info("Overlay-Hub gestartet (Proxy auf :%d)", PROXY_PORT)
    error = None
    async with server:
        while True:
            try:
                new_error = await asyncio.to_thread(hub.tick, error)
            except Exception:
                logger.exception("Overlay-Hub-Runde fehlgeschlagen")
                new_error = "interner Fehler"
            if new_error and new_error != error:
                logger.warning("Overlay: %s", new_error)
            error = new_error
            await asyncio.sleep(APPLY_EVERY)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
