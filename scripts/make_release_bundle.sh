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

1. install -m 600 .env.example .env      (file readable only by you)
   sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=\$(openssl rand -hex 24)|" .env
                                         (generates your own database password; on macOS/Windows open the .env in an
                                          editor and enter POSTGRES_PASSWORD by hand)
2. docker compose up -d                  (pulls the images itself the first time)
3. Log in:    ./opnexus password          (shows the admin password if ADMIN_PASSWORD was empty)
              then open https://<server-ip> in the browser (locally https://localhost) — the setup assistant guides you through the rest
4. Updates:   ./opnexus update            (newest version of the track chosen in .env)
              ./opnexus update 1.10.1     (fixed version)
   Back:      ./opnexus rollback
   Backup:    ./opnexus backup

Important: POSTGRES_PASSWORD applies only on the very first start with an empty database volume. If the backend then restarts in a
loop ("password authentication failed"), the volume comes from an earlier attempt: put the original password back or, without data,
run "docker compose down -v" (deletes the database). The Compose project name is always "opnexus", no matter which folder; for a
second instance set COMPOSE_PROJECT_NAME in the .env.

Optional: one-click updates in the web interface (Settings › "Update OPNexus"):
   set COMPOSE_PROFILES=updater and UPDATE_CHECK_URL in .env, then
   docker compose up -d     (security notes: handbook)

Full guide: handbook (docs/HANDBOOK.md, German: docs/HANDBUCH.md), section "Getting started", and https://opnexus.dev.
TXT

# Reproduzierbar: gleiche Eingaben ergeben dasselbe Archiv (feste Reihenfolge, Besitzer und Zeit = Commit des Tags, gzip ohne Zeitstempel), damit die
# Release-Pipeline ein schon veröffentlichtes Bundle per Prüfsumme als identisch erkennen kann statt es zu ersetzen.
epoch="$(git log -1 --format=%ct "v$version" 2>/dev/null || date +%s)"
find "$out" -exec touch -h -d "@$epoch" {} +
tar -C dist --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" -cf - "$name" | gzip -n -9 > "dist/$name.tar.gz"
echo "dist/$name.tar.gz"
