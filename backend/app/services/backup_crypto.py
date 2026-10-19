"""Client-side encryption of config backups before they leave the server.

File format (``.opxbak``): ``OPXBAK1`` | salt (16) | nonce (12) | AES-256-GCM ciphertext+tag.
The key is scrypt(passphrase, salt). The header is authenticated as associated data, so a
file cannot be re-labelled. The passphrase never leaves OPNexus' secret store; without it
an offsite copy is unreadable, which is the point — and also why it must be kept somewhere
else too (see the handbook). Decrypt with ``python -m app.backup_decrypt``.
"""
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"OPXBAK1"
_SALT, _NONCE = 16, 12
# scrypt cost: ~64 MiB, well above interactive defaults; a backup is encrypted rarely.
_N, _R, _P = 2**16, 8, 1


class DecryptError(Exception):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=_N, r=_R, p=_P).derive(passphrase.encode())


def encrypt(plaintext: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(_SALT), os.urandom(_NONCE)
    header = MAGIC + salt + nonce
    return header + AESGCM(_key(passphrase, salt)).encrypt(nonce, plaintext, header)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    header_len = len(MAGIC) + _SALT + _NONCE
    if len(blob) < header_len + 16 or not blob.startswith(MAGIC):
        raise DecryptError("Keine OPNexus-Backup-Datei (Kopfzeile fehlt).")
    header, body = blob[:header_len], blob[header_len:]
    salt = header[len(MAGIC): len(MAGIC) + _SALT]
    nonce = header[len(MAGIC) + _SALT:]
    try:
        return AESGCM(_key(passphrase, salt)).decrypt(nonce, body, header)
    except InvalidTag:
        raise DecryptError("Falsche Passphrase oder beschädigte Datei.") from None
