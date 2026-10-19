"""Management overlay: keys, addressing, the hub's config file and the live status.

How it fits together
--------------------
* The ``wg-hub`` container (app/overlay_hub.py, same image as the backend, NET_ADMIN, UDP port published)
  owns the WireGuard interface. It reads ``<overlay_dir>/hub.json`` — written here whenever settings or peers
  change — and writes ``<overlay_dir>/status.json`` (handshakes, traffic) every few seconds.
* The backend itself needs no extra privileges: requests to a firewall whose address lies in the overlay subnet
  go through the hub's small CONNECT proxy (``proxy_for``), which only lets traffic into that subnet.
* The firewall dials out to the hub. Its private key never leaves it; OPNexus only learns its public key. A
  per-peer pre-shared key is generated here and shown to an admin for entering on the firewall.
"""
import base64
import ipaddress
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.overlay import OverlayPeer, OverlaySettings
from app.services import secure_files

SINGLETON_ID = 1
KEEPALIVE = 25
CONNECTED_WITHIN = 180  # seconds since the last handshake
_B64_KEY = re.compile(r"^[A-Za-z0-9+/]{43}=$")

_active_network: ipaddress.IPv4Network | None = None


class OverlayError(Exception):
    """A user-facing problem; ``code`` is stable, ``detail`` is for the log."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def _dir() -> Path:
    return Path(settings.overlay_dir)


# ── keys ─────────────────────────────────────────────────────────────────

def valid_public_key(value: str) -> bool:
    if not _B64_KEY.match(value or ""):
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except ValueError:
        return False


def _raw_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def generate_private_key() -> str:
    key = X25519PrivateKey.generate()
    return _raw_b64(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))


def public_from_private(private_b64: str) -> str:
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
    return _raw_b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def hub_private_key() -> str:
    """The hub's private key, created on first use."""
    path = _dir() / "hub.key"
    if not path.exists():
        secure_files.write_private(path, generate_private_key())
    return path.read_text().strip()


def hub_public_key() -> str:
    return public_from_private(hub_private_key())


def peer_psk(peer_id) -> str:
    path = _dir() / f"psk-{peer_id}"
    if not path.exists():
        secure_files.write_private(path, _raw_b64(secrets.token_bytes(32)))
    return path.read_text().strip()


def delete_peer_secret(peer_id) -> None:
    (_dir() / f"psk-{peer_id}").unlink(missing_ok=True)


# ── addressing ───────────────────────────────────────────────────────────

def parse_subnet(value: str) -> ipaddress.IPv4Network:
    try:
        net = ipaddress.ip_network(value.strip(), strict=True)
    except ValueError:
        raise OverlayError("subnet", "kein gültiges Netz") from None
    if not isinstance(net, ipaddress.IPv4Network) or not net.is_private or not (16 <= net.prefixlen <= 29):
        raise OverlayError("subnet", "ein privates IPv4-Netz zwischen /16 und /29 ist nötig")
    return net


def hub_address(net: ipaddress.IPv4Network) -> ipaddress.IPv4Address:
    return net.network_address + 1


def next_free_ip(net: ipaddress.IPv4Network, taken: set[str]) -> str:
    hub = hub_address(net)
    for candidate in net.hosts():
        if candidate != hub and str(candidate) not in taken:
            return str(candidate)
    raise OverlayError("full", "Im Overlay-Netz ist keine Adresse mehr frei")


def proxy_for(base_url: str) -> str | None:
    """The hub's proxy if this firewall address is inside the overlay subnet, else None (direct)."""
    if _active_network is None:
        return None
    try:
        host = urlsplit(base_url).hostname
        return settings.overlay_proxy_url if host and ipaddress.ip_address(host) in _active_network else None
    except ValueError:
        return None


# ── settings, hub config ─────────────────────────────────────────────────

async def get_settings(db: AsyncSession) -> OverlaySettings:
    row = await db.get(OverlaySettings, SINGLETON_ID)
    if row is None:
        row = OverlaySettings(id=SINGLETON_ID, enabled=False, subnet="10.99.0.0/24", listen_port=51820)
        db.add(row)
        await db.flush()
    return row


def endpoint_with_port(cfg: OverlaySettings) -> str | None:
    if not cfg.endpoint:
        return None
    return cfg.endpoint if re.search(r":\d+$", cfg.endpoint) else f"{cfg.endpoint}:{cfg.listen_port}"


async def sync(db: AsyncSession) -> None:
    """Make hub.json match the database (call after every change and at startup) and refresh the proxy rule."""
    global _active_network
    cfg = await get_settings(db)
    net = parse_subnet(cfg.subnet)
    _active_network = net if cfg.enabled else None
    peers = (await db.scalars(select(OverlayPeer).order_by(OverlayPeer.ip))).all()
    config = {
        "enabled": bool(cfg.enabled), "address": f"{hub_address(net)}/{net.prefixlen}", "subnet": str(net),
        "listen_port": cfg.listen_port,
        "private_key": hub_private_key() if cfg.enabled else None,
        "peers": [{"public_key": p.public_key, "psk": peer_psk(p.id), "ip": p.ip} for p in peers if p.public_key] if cfg.enabled else [],
    }
    _dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = _dir() / "hub.json.tmp"
    secure_files.write_private(tmp, json.dumps(config))
    os.replace(tmp, _dir() / "hub.json")


# ── live status (written by the hub) ─────────────────────────────────────

def read_status() -> dict | None:
    try:
        data = json.loads((_dir() / "status.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def hub_state(status: dict | None, now: float | None = None) -> str:
    """'running' | 'stale' (the hub stopped reporting) | 'absent' (never reported: container not running)"""
    if not status:
        return "absent"
    now = now or datetime.now(timezone.utc).timestamp()
    return "running" if now - float(status.get("updated_at", 0)) < 30 else "stale"


def peer_status(status: dict | None, public_key: str | None, now: float | None = None) -> dict:
    now = now or datetime.now(timezone.utc).timestamp()
    entry = next((p for p in (status or {}).get("peers", []) if p.get("public_key") == public_key), None) if public_key else None
    handshake = int(entry.get("handshake_at", 0)) if entry else 0
    return {
        "connected": bool(handshake and now - handshake < CONNECTED_WITHIN and hub_state(status, now) == "running"),
        "last_handshake_at": datetime.fromtimestamp(handshake, timezone.utc) if handshake else None,
        "rx_bytes": int(entry.get("rx", 0)) if entry else 0, "tx_bytes": int(entry.get("tx", 0)) if entry else 0,
        "remote_endpoint": entry.get("endpoint") if entry else None,
    }


def instructions(cfg: OverlaySettings, peer: OverlayPeer) -> dict:
    net = parse_subnet(cfg.subnet)
    return {
        "hub_public_key": hub_public_key(), "endpoint": endpoint_with_port(cfg),
        "tunnel_address": f"{peer.ip}/32", "allowed_ips": f"{hub_address(net)}/32", "psk": peer_psk(peer.id),
        "keepalive": KEEPALIVE, "hub_ip": str(hub_address(net)), "host_url": f"https://{peer.ip}",
    }
