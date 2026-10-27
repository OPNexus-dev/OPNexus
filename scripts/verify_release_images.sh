#!/usr/bin/env bash
# Prüft eine veröffentlichte Version in einer oder mehreren Registries, ohne etwas zu schreiben (nur Lesezugriff nötig).
#
#   scripts/verify_release_images.sh v1.36.0                    # GHCR und Gitea, alles inklusive schwebender Tags
#   scripts/verify_release_images.sh v1.36.0 --exact-only       # ohne schwebende Tags (Voraussetzung für promote_floating_tags.sh)
#   scripts/verify_release_images.sh v1.36.0 --repo ghcr.io/opnexus-dev     # nur diese Registry(s); --repo darf wiederholt werden
#   scripts/verify_release_images.sh v1.36.0 --no-compose       # ohne das Updater-Image zu ziehen
#
# Je Dienst und Registry: Tag vorhanden, beide Plattformen, Signatur gültig (cosign.pub). Zwischen den Registries: gleicher
# Digest. Schwebende Tags (X.Y, X, latest), wo sie nach den Git-Tags dieses Repos zu erwarten sind, zeigen auf denselben Digest
# (nicht mit --exact-only). Updater-Image: enthält /usr/share/opnexus/docker-compose.yml der Version (ohne --no-compose; zieht das
# Image einmal). Fehler nennen Dienst, Registry und Schritt, mit der bereinigten Ausgabe des fehlgeschlagenen Werkzeugs.
# Läuft auch ohne Zugangsdaten (öffentlich lesbare Registries). Exit 0 nur, wenn alles stimmt.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/release_common.sh

tag="${1:?Usage: $0 vX.Y.Z [--repo REPO]... [--exact-only] [--no-compose]}"; shift
repos=(); compose=1; exact_only=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repos+=("${2:?--repo braucht eine Registry}"); shift ;;
    --no-compose) compose=0 ;;
    --exact-only) exact_only=1 ;;
    *) release_die "Unbekannte Option: $1" ;;
  esac
  shift
done
version="$(release_version_of_tag "$tag")"
[ "${#repos[@]}" -gt 0 ] || repos=("$OPNEXUS_PRIMARY_REPO" "$OPNEXUS_MIRROR_REPO")
command -v cosign >/dev/null 2>&1 || release_die "cosign fehlt."
[ -f "${OPNEXUS_COSIGN_PUB:-cosign.pub}" ] || release_die "cosign.pub fehlt."

fail=0
bad() { echo "    FEHLER: $*" >&2; fail=1; }

floating=()
if [ "$exact_only" = 0 ]; then
  mapfile -t tags < <(release_tags "$version")
  floating=("${tags[@]:1}")
  git_tags="$(git tag --list 'v[0-9]*' 2>/dev/null | wc -l)"
  if [ "$git_tags" = 0 ] && [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Hinweis: keine Git-Tags vorhanden (flacher Checkout?) — die schwebenden Tags werden nicht geprüft."
    floating=()
  fi
fi

declare -A first   # Dienst -> Digest in der ersten Registry
for repo in "${repos[@]}"; do
  echo "==> $repo ($version)"
  for svc in "${OPNEXUS_SERVICES[@]}"; do
    img="$repo/opnexus-$svc"; before="$fail"
    rc=0; release_tag_digest "$img:$version" d || rc=$?
    if [ "$rc" = 1 ]; then bad "$svc: $img:$version fehlt (Registry $repo)"; continue; fi
    if [ "$rc" = 2 ]; then bad "$svc: $img:$version nicht lesbar (Registry $repo): $(printf '%s' "$d" | tail -n 2 | release_redact | tr '\n' ' ')"; continue; fi
    release_has_platforms "$img@$d" || bad "$svc: $img:$version hat nicht beide Plattformen (amd64, arm64) (Registry $repo)"
    release_step "$svc" "$repo" "Signatur prüfen" cosign verify --key "${OPNEXUS_COSIGN_PUB:-cosign.pub}" --insecure-ignore-tlog=true "$img@$d" \
      || bad "$svc: $img@$d: Signatur ungültig oder fehlt (Registry $repo)"
    if [ -z "${first[$svc]:-}" ]; then first[$svc]="$d"
    elif [ "${first[$svc]}" != "$d" ]; then bad "$svc: Digest in $repo ($d) weicht von dem der ersten Registry (${first[$svc]}) ab"; fi
    for t in "${floating[@]}"; do
      rc=0; release_tag_digest "$img:$t" dt || rc=$?
      [ "$rc" = 0 ] && [ "$dt" = "$d" ] || bad "$svc: $img:$t zeigt auf '${dt:0:80}', erwartet $d (Registry $repo)"
    done
    if [ "$fail" = "$before" ]; then echo "    ok   opnexus-$svc  $d"; fi
  done
done

if [ "$compose" = 1 ] && [ -n "${first[updater]:-}" ]; then
  img="${repos[0]}/opnexus-updater@${first[updater]}"
  echo "==> Release-Compose im Updater-Image ($img)"
  if release_step updater "${repos[0]}" "Updater-Image ziehen" docker pull -q "$img"; then
    out="$(docker run --rm --pull never --entrypoint cat "$img" /usr/share/opnexus/docker-compose.yml 2>/dev/null || true)"
    if ! grep -q '^services:' <<<"$out" || ! grep -q 'opnexus-backend:' <<<"$out"; then
      bad "updater: das Updater-Image enthält keine Release-Compose-Datei (/usr/share/opnexus/docker-compose.yml)"
    else
      grep -qF "OPNEXUS_VERSION:-$version}" <<<"$out" || bad "updater: die Compose-Datei im Updater-Image hat nicht die Version $version als Standard"
      if grep -qF "OPNEXUS_IMAGE_REPO:-$OPNEXUS_PRIMARY_REPO}" <<<"$out"; then echo "    ok   Standard-Registry: $OPNEXUS_PRIMARY_REPO"
      elif grep -qF "OPNEXUS_IMAGE_REPO:-$OPNEXUS_MIRROR_REPO}" <<<"$out"; then echo "    ok   Standard-Registry: $OPNEXUS_MIRROR_REPO (ältere Version)"
      else bad "updater: die Compose-Datei im Updater-Image hat eine unbekannte Standard-Registry"; fi
      ! grep -qE '^\s+build:' <<<"$out" || bad "updater: die Compose-Datei im Updater-Image enthält build:"
    fi
  else
    bad "updater: das Updater-Image $img lässt sich nicht ziehen"
  fi
fi

if [ "$fail" = 0 ]; then
  if [ "$exact_only" = 1 ]; then suffix=" (ohne schwebende Tags)"; else suffix=""; fi
  echo "Alles in Ordnung: $version ist in ${#repos[@]} Registry(s) vollständig, digestgleich und signiert$suffix."
else
  echo "Prüfung FEHLGESCHLAGEN." >&2
fi
exit "$fail"
