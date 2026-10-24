#!/usr/bin/env bash
# Baut das Release-Bundle (dist/opnexus-<version>.tar.gz), mit dem man OPNexus
# ohne Repo-Checkout betreibt: docker-compose.yml (nur Images, Registry und
# Version als Standard eingebacken), .env.example, das opnexus-Update-Skript
# und eine Kurzanleitung.
#
#   scripts/make_release_bundle.sh 1.10.0 git.opnexus.dev/opnexus
#
# Das Ergebnis wird manuell an die Release-Seite angehängt (die CI lädt es
# bewusst nicht hoch: GitHub und Forgejo haben dafür unterschiedliche APIs).
set -euo pipefail
cd "$(dirname "$0")/.."

version="${1:?Usage: $0 VERSION IMAGE_REPO}"
repo="${2:?Usage: $0 VERSION IMAGE_REPO}"
name="opnexus-$version"
out="dist/$name"

rm -rf "$out" "dist/$name.tar.gz"
mkdir -p "$out"

# Standardwerte in der Compose-Datei durch die Release-Werte ersetzen (dieselbe Datei steckt im Updater-Image).
scripts/release_compose.sh "$version" "$repo" > "$out/docker-compose.yml"

# .env.example mit gesetzter Registry und Version (der Updater liest .env).
sed -e "s|^# OPNEXUS_IMAGE_REPO=.*|OPNEXUS_IMAGE_REPO=$repo|" \
    -e "s|^# OPNEXUS_VERSION=.*|OPNEXUS_VERSION=$version|" \
    .env.example > "$out/.env.example"

cp scripts/opnexus "$out/opnexus"
cp LICENSE "$out/"
cp cosign.pub "$out/"
cat > "$out/INSTALL.txt" <<TXT
OPNexus $version

1. install -m 600 .env.example .env     und POSTGRES_PASSWORD (und was sonst nötig ist) eintragen
2. docker compose pull && docker compose up -d --no-build
3. Anmelden:  ./opnexus password          (zeigt das Admin-Passwort, wenn ADMIN_PASSWORD leer war)
              dann im Browser öffnen — der Einrichtungsassistent führt durch den Rest
4. Updates:   ./opnexus update            (neueste Version der in .env gewählten Spur)
              ./opnexus update 1.10.1     (feste Version)
   Zurück:    ./opnexus rollback
   Sichern:   ./opnexus backup

Optional: Updates per Klick im Webinterface (Einstellungen › "OPNexus aktualisieren"):
   in .env  COMPOSE_PROFILES=updater  und UPDATE_CHECK_URL setzen, dann
   docker compose up -d --no-build     (Hinweise zur Sicherheit: Handbuch)

Vollständige Anleitung: README.md im Repository, Abschnitt "Release-Images & CI".
TXT

tar -C dist -czf "dist/$name.tar.gz" "$name"
echo "dist/$name.tar.gz"
