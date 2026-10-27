#!/usr/bin/env bash
# Setzt die schwebenden Tags (X.Y, X, latest) einer Version in den Registries, ERST NACHDEM alles geprüft ist.
#
#   scripts/promote_floating_tags.sh v1.36.0                       # GHCR und Gitea
#   scripts/promote_floating_tags.sh v1.36.0 --repo REPO [--repo REPO] [--dry-run]
#
# Vorbedingung (wird hier selbst geprüft, mit scripts/verify_release_images.sh --exact-only): in ALLEN genannten Registries
# liegen alle vier Dienste unter dem vollen Versions-Tag mit gleichem Digest, beiden Plattformen und gültiger Signatur, und das
# Updater-Image enthält die Release-Compose. Ist etwas nicht in Ordnung, wird KEIN schwebendes Tag angefasst.
# Welche schwebenden Tags zu setzen sind, bestimmt release_tags (release_common.sh): nur stabile Versionen, nur wo es keine
# höhere Version gibt. Eine Vorabversion oder ein älterer Stand ändert nichts.
#
# EINSCHRÄNKUNG: Registries kennen keine atomare Änderung mehrerer Tags oder Images. Beim Umsetzen, Dienst für Dienst, gibt es
# darum ein kurzes Fenster, in dem z. B. backend:latest schon neu ist und frontend:latest noch alt. Wer produktiv ein schwebendes
# Tag verfolgt, kann in diesem Fenster eine Mischung ziehen (jedes Image für sich ist gültig signiert). Darum: produktiv eine feste
# Version pinnen (OPNEXUS_VERSION=X.Y.Z), und hier gilt:
#   - die alten Digests aller Tags werden vor der ersten Änderung gelesen (nicht lesbar -> Abbruch ohne Änderung);
#   - scheitert eine Änderung, werden schon umgesetzte Tags auf ihren alten Digest zurückgesetzt (soweit dessen Manifest noch
#     in der Registry liegt) und die Meldung nennt jedes Tag, das nicht zurückgesetzt werden konnte, mit Registry und altem Digest;
#   - ein Tag, das es vorher nicht gab, lässt sich mit diesem Werkzeug nicht löschen: es bleibt auf dem NEUEN, geprüften Digest
#     stehen (in sich stimmig, nur nicht über alle Dienste); der Aufruf ist wiederholbar und setzt fort, was fehlt.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/release_common.sh

tag="${1:?Usage: $0 vX.Y.Z [--repo REPO]... [--dry-run]}"; shift
repos=(); dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repos+=("${2:?--repo braucht eine Registry}"); shift ;;
    --dry-run) dry=1 ;;
    *) release_die "Unbekannte Option: $1" ;;
  esac
  shift
done
version="$(release_version_of_tag "$tag")"
[ "${#repos[@]}" -gt 0 ] || repos=("$OPNEXUS_PRIMARY_REPO" "$OPNEXUS_MIRROR_REPO")

mapfile -t all_tags < <(release_tags "$version")
floating=("${all_tags[@]:1}")
if [ "${#floating[@]}" = 0 ]; then
  echo "Keine schwebenden Tags für $version (Vorabversion oder es gibt eine höhere Version): nichts zu tun."
  exit 0
fi

echo "==> Vorbedingung: alle Images, Signaturen und die Release-Compose von $version prüfen"
verify_args=(); for r in "${repos[@]}"; do verify_args+=(--repo "$r"); done
scripts/verify_release_images.sh "$tag" --exact-only "${verify_args[@]}" \
  || release_abort "Die Prüfung von $version ist nicht bestanden — es wurde KEIN schwebendes Tag verändert."

# Digest je Dienst (aus der ersten Registry; die Prüfung hat gleiche Digests überall sichergestellt) und alter Stand aller Tags.
declare -A want old
for svc in "${OPNEXUS_SERVICES[@]}"; do
  release_tag_digest "${repos[0]}/opnexus-$svc:$version" got || release_abort "$svc: $version ist nicht mehr lesbar."
  want[$svc]="$got"
  for repo in "${repos[@]}"; do
    for t in "${floating[@]}"; do
      rc=0; release_tag_digest "$repo/opnexus-$svc:$t" cur || rc=$?
      if [ "$rc" = 2 ]; then
        printf '%s\n' "$cur" | grep -vE '^\s*$' | tail -n 4 | release_redact | sed 's/^/      | /' >&2
        release_abort "$svc: $repo/opnexus-$svc:$t ist nicht lesbar (Registry $repo) — es wurde nichts verändert."
      fi
      old["$repo|$svc|$t"]="$cur"
    done
  done
done

echo "==> Schwebende Tags für $version: ${floating[*]}  (Registries: ${repos[*]})"
changes=()   # "repo|svc|tag" in der Reihenfolge der Änderung
for repo in "${repos[@]}"; do
  for svc in "${OPNEXUS_SERVICES[@]}"; do
    for t in "${floating[@]}"; do
      key="$repo|$svc|$t"
      if [ "${old[$key]}" = "${want[$svc]}" ]; then continue; fi   # schon richtig (Wiederholung)
      if [ "$dry" = 1 ]; then echo "    würde setzen: $repo/opnexus-$svc:$t  ${old[$key]:-(neu)} -> ${want[$svc]}"; continue; fi
      if release_step "$svc" "$repo" "Tag $t setzen" docker buildx imagetools create -t "$repo/opnexus-$svc:$t" "$repo/opnexus-$svc@${want[$svc]}"; then
        changes+=("$key")
        rc=0; release_tag_digest "$repo/opnexus-$svc:$t" now || rc=$?
        if [ "$rc" = 0 ] && [ "$now" = "${want[$svc]}" ]; then continue; fi
        echo "    FEHLER [Dienst: $svc | Registry: $repo | Schritt: Tag $t prüfen]: zeigt auf '${now:0:80}', erwartet ${want[$svc]}" >&2
      fi
      # ── Fehler: schon umgesetzte Tags zurücksetzen ──
      echo "==> Fehler beim Setzen von $repo/opnexus-$svc:$t — setze bereits geänderte Tags zurück (Reihenfolge rückwärts)" >&2
      stuck=()
      for ((i=${#changes[@]}-1; i>=0; i--)); do
        IFS='|' read -r r s tt <<<"${changes[$i]}"
        prev="${old[${changes[$i]}]}"
        if [ -z "$prev" ]; then stuck+=("$r/opnexus-$s:$tt (war neu; bleibt auf ${want[$s]}, mit der Registry-Oberfläche löschbar)"); continue; fi
        if release_step "$s" "$r" "Tag $tt zurücksetzen" docker buildx imagetools create -t "$r/opnexus-$s:$tt" "$r/opnexus-$s@$prev"; then
          echo "    zurückgesetzt: $r/opnexus-$s:$tt -> $prev" >&2
        else
          stuck+=("$r/opnexus-$s:$tt (sollte wieder auf $prev zeigen: docker buildx imagetools create -t $r/opnexus-$s:$tt $r/opnexus-$s@$prev)")
        fi
      done
      if [ "${#stuck[@]}" -gt 0 ]; then
        echo "ACHTUNG: nicht zurückgesetzt:" >&2; printf '  - %s\n' "${stuck[@]}" >&2
        release_abort "Schwebende Tags sind über die Dienste hinweg NICHT einheitlich, bis dieser Aufruf wiederholt wird (er setzt fort, was fehlt)."
      fi
      release_abort "Schwebende Tags wurden zurückgesetzt, $version ist unter seinem vollen Tag weiter vollständig. Aufruf wiederholen, wenn die Ursache behoben ist."
    done
  done
done

if [ "$dry" = 1 ]; then echo "Trockenlauf beendet, nichts geändert."; exit 0; fi
echo "==> Endprüfung inklusive schwebender Tags"
scripts/verify_release_images.sh "$tag" --no-compose "${verify_args[@]}" \
  || release_abort "Die Endprüfung nach dem Setzen der schwebenden Tags ist fehlgeschlagen — Aufruf wiederholen oder Zustand prüfen."
echo "Fertig: ${floating[*]} zeigen in ${#repos[@]} Registry(s) auf $version."
