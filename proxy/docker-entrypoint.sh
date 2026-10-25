#!/bin/sh
# Caddy liest die Adressliste aus OPNEXUS_SITES / OPNEXUS_GUI_SITES als Text in der Caddyfile. Ein Komma ohne Leerzeichen
# danach ("a,b") ist dort keine Trennung: Caddy lehnt die Adresse ab, und der Proxy startet in einer Schleife neu (HA-Lab
# 2026-10-25, B20). Hier wird jedes Komma zu ", " (und doppelte Leerzeichen entfallen), bevor Caddy startet; eine Liste, die
# schon "a, b" lautet, bleibt unverändert.
set -eu
for var in OPNEXUS_SITES OPNEXUS_GUI_SITES; do
  eval "value=\${$var:-}"
  if [ -n "$value" ]; then
    # Eintrag je Zeile, Leerraum außen weg, leere Einträge (",,", führendes oder letztes Komma) weg, mit ", " wieder verbinden.
    value=$(printf '%s' "$value" | tr ',' '\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^$' | paste -sd, - | sed 's/,/, /g' || true)
    export "$var=$value"
  fi
done
exec "$@"
