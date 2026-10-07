"""Per-instance OpenVPN static-key (tls-auth/tls-crypt hardening) material
storage.

Mirrors ipsec_psk_store.py's convention exactly (itself mirroring
opnsense_secret_store.py): the raw key blob never touches the database
as its own column, only disk, one file per instance id, mode 0600.
OpenVpnInstance.tls_key_material_set is the DB-side flag tracking whether
a file exists, same pattern as IpsecTunnel.pre_shared_key_set.

Note (documented, not a new issue introduced here): exactly like
ipsec_deploy_service.py's build_payloads(), the StaticKey payload built
for deploy embeds this same raw key text under payloads["statickey"]["key"]
so no-op-redeploy detection can compare it — and that whole payloads dict
is what gets persisted as OpenVpnInstanceDeployment.last_payload (a
Postgres JSON column). So the key material DOES end up in Postgres via
last_payload, same pre-existing wrinkle already present for IPsec's PSK
(see IpsecTunnelDeployment.last_payload) — matched here for consistency
with the established pattern rather than diverging from it.
"""
import uuid
from pathlib import Path

from app.config import settings

_SUFFIX = ".ovpnkey"


def _path(instance_id: uuid.UUID) -> Path:
    return Path(settings.openvpn_key_secret_dir) / f"{instance_id}{_SUFFIX}"


def save_secret(instance_id: uuid.UUID, secret: str) -> None:
    path = _path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.strip())
    path.chmod(0o600)


def read_secret(instance_id: uuid.UUID) -> str | None:
    path = _path(instance_id)
    if not path.exists():
        return None
    return path.read_text().strip()


def delete_secret(instance_id: uuid.UUID) -> None:
    _path(instance_id).unlink(missing_ok=True)
