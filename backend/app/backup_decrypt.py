"""Decrypt an offsite backup (``.opxbak``) back into a config.xml.

    docker compose exec -T backend python -m app.backup_decrypt < file.opxbak > config.xml

The passphrase comes from the BACKUP_PASSPHRASE environment variable or, if that is
not set, from the passphrase stored in OPNexus. For a disaster-recovery machine
without OPNexus run it with the passphrase you kept elsewhere:

    docker run --rm -i -e BACKUP_PASSPHRASE=... <backend image> python -m app.backup_decrypt < file.opxbak > config.xml
"""
import os
import sys

from app.services import backup_crypto, notification_secret_store


def main() -> int:
    passphrase = os.environ.get("BACKUP_PASSPHRASE") or notification_secret_store.read_secret("backup_passphrase")
    if not passphrase:
        print("Keine Passphrase: BACKUP_PASSPHRASE setzen.", file=sys.stderr)
        return 2
    try:
        sys.stdout.buffer.write(backup_crypto.decrypt(sys.stdin.buffer.read(), passphrase))
    except backup_crypto.DecryptError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
