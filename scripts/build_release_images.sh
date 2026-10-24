#!/usr/bin/env bash
# Baut die Release-Images (backend, frontend, proxy, updater) aus dem Stand eines
# Tags und pusht sie optional — für den Fall, dass (noch) kein CI-Runner
# existiert. Tags und Namen entsprechen .github/workflows/release.yml:
#   <REPO>/opnexus-{backend,frontend,proxy,updater}:X.Y.Z, :X.Y, :X (+ :latest bei stabilen Versionen)
#
#   scripts/build_release_images.sh v1.10.0                    # nur bauen (Architektur dieses Hosts)
#   docker login git.opnexus.dev
#   scripts/build_release_images.sh v1.10.0 --push             # bauen und pushen
#   scripts/build_release_images.sh v1.10.0 --push --multiarch # amd64+arm64 (braucht QEMU/buildx)
#
# Einmalig pro Build-Host für --multiarch (scripts/release_all.sh nutzt es immer):
#   docker run --privileged --rm tonistiigi/binfmt --install arm64           # QEMU-Emulation (bis zum Neustart)
#   docker buildx create --name opnexus-multiarch --driver docker-container --bootstrap
# Der Builder lässt sich mit OPNEXUS_BUILDER ändern; fehlt er, bricht das Skript mit dieser Anleitung ab.
#
# Signieren (cosign): OPNEXUS_SIGN=1 COSIGN_KEY=/pfad/cosign.key [COSIGN_PASSWORD=…] — der öffentliche
# Schlüssel liegt als cosign.pub im Repo; die Registry muss ihren Token-Realm per https melden.
# REPO: Umgebungsvariable OPNEXUS_IMAGE_REPO (Standard git.opnexus.dev/opnexus).
# Hinweis: Hinter dem Cloudflare-Proxy (Free) scheitern Pushes mit Layern > 100 MB —
# dann die Registry-Domain auf "DNS only" stellen oder aus dem LAN pushen.
set -euo pipefail
cd "$(dirname "$0")/.."

tag="${1:?Usage: $0 vX.Y.Z[-rcN] [--push] [--multiarch]}"; shift
push=0; multi=0
for a in "$@"; do case "$a" in --push) push=1;; --multiarch) multi=1;; *) echo "Unbekannte Option: $a" >&2; exit 1;; esac; done
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || { echo "Tag muss vX.Y.Z (oder vX.Y.Z-rcN) sein" >&2; exit 1; }
[ "$multi" = 0 ] || [ "$push" = 1 ] || { echo "--multiarch geht nur zusammen mit --push" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null || { echo "Tag $tag existiert lokal nicht" >&2; exit 1; }
repo="${OPNEXUS_IMAGE_REPO:-git.opnexus.dev/opnexus}"
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
version="${tag#v}"

# Aus dem Tag bauen, nicht aus dem Working Tree (keine uncommitteten Änderungen im Image).
src="$(mktemp -d)"; trap 'rm -rf "$src"' EXIT
git archive "$tag" | tar -x -C "$src"
(cd "$src" && scripts/check_version.sh "${tag%%-*}")
# Die Release-Fassung der Compose-Datei ins Updater-Image: `opnexus update` übernimmt sie von dort (B18).
(cd "$src" && scripts/release_compose.sh "$version" "$repo" > updater/docker-compose.release.yml)

tags=("$version")
if [[ "$version" != *-* ]]; then   # nur stabile Versionen bekommen Kurz-Tags und latest
  IFS=. read -r major minor _ <<<"$version"
  tags+=("$major.$minor" "$major" latest)
fi

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
      digest="$(docker image inspect "$image:$version" --format '{{json .RepoDigests}}' | tr ',' '\n' | grep -F "$image@" | tr -d '[]"')"; digest="${digest##*@}"
    fi
    [ -n "$digest" ] || { echo "Digest von $image:$version nicht gefunden (Push fehlgeschlagen?)" >&2; exit 1; }
    # Ohne öffentliches Transparenzlog (Rekor): Prüfen soll auch in abgeschotteten Netzen gehen.
    cosign sign --yes --use-signing-config=false --tlog-upload=false --key "$COSIGN_KEY" "$image@$digest"
    cosign verify --key cosign.pub --insecure-ignore-tlog=true "$image@$digest" >/dev/null
    echo "    signiert: $image@$digest"
  fi
done
[ "$push" = 1 ] && echo "Fertig und gepusht: $repo (${tags[*]})" || echo "Fertig (lokal gebaut, nicht gepusht)."
