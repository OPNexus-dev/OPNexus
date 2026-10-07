#!/usr/bin/env bash
# Prüft, dass ein Release-Tag (z. B. v1.9.0) zu den Versionsdateien und dem
# CHANGELOG passt. Aufruf: scripts/check_version.sh v1.9.0
set -euo pipefail
cd "$(dirname "$0")/.."

tag="${1:?Usage: $0 vX.Y.Z}"
want="${tag#v}"
backend=$(sed -nE 's/^APP_VERSION = "([^"]+)"$/\1/p' backend/app/version.py)
frontend=$(sed -nE 's/^export const APP_VERSION = "([^"]+)";$/\1/p' frontend/lib/version.ts)

fail=0
[ "$backend" = "$want" ]  || { echo "backend/app/version.py: $backend, Tag: $want" >&2; fail=1; }
[ "$frontend" = "$want" ] || { echo "frontend/lib/version.ts: $frontend, Tag: $want" >&2; fail=1; }
grep -qF "## [$want]" CHANGELOG.md || { echo "CHANGELOG.md hat keinen Abschnitt '## [$want]'" >&2; fail=1; }
[ "$fail" = 0 ] && echo "Version $want ist konsistent"
exit "$fail"
