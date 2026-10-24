#!/bin/sh
# Startet das Backend als unprivilegierter Benutzer (uid 10001).
#
# Der Container beginnt als root, damit bestehende Installationen ihre Volumes
# (bisher von root angelegt) selbst auf den Benutzer umstellen können: ein Update
# braucht dadurch keinen Handgriff, und ein Rückweg auf eine ältere Version bleibt
# möglich (root liest alles). Danach gibt `setpriv` root und alle Capabilities ab.
#
# OPNEXUS_RUN_AS_ROOT=1 lässt das Backend wie früher als root laufen (Notausgang
# für Sonderfälle, z. B. Bind-Mounts, die sich nicht umstellen lassen).
set -eu

APP_UID=10001
APP_GID=10001

if [ "$(id -u)" = "0" ] && [ "${OPNEXUS_RUN_AS_ROOT:-0}" != "1" ]; then
  # Datenverzeichnisse: alles, was nicht dem App-Benutzer gehört, wird umgestellt.
  # Bei jedem Start, nicht nur einmal: Befehle wie `opnexus restore` laufen als
  # root und hinterlassen root-eigene Dateien.
  for d in \
    "${OPNSENSE_SECRET_DIR:-}" "${OPNSENSE_BACKUP_DIR:-}" "${IPSEC_PSK_SECRET_DIR:-}" \
    "${OPENVPN_KEY_SECRET_DIR:-}" "${WIREGUARD_KEY_SECRET_DIR:-}" "${CERT_KEY_SECRET_DIR:-}" \
    "${ACME_SECRET_DIR:-}" "${TOTP_SECRET_DIR:-}" "${NOTIFICATION_SECRET_DIR:-}" \
    "${OVERLAY_DIR:-}" /backups
  do
    [ -n "$d" ] && [ -d "$d" ] || continue
    find "$d" -xdev \( ! -user "$APP_UID" -o ! -group "$APP_GID" \) \
      -exec chown -h "$APP_UID:$APP_GID" {} + 2>/dev/null || true
  done
  # HOME zeigt sonst noch auf /root (nicht lesbar): asyncpg/libpq suchen dort ~/.postgresql.
  export HOME=/app
  exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups \
    --inh-caps=-all --bounding-set=-all --no-new-privs "$@"
fi

exec "$@"
