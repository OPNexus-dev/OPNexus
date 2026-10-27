#!/usr/bin/env bash
# Gemeinsame Bausteine der Release-Skripte (build_release_images.sh, copy_release_images.sh,
# verify_release_images.sh, release_all.sh). Wird mit `. scripts/release_common.sh` geladen, nicht ausgeführt.
#
# Zwei Registries tragen dieselben Images (gleiche Digests, gleiche cosign-Signatur):
#   PRIMARY  Standard für neue Installationen (ins Release-Bundle und in die Compose-Datei eingebacken)
#   MIRROR   die bisherige Gitea-Registry; bestehende Installationen bleiben unverändert dort
# Beides lässt sich für Tests oder einen Notfall per Umgebung überschreiben.
OPNEXUS_PRIMARY_REPO="${OPNEXUS_PRIMARY_REPO:-ghcr.io/opnexus-dev}"
OPNEXUS_MIRROR_REPO="${OPNEXUS_MIRROR_REPO:-git.opnexus.dev/opnexus}"
OPNEXUS_SERVICES=(backend frontend proxy updater)

# Fehler mit Meldung auf stderr und Abbruch. Die aufrufenden Skripte laufen alle mit `set -e`.
release_die() { echo "$*" >&2; exit 1; }

# Prüft "vX.Y.Z" oder "vX.Y.Z-rcN" und gibt die Version ohne "v" aus.
release_version_of_tag() {
  [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || release_die "Tag muss vX.Y.Z (oder vX.Y.Z-rcN) sein: $1"
  printf '%s\n' "${1#v}"
}

# Die Image-Tags einer Version, einer pro Zeile; der volle Versions-Tag zuerst.
#   stabil und höchste Version ihrer Reihe:  X.Y.Z, X.Y (wenn es keine höhere X.Y.* gibt), X (wenn es keine höhere X.*.* gibt),
#                                            latest (wenn es überhaupt keine höhere stabile Version gibt)
#   Vorabversion (X.Y.Z-rcN):                nur der volle Tag, nie ein schwebender
# Maßgeblich sind die Git-Tags vX.Y.Z des Repositories: Ein nachträglich veröffentlichter älterer Stand (Backfill, Patch einer
# alten Reihe) verschiebt "latest" also nie rückwärts.
release_tags() {
  local version="$1" major minor patch a b c
  printf '%s\n' "$version"
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 0
  IFS=. read -r major minor patch <<<"$version"
  local newer_minor=0 newer_major=0 newer_any=0
  while IFS=. read -r a b c; do
    [ -n "$a" ] || continue
    # Nur höhere Versionen zählen (die eigene Version selbst nicht).
    if (( a > major )) || { (( a == major )) && (( b > minor )); } || { (( a == major )) && (( b == minor )) && (( c > patch )); }; then
      newer_any=1
      (( a == major )) && newer_major=1
      (( a == major )) && (( b == minor )) && newer_minor=1
    fi
  done < <(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sed 's/^v//')
  [ "$newer_minor" = 1 ] || printf '%s\n' "$major.$minor"
  [ "$newer_major" = 1 ] || printf '%s\n' "$major"
  [ "$newer_any" = 1 ] || printf '%s\n' latest
  return 0
}

# Digest ("sha256:…") des Manifests/Index hinter einer Referenz; leer, wenn es sie nicht gibt.
release_digest_of() {
  docker buildx imagetools inspect "$1" --format '{{json .Manifest.Digest}}' 2>/dev/null | tr -d '"' || true
}

# cosign-Prüfung gegen den öffentlichen Schlüssel des Projekts (ohne Transparenzlog, wie beim Signieren).
release_cosign_verify() {
  cosign verify --key "${OPNEXUS_COSIGN_PUB:-cosign.pub}" --insecure-ignore-tlog=true "$1" >/dev/null 2>&1
}

# Beide Plattformen im Image-Index?
release_has_platforms() {
  local out; out="$(docker buildx imagetools inspect "$1" 2>/dev/null)" || return 1
  grep -q 'linux/amd64' <<<"$out" && grep -q 'linux/arm64' <<<"$out"
}

# ── Fehlerdiagnose ────────────────────────────────────────────────────────────────────────────────────────
# Zugangsdaten aus Meldungen entfernen: bekannte Geheimnisse der Umgebung (wörtlich), Zugangsdaten in URLs, Bearer-/Basic-
# Kopfzeilen, Felder wie password=/token:/authorization: und GitHub-Token. Liest stdin, schreibt stdout.
release_redact() {
  local text v val
  text="$(cat)"
  for v in GHCR_TOKEN GITEA_OPNEXUS_TOKEN FORGEJO_PRIVATE_TOKEN CLOUDFLARE_API_TOKEN COSIGN_PASSWORD; do
    val="${!v:-}"
    if [ "${#val}" -ge 6 ]; then text="${text//"$val"/***}"; fi
  done
  printf '%s\n' "$text" | sed -E \
    -e 's#(https?://)[^/@[:space:]]+@#\1***@#g' \
    -e 's#([Bb]earer|[Bb]asic)[[:space:]]+[A-Za-z0-9._~+/=-]+#\1 ***#g' \
    -e "s#((password|passwd|token|secret|authorization|credential)[A-Za-z_-]*[\"']?[[:space:]]*[:=][[:space:]]*)[^[:space:],;\"']+#\1***#Ig" \
    -e 's#gh[pousr]_[A-Za-z0-9]{16,}#***#g' \
    -e 's#github_pat_[A-Za-z0-9_]{16,}#***#g'
}

# release_step DIENST REGISTRY "Schritt" BEFEHL...   Führt den Befehl aus, ohne seine Ausgabe zu verwerfen: Bei einem Fehler steht Dienst,
# Registry, Schritt und Exit-Code in der Meldung, darunter die letzten Zeilen der (bereinigten) Ausgabe; bei Erfolg bleibt es still.
RELEASE_LAST_OUTPUT=""
release_step() {
  local svc="$1" reg="$2" step="$3" out rc=0
  shift 3
  out="$("$@" 2>&1)" || rc=$?
  RELEASE_LAST_OUTPUT="$out"
  [ "$rc" != 0 ] || return 0
  {
    echo "    FEHLER [Dienst: $svc | Registry: $reg | Schritt: $step | Exit-Code: $rc]"
    printf '%s\n' "$out" | grep -vE '^\s*$|Skipping tlog verification|reference for unknown type' | tail -n 8 | release_redact | sed 's/^/      | /'
  } >&2
  return 1
}

# Schlägt ein Befehl fehl, dessen Ausgabe schon auf den Bildschirm ging (release_step), bricht das Skript mit dem Hinweis ab, was nun gilt.
release_abort() { echo "ABBRUCH: $*" >&2; exit 1; }

# release_tag_digest REF OUTVAR   Rückgabe 0: gefunden (OUTVAR = Digest), 1: das Tag gibt es nicht (OUTVAR leer),
# 2: jeder andere Fehler, z. B. Netz oder Anmeldung (OUTVAR = Ausgabe). Nur "nicht vorhanden" gilt als nicht vorhanden.
release_tag_digest() {
  local -n _digest_out="$2"
  local o rc=0
  o="$(docker buildx imagetools inspect "$1" --format '{{json .Manifest.Digest}}' 2>&1)" || rc=$?
  if [ "$rc" = 0 ]; then _digest_out="$(tr -d '"' <<<"$o" | tail -n1)"; return 0; fi
  if grep -qiE 'not found|MANIFEST_UNKNOWN|NAME_UNKNOWN' <<<"$o"; then _digest_out=""; return 1; fi
  _digest_out="$o"
  return 2
}

# Zustand der Signatur eines Images: valid | none (keine Signatur) | invalid (Signatur, aber nicht von unserem Schlüssel) |
# unknown (alles andere, z. B. Netz-/Anmeldefehler: wird nie als "nicht signiert" gewertet).
release_signature_state() {
  local out rc=0
  out="$(cosign verify --key "${OPNEXUS_COSIGN_PUB:-cosign.pub}" --insecure-ignore-tlog=true "$1" 2>&1)" || rc=$?
  if [ "$rc" = 0 ]; then echo valid
  elif grep -qiE 'no signatures found' <<<"$out"; then echo none
  elif grep -qiE 'failed to verify signature|do not match threshold|no matching (signatures|attestations)|invalid signature' <<<"$out"; then echo invalid
  else echo unknown; fi
}
