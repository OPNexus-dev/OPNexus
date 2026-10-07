"""Per-tunnel WireGuard secret storage: the Server's private key (always
required — see wireguard_deploy_service.py's module docstring for why
OPNsense's model doesn't derive one from a supplied public key or vice
versa) and the Peer's optional pre-shared key (WireGuard's own per-peer
`psk`, unrelated to IPsec's PSK despite the shared name).

Mirrors ipsec_psk_store.py/openvpn_key_store.py's convention exactly: raw
secret material never touches the database as its own column, only disk,
one file per tunnel id per secret kind, mode 0600.
WireguardTunnel.server_private_key_set/peer_psk_set are the DB-side flags
tracking whether each file exists.

Unlike openvpn_key_store.py (one secret kind -> one dedicated module),
this file handles BOTH of this entity's secrets in one place — both are
addressed by the same tunnel id, and the entity has exactly two secret
kinds by construction (never more), so two thin near-duplicate modules
would add nothing except upkeep. Documented as a deliberate deviation
from the one-file-per-secret-kind precedent, not an oversight.

Note (documented, not a new issue introduced here): exactly like
ipsec_deploy_service.py's/openvpn_deploy_service.py's build_payloads(),
both secrets end up embedded in the payloads dict persisted as
WireguardTunnelDeployment.last_payload (a Postgres JSON column), for the
same no-op-redeploy-detection reason — matched here for consistency with
the established pattern rather than diverging from it.
"""
import uuid
from pathlib import Path

from app.config import settings

_PRIVATE_KEY_SUFFIX = ".wgkey"
_PSK_SUFFIX = ".wgpsk"


def _path(tunnel_id: uuid.UUID, suffix: str) -> Path:
    return Path(settings.wireguard_key_secret_dir) / f"{tunnel_id}{suffix}"


def save_private_key(tunnel_id: uuid.UUID, secret: str) -> None:
    path = _path(tunnel_id, _PRIVATE_KEY_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.strip())
    path.chmod(0o600)


def read_private_key(tunnel_id: uuid.UUID) -> str | None:
    path = _path(tunnel_id, _PRIVATE_KEY_SUFFIX)
    if not path.exists():
        return None
    return path.read_text().strip()


def delete_private_key(tunnel_id: uuid.UUID) -> None:
    _path(tunnel_id, _PRIVATE_KEY_SUFFIX).unlink(missing_ok=True)


def save_psk(tunnel_id: uuid.UUID, secret: str) -> None:
    path = _path(tunnel_id, _PSK_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.strip())
    path.chmod(0o600)


def read_psk(tunnel_id: uuid.UUID) -> str | None:
    path = _path(tunnel_id, _PSK_SUFFIX)
    if not path.exists():
        return None
    return path.read_text().strip()


def delete_psk(tunnel_id: uuid.UUID) -> None:
    _path(tunnel_id, _PSK_SUFFIX).unlink(missing_ok=True)
