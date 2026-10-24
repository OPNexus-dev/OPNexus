#!/usr/bin/env bash
# Gibt die Release-Fassung der docker-compose.yml aus: Registry und Version als Standard eingebacken, kein build:.
# Dieselbe Datei kommt ins Release-Bundle und ins Updater-Image (dort holt `opnexus update` sie ab, B18).
#
#   scripts/release_compose.sh VERSION IMAGE_REPO > docker-compose.yml
set -euo pipefail
cd "$(dirname "$0")/.."
version="${1:?Usage: $0 VERSION IMAGE_REPO}"
repo="${2:?Usage: $0 VERSION IMAGE_REPO}"
out=$(sed -e "s|\${OPNEXUS_IMAGE_REPO:-local}|\${OPNEXUS_IMAGE_REPO:-$repo}|g" \
          -e "s|\${OPNEXUS_VERSION:-latest}|\${OPNEXUS_VERSION:-$version}|g" docker-compose.yml)
grep -q "OPNEXUS_IMAGE_REPO:-$repo" <<<"$out" || { echo "Compose-Defaults nicht ersetzt" >&2; exit 1; }
! grep -qE '^\s+build:' <<<"$out" || { echo "build: im Release-Compose" >&2; exit 1; }
printf '%s\n' "$out"
