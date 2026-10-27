#!/usr/bin/env bash
# Kopiert die Release-Images einer Version digestgleich von einer Registry in eine andere, samt cosign-Signatur.
# Die Images werden nur EINMAL gebaut (scripts/build_release_images.sh) und signiert; hier entsteht kein neuer Build.
# Kopiert werden NUR die unveränderlichen Versions-Tags (X.Y.Z). Die schwebenden Tags (X.Y, X, latest) setzt erst
# scripts/promote_floating_tags.sh, nachdem alle vier Images, ihre Signaturen und die Release-Compose geprüft sind.
#
#   scripts/copy_release_images.sh v1.36.0                       # Gitea-Registry -> GHCR (die Standardrichtung)
#   scripts/copy_release_images.sh v1.36.0 --dry-run             # nur prüfen und anzeigen, nichts schreiben
#   scripts/copy_release_images.sh v1.35.0 --force               # ein vorhandenes, NICHT gültig signiertes Tag ersetzen
#   scripts/copy_release_images.sh v1.36.0 --from REPO --to REPO
#   scripts/copy_release_images.sh v1.36.0 --assume-absent       # nur für ein brandneues Ziel-Paket, siehe unten
#
# Vorher: docker login bei der Ziel-Registry (die Quelle muss lesbar sein).
# Ablauf (bricht beim ersten Fehler ab):
#   1. Prüfung ohne Schreiben, für alle vier Dienste: Quelle vorhanden, beide Plattformen (amd64, arm64), Signatur der Quelle gültig
#      (ein unsigniertes Image wird nie kopiert), Zustand des Ziel-Tags (siehe Schutz).
#   2. Je Dienst, nacheinander: ZUERST die Signatur (Tag sha256-<hex>, das Schema von cosign 3, mit dem alle signierten OPNexus-Images
#      ab 1.23.9 signiert sind), DANN das Image (Index + Plattform-Manifeste + Attestierungen, Digest bleibt gleich), dann Prüfung im
#      Ziel: gleicher Digest, Signatur gültig. So gibt es das Versions-Tag nie ohne Signatur. Nimmt eine Registry die Signatur vor dem
#      Image nicht an, wird mit Hinweis die umgekehrte Reihenfolge benutzt (dann gibt es kurz ein Tag ohne Signatur; scheitert
#      der Schritt, bleibt das so, bis der Aufruf wiederholt wird).
#      Ein Image gilt erst als veröffentlicht, wenn Digest UND Signatur im Ziel geprüft sind; vollständig ist die Version erst, wenn
#      das für alle vier Dienste gilt ("VOLLSTÄNDIG" am Ende). `cosign copy` ist ungeeignet (es lässt das Schema von cosign 3 aus),
#      und eine Signatur im alten .sig-Schema würde `imagetools` unbrauchbar umpacken: solche Images werden abgelehnt.
#   Wiederholbar: ein Abbruch (Netz, Anmeldung, Kontingent) hinterlässt nur Signatur-Tags ohne Image oder fertige Dienste; derselbe
#   Aufruf macht weiter, was fehlt, und ändert Fertiges nicht.
#
# SCHUTZ vorhandener Versions-Tags im Ziel (Versionen sind unveränderlich):
#   gleicher Digest .................. in Ordnung (Wiederanlauf)
#   anderer Digest, GÜLTIG signiert .. wird NIE überschrieben, auch nicht mit --force
#   anderer Digest, ohne gültige Signatur (keine Signatur oder nicht von unserem Schlüssel) .. nur mit --force
#   Zustand nicht feststellbar (Netz-, Anmelde-, Registry-Fehler) .. wird nie überschrieben
# Gibt die Registry beim Nachsehen weder "gefunden" noch "nicht gefunden" zurück (z. B. "denied" bei einem noch nicht angelegten
# Paket), bricht das Skript ab. --assume-absent behandelt das ausdrücklich als "gibt es noch nicht": nur benutzen, wenn das Paket
# neu ist.
# Notfall: Liest das Ziel die kopierte Signatur nicht (Registry ohne passende Referrers-Unterstützung), kann mit
# OPNEXUS_RESIGN_ON_COPY_FAILURE=1 und COSIGN_KEY (derselbe Schlüssel wie beim Bau) im Ziel neu signiert werden; die Prüfung
# gegen cosign.pub bleibt Pflicht.
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/release_common.sh

tag="${1:?Usage: $0 vX.Y.Z[-rcN] [--from REPO] [--to REPO] [--dry-run] [--force] [--assume-absent]}"; shift
from="$OPNEXUS_MIRROR_REPO"; to="$OPNEXUS_PRIMARY_REPO"; dry=0; force=0; assume_absent=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from) from="${2:?--from braucht eine Registry}"; shift ;;
    --to) to="${2:?--to braucht eine Registry}"; shift ;;
    --dry-run) dry=1 ;;
    --force) force=1 ;;
    --assume-absent) assume_absent=1 ;;
    *) release_die "Unbekannte Option: $1" ;;
  esac
  shift
done
version="$(release_version_of_tag "$tag")"
[ "$from" != "$to" ] || release_die "Quelle und Ziel sind dieselbe Registry ($from)."
command -v cosign >/dev/null 2>&1 || release_die "cosign fehlt (ohne Signaturprüfung wird nichts kopiert)."
[ -f "${OPNEXUS_COSIGN_PUB:-cosign.pub}" ] || release_die "cosign.pub fehlt."

declare -A digest
echo "==> Prüfung: $from -> $to ($version)"
for svc in "${OPNEXUS_SERVICES[@]}"; do
  src="$from/opnexus-$svc"; dst="$to/opnexus-$svc"
  rc=0; release_tag_digest "$src:$version" d || rc=$?
  if [ "$rc" = 1 ]; then release_die "$svc: $src:$version gibt es nicht."; fi
  if [ "$rc" = 2 ]; then printf '%s\n' "$d" | grep -vE '^\s*$' | tail -n 6 | release_redact | sed 's/^/      | /' >&2; release_die "$svc: $src:$version ist nicht lesbar (Registry $from nicht erreichbar oder keine Berechtigung?)."; fi
  [[ "$d" =~ ^sha256:[0-9a-f]{64}$ ]] || release_die "$svc: unerwarteter Digest '$d' für $src:$version."
  release_has_platforms "$src@$d" || release_die "$svc: $src:$version enthält nicht beide Plattformen (linux/amd64 und linux/arm64)."
  release_step "$svc" "$from" "Signatur der Quelle prüfen" cosign verify --key "${OPNEXUS_COSIGN_PUB:-cosign.pub}" --insecure-ignore-tlog=true "$src@$d" \
    || release_die "$svc: Signatur von $src@$d ist ungültig oder fehlt — es wird nichts kopiert."
  docker buildx imagetools inspect "$src:sha256-${d#sha256:}" >/dev/null 2>&1 \
    || release_die "$svc: $src@$d ist signiert, aber nicht im Schema von cosign 3 (Tag sha256-<hex> fehlt) — das wird nicht übertragen. Neu signieren oder dort lassen."

  rc=0; release_tag_digest "$dst:$version" have || rc=$?
  if [ "$rc" = 2 ]; then
    if [ "$assume_absent" = 1 ]; then
      echo "    Hinweis ($svc): Zustand von $dst:$version nicht feststellbar — wird mit --assume-absent als 'nicht vorhanden' behandelt."; have=""
    else
      printf '%s\n' "$have" | grep -vE '^\s*$' | tail -n 6 | release_redact | sed 's/^/      | /' >&2
      release_die "$svc: Zustand von $dst:$version nicht feststellbar (Registry $to nicht erreichbar, nicht angemeldet, oder das Paket gibt es noch nicht — dann --assume-absent). Es wurde nichts geschrieben."
    fi
  fi
  if [ -n "$have" ] && [ "$have" != "$d" ]; then
    sigstate="$(release_signature_state "$dst@$have")"
    case "$sigstate" in
      valid) release_die "$svc: $dst:$version ist mit einem ANDEREN Digest ($have, Quelle: $d) vorhanden und gültig signiert. Ein so geschütztes Versions-Tag wird nie überschrieben, auch nicht mit --force. Soll die Quelle gelten, eine neue Version veröffentlichen." ;;
      none|invalid)
        [ "$force" = 1 ] || release_die "$svc: $dst:$version existiert mit einem anderen Digest ($have, Quelle: $d) ohne gültige Signatur (Zustand: $sigstate). Nur mit --force zu ersetzen."
        echo "    ACHTUNG --force ($svc): $dst:$version ($have, Signatur: $sigstate) wird durch $d ersetzt." ;;
      *) release_die "$svc: $dst:$version existiert mit einem anderen Digest ($have), aber die Signatur lässt sich nicht beurteilen (Netz-/Anmeldefehler?). Es wird nichts überschrieben — Prüfung wiederholen." ;;
    esac
  fi
  digest[$svc]="$d"
  echo "    ok   opnexus-$svc  $d${have:+  (Ziel-Tag: $([ "$have" = "$d" ] && echo schon vorhanden || echo wird ersetzt))}"
done

if [ "$dry" = 1 ]; then
  echo "==> Trockenlauf: kopiert würde $version (4 Dienste, Digests wie oben), je Dienst erst die Signatur, dann das Image. Schwebende Tags: scripts/promote_floating_tags.sh $tag."
  exit 0
fi

resume_hint() { echo "    Nichts davon ist unumkehrbar falsch: Signatur-Tags ohne Image sind wirkungslos, fertige Dienste bleiben gültig. Derselbe Aufruf setzt fort: $0 $tag --from $from --to $to" >&2; }

echo "==> Kopiere $version"
done_services=()
for svc in "${OPNEXUS_SERVICES[@]}"; do
  src="$from/opnexus-$svc"; dst="$to/opnexus-$svc"; d="${digest[$svc]}"
  st="sha256-${d#sha256:}"   # Signatur-Tag (Referrers-Ausweichschema)
  sig_done=0
  if release_step "$svc" "$to" "Signatur kopieren (vor dem Image)" docker buildx imagetools create -t "$dst:$st" "$src:$st"; then
    sig_done=1
  else
    echo "    Hinweis ($svc): $to nimmt die Signatur vor dem Image nicht an — kopiere zuerst das Image, dann die Signatur."
  fi
  release_step "$svc" "$to" "Image kopieren" docker buildx imagetools create -t "$dst:$version" "$src@$d" \
    || { resume_hint; release_abort "$svc konnte nicht kopiert werden. Fertig: ${done_services[*]:-keiner}. $( [ "$sig_done" = 1 ] && echo "Die Signatur von $svc liegt schon im Ziel (ohne Image wirkungslos)." || echo "Im Ziel wurde für $svc nichts angelegt." )"; }
  if [ "$sig_done" = 0 ]; then
    release_step "$svc" "$to" "Signatur kopieren (nach dem Image)" docker buildx imagetools create -t "$dst:$st" "$src:$st" \
      || { resume_hint; release_abort "$svc: $dst:$version existiert, aber OHNE Signatur (nicht verwenden: opnexus update lehnt es ab). Aufruf wiederholen."; }
  fi
  got=""; rc=0; release_tag_digest "$dst:$version" got || rc=$?
  [ "$rc" = 0 ] && [ "$got" = "$d" ] || { resume_hint; release_abort "$svc: $dst:$version hat nach dem Kopieren nicht den Digest $d (gelesen: '${got:0:80}')."; }
  if ! release_step "$svc" "$to" "Signatur prüfen" cosign verify --key "${OPNEXUS_COSIGN_PUB:-cosign.pub}" --insecure-ignore-tlog=true "$dst@$d"; then
    if [ "${OPNEXUS_RESIGN_ON_COPY_FAILURE:-0}" = 1 ] && [ -n "${COSIGN_KEY:-}" ]; then
      echo "    Signatur im Ziel nicht lesbar — signiere $dst@$d mit demselben Schlüssel neu."
      release_step "$svc" "$to" "Neu signieren" cosign sign --yes --use-signing-config=false --tlog-upload=false --key "$COSIGN_KEY" "$dst@$d" \
        || { resume_hint; release_abort "$svc: Neu signieren fehlgeschlagen."; }
    fi
    release_step "$svc" "$to" "Signatur prüfen (nach dem Kopieren)" cosign verify --key "${OPNEXUS_COSIGN_PUB:-cosign.pub}" --insecure-ignore-tlog=true "$dst@$d" \
      || { resume_hint; release_abort "$svc: Signatur von $dst@$d ist nach dem Kopieren ungültig oder nicht lesbar. Das Tag $version existiert in $to; es gilt als NICHT veröffentlicht, bis ein Aufruf diesen Schritt besteht."; }
  fi
  done_services+=("$svc")
  echo "    ok   $dst:$version  $d  (Digest und Signatur geprüft)"
done

echo "VOLLSTÄNDIG: $version liegt in $to für alle ${#OPNEXUS_SERVICES[@]} Dienste digestgleich und mit geprüfter Signatur."
echo "Noch KEINE schwebenden Tags. Weiter: scripts/promote_floating_tags.sh $tag   (prüft zuerst alles, setzt dann X.Y, X, latest)"
