#!/usr/bin/env bash
# Baut die Release-Images (backend, frontend, proxy, updater) aus dem Stand eines
# Tags und pusht sie optional. Das ist der EINZIGE Build der Release-Images: signiert wird hier,
# in die zweite Registry kommen sie danach digestgleich per scripts/copy_release_images.sh (keine
# zweite, unabhängige Bauweise — ein anderer Build hätte einen anderen Digest und keine Signatur).
#   <REPO>/opnexus-{backend,frontend,proxy,updater}:X.Y.Z
#   dazu :X.Y, :X und :latest, aber nur für stabile Versionen und nur, wenn es keine höhere Version gibt
#   (scripts/release_common.sh, release_tags) — außer mit --no-floating: dann entsteht NUR der volle Versions-Tag. Die
#   schwebenden Tags setzt scripts/promote_floating_tags.sh nach der Prüfung aller Images (release_all.sh macht das so).
#
#   scripts/build_release_images.sh v1.10.0                    # nur bauen (Architektur dieses Hosts)
#   docker login git.opnexus.dev
#   scripts/build_release_images.sh v1.10.0 --push             # bauen und pushen
#   scripts/build_release_images.sh v1.10.0 --push --multiarch # amd64+arm64 (braucht QEMU/buildx)
#   ... --no-floating                                          # nur der volle Versions-Tag, keine schwebenden Tags
#
# Einmalig pro Build-Host für --multiarch (scripts/release_all.sh nutzt es immer):
#   docker run --privileged --rm tonistiigi/binfmt --install arm64           # QEMU-Emulation (bis zum Neustart)
#   docker buildx create --name opnexus-multiarch --driver docker-container --bootstrap
# Der Builder lässt sich mit OPNEXUS_BUILDER ändern; fehlt er, bricht das Skript mit dieser Anleitung ab.
#
# Signieren (cosign): OPNEXUS_SIGN=1 COSIGN_KEY=/pfad/cosign.key [COSIGN_PASSWORD=…] — der öffentliche
# Schlüssel liegt als cosign.pub im Repo; die Registry muss ihren Token-Realm per https melden.
# Ziel des Pushs: OPNEXUS_BUILD_REPO (Standard: die Gitea-Registry git.opnexus.dev/opnexus; ersatzweise der
# frühere Name OPNEXUS_IMAGE_REPO). Welche Registry die Compose-Datei und das Bundle als Standard für neue
# Installationen eingebacken bekommen, ist davon getrennt: OPNEXUS_COMPOSE_REPO (Standard ghcr.io/opnexus-dev).
# Hinweis: Hinter dem Cloudflare-Proxy (Free) scheitern Pushes mit Layern > 100 MB —
# dann die Registry-Domain auf "DNS only" stellen oder aus dem LAN pushen.
set -euo pipefail
cd "$(dirname "$0")/.."

. scripts/release_common.sh
tag="${1:?Usage: $0 vX.Y.Z[-rcN] [--push] [--multiarch] [--no-floating]}"; shift
push=0; multi=0; floating=1
for a in "$@"; do case "$a" in --push) push=1;; --multiarch) multi=1;; --no-floating) floating=0;; *) echo "Unbekannte Option: $a" >&2; exit 1;; esac; done
version="$(release_version_of_tag "$tag")"
[ "$multi" = 0 ] || [ "$push" = 1 ] || { echo "--multiarch geht nur zusammen mit --push" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null || { echo "Tag $tag existiert lokal nicht" >&2; exit 1; }
repo="${OPNEXUS_BUILD_REPO:-${OPNEXUS_IMAGE_REPO:-$OPNEXUS_MIRROR_REPO}}"
compose_repo="${OPNEXUS_COMPOSE_REPO:-$OPNEXUS_PRIMARY_REPO}"
builder="${OPNEXUS_BUILDER:-opnexus-multiarch}"
if [ "$multi" = 1 ]; then
  docker buildx inspect "$builder" >/dev/null 2>&1 \
    || { echo "buildx-Builder '$builder' fehlt: docker buildx create --name $builder --driver docker-container --bootstrap" >&2; exit 1; }
  # Der Cross-Build füllt den Build-Cache um mehrere GB; ein volles Dateisystem legt auch die laufenden Dienste lahm.
  free_mb="$(df --output=avail -BM . | tail -1 | tr -dc 0-9)"
  [ "$free_mb" -ge 3500 ] || { echo "Nur ${free_mb} MB frei (für --multiarch werden ca. 3500 MB gebraucht): Platz schaffen (docker image prune, docker buildx prune)" >&2; exit 1; }
  # Emulation ist nach einem Neustart weg; ohne sie scheitert der arm64-Teil erst nach Minuten.
  platforms="$(docker buildx inspect "$builder" --bootstrap 2>/dev/null || true)"   # nicht per Pipe an grep -q: SIGPIPE + pipefail
  grep -q 'linux/arm64' <<<"$platforms" \
    || { echo "arm64 nicht verfügbar: docker run --privileged --rm tonistiigi/binfmt --install arm64" >&2; exit 1; }
fi

# Versions-Tags sind unveränderlich. Ein Push würde ein vorhandenes Tag mit einem anderen Digest überschreiben (ein zweiter Bau ist nie
# digestgleich); der Schutz in copy_release_images.sh greift erst beim Kopieren. Darum vor dem ersten Bau: Gibt es eines der vier Versions-Tags
# im Ziel schon, oder lässt sich das nicht feststellbar (Netz, Anmeldung), wird nichts gebaut und nichts geschrieben. Nach einem Abbruch mitten im
# Push gilt dasselbe: das unvollständige Tag in der Registry-Oberfläche löschen (nur wenn es noch nicht veröffentlicht ist) oder eine neue Version nehmen.
if [ "$push" = 1 ]; then
  for svc in "${OPNEXUS_SERVICES[@]}"; do
    rc=0; release_tag_digest "$repo/opnexus-$svc:$version" existing || rc=$?
    case "$rc" in
      0) release_die "$repo/opnexus-$svc:$version existiert bereits ($existing) — Versions-Tags werden nie überschrieben. Nichts wurde gebaut. Eine neue Version veröffentlichen." ;;
      1) ;;
      *) printf '%s\n' "$existing" | grep -vE '^\s*$' | tail -n 4 | release_redact | sed 's/^/      | /' >&2
         release_die "Zustand von $repo/opnexus-$svc:$version nicht feststellbar (Netz oder Anmeldung?) — es wird nichts gebaut, damit kein Tag überschrieben wird." ;;
    esac
  done
fi

# Aus dem Tag bauen, nicht aus dem Working Tree (keine uncommitteten Änderungen im Image).
src="$(mktemp -d)"; trap 'rm -rf "$src"' EXIT
git archive "$tag" | tar -x -C "$src"
(cd "$src" && scripts/check_version.sh "${tag%%-*}")
# Die Release-Fassung der Compose-Datei ins Updater-Image: `opnexus update` übernimmt sie von dort (B18).
(cd "$src" && scripts/release_compose.sh "$version" "$compose_repo" > updater/docker-compose.release.yml)

mapfile -t tags < <(release_tags "$version")   # Vorabversion: nur der volle Tag; stabil: dazu X.Y, X, latest, wo es keine höhere Version gibt
[ "$floating" = 1 ] || tags=("${tags[0]}")      # --no-floating: nur der volle Versions-Tag

for svc in backend frontend proxy updater; do
  image="$repo/opnexus-$svc"
  targs=(); for t in "${tags[@]}"; do targs+=(-t "$image:$t"); done
  echo "==> $image  (${tags[*]})"
  if [ "$multi" = 1 ]; then
    docker buildx build --builder "$builder" --platform linux/amd64,linux/arm64 "${targs[@]}" --push "$src/$svc"
    docker buildx prune --builder "$builder" -af >/dev/null 2>&1 || true   # Platz für den nächsten Dienst
  else
    docker build "${targs[@]}" "$src/$svc"
    if [ "$push" = 1 ]; then for t in "${tags[@]}"; do docker push "$image:$t"; done; fi
  fi
  if [ "$push" = 1 ] && [ "${OPNEXUS_SIGN:-0}" = 1 ]; then
    # Signiert wird der Digest (nicht der veränderliche Tag). Ein Schlüssel: COSIGN_KEY (+ COSIGN_PASSWORD).
    [ -n "${COSIGN_KEY:-}" ] || { echo "OPNEXUS_SIGN=1, aber COSIGN_KEY ist nicht gesetzt" >&2; exit 1; }
    if [ "$multi" = 1 ]; then
      digest="$(docker buildx imagetools inspect "$image:$version" --format '{{json .Manifest.Digest}}' | tr -d '"')"
    else
      # Nur Einträge genau dieses Repositorys (Präfix "$image@", kein Teilstring, nie nach Position): ein lokales Image kann unter mehreren
      # Registry-Adressen liegen. Gäbe es für dieses Repository mehrere verschiedene Digests, ist nicht klar, welcher gerade gepusht wurde: Abbruch.
      digest="$(docker image inspect "$image:$version" --format '{{range .RepoDigests}}{{println .}}{{end}}' | awk -v p="$image@" 'index($0, p) == 1 { sub(/^.*@/, ""); print }' | sort -u)"
      [ "$(printf '%s\n' "$digest" | grep -c . || true)" -le 1 ] || { echo "Mehrere verschiedene Digests für $image lokal vorhanden – nicht eindeutig, nichts wird signiert:" >&2; printf '  %s\n' $digest >&2; exit 1; }
    fi
    [ -n "$digest" ] || { echo "Digest von $image:$version nicht gefunden (Push fehlgeschlagen?)" >&2; exit 1; }
    # Ohne öffentliches Transparenzlog (Rekor): Prüfen soll auch in abgeschotteten Netzen gehen.
    cosign sign --yes --use-signing-config=false --tlog-upload=false --key "$COSIGN_KEY" "$image@$digest"
    cosign verify --key "${OPNEXUS_COSIGN_PUB:-cosign.pub}" --insecure-ignore-tlog=true "$image@$digest" >/dev/null
    echo "    signiert: $image@$digest"
  fi
done
[ "$push" = 1 ] && echo "Fertig und gepusht: $repo (${tags[*]})" || echo "Fertig (lokal gebaut, nicht gepusht)."
