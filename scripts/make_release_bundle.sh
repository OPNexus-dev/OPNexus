#!/usr/bin/env bash
# Baut das Release-Bundle (dist/opnexus-<version>.tar.gz), mit dem man OPNexus
# ohne Repo-Checkout betreibt: docker-compose.yml (nur Images, Registry und
# Version als Standard eingebacken), .env.example, das opnexus-Update-Skript
# und eine Kurzanleitung.
#
#   scripts/make_release_bundle.sh 1.10.0 ghcr.io/opnexus-dev
#
# IMAGE_REPO ist die Registry, die für neue Installationen als Standard eingebacken wird (release_all.sh: GHCR; die
# Images liegen digestgleich und signiert auch in der Gitea-Registry). Eine vorhandene .env wird nie angefasst — das Bundle
# enthält nur .env.example.
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

1. install -m 600 .env.example .env      (Datei nur für dich lesbar)
   sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=\$(openssl rand -hex 24)|" .env
                                         (eigenes Datenbank-Passwort erzeugen; unter macOS/Windows die .env im Editor
                                          öffnen und POSTGRES_PASSWORD von Hand eintragen)
2. docker compose up -d                  (lädt die Images beim ersten Mal selbst)
3. Anmelden:  ./opnexus password          (zeigt das Admin-Passwort, wenn ADMIN_PASSWORD leer war)
              dann https://<server-ip> im Browser öffnen (lokal https://localhost) — der Einrichtungsassistent führt durch den Rest
4. Updates:   ./opnexus update            (neueste Version der in .env gewählten Spur)
              ./opnexus update 1.10.1     (feste Version)
   Zurück:    ./opnexus rollback
   Sichern:   ./opnexus backup

Wichtig: POSTGRES_PASSWORD gilt nur beim allerersten Start mit leerem Datenbank-Volume. Startet das Backend danach in einer
Schleife neu ("password authentication failed"), stammt das Volume aus einem früheren Versuch: das ursprüngliche Passwort
wieder eintragen oder ohne Daten "docker compose down -v" (löscht die Datenbank). Der Compose-Projektname ist immer
"opnexus", egal in welchem Ordner; für eine zweite Instanz COMPOSE_PROJECT_NAME in der .env setzen.

Optional: Updates per Klick im Webinterface (Einstellungen › "OPNexus aktualisieren"):
   in .env  COMPOSE_PROFILES=updater  und UPDATE_CHECK_URL setzen, dann
   docker compose up -d     (Hinweise zur Sicherheit: Handbuch)

Vollständige Anleitung: Handbuch (docs/HANDBUCH.md), Abschnitt "Erste Schritte", und https://opnexus.dev.
TXT

# Reproduzierbar: gleiche Eingaben ergeben dasselbe Archiv (feste Reihenfolge, Besitzer und Zeit = Commit des Tags, gzip ohne Zeitstempel), damit die
# Release-Pipeline ein schon veröffentlichtes Bundle per Prüfsumme als identisch erkennen kann statt es zu ersetzen.
epoch="$(git log -1 --format=%ct "v$version" 2>/dev/null || date +%s)"
find "$out" -exec touch -h -d "@$epoch" {} +
tar -C dist --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" -cf - "$name" | gzip -n -9 > "dist/$name.tar.gz"
echo "dist/$name.tar.gz"
