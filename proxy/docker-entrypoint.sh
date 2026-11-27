#!/bin/sh
# Start des Proxys.
#
# 1. Adresslisten glätten (B20). Caddy liest die Adressliste aus OPNEXUS_SITES / OPNEXUS_GUI_SITES als Text in der Caddyfile. Ein Komma
#    ohne Leerzeichen danach ("a,b") ist dort keine Trennung: Caddy lehnt die Adresse ab, und der Proxy startet in einer Schleife neu
#    (HA-Lab 2026-10-25). Hier wird jedes Komma zu ", " (und doppelte Leerzeichen entfallen), bevor Caddy startet.
#
# 2. Webzertifikat (H2-11). Caddy stellt für jede Adresse ein EIGENES Zertifikat aus. Ein Browser sendet für eine nackte IP keinen
#    Servernamen, Caddy liefert dann nur das Zertifikat der Standard-Adresse (OPNEXUS_DEFAULT_SNI): wer die VIP eines HA-Paars oder eine
#    zweite Adresse aufruft, sieht trotz importierter CA "Zertifikat gilt nicht für diese Adresse". Deshalb erzeugt dieser Start EIN Zertifikat
#    mit allen Adressen aus OPNEXUS_SITES (und der Standard-Adresse), signiert mit der Caddy-eigenen Root-CA (dieselbe, die die Clients
#    schon importiert haben; auf beiden HA-Knoten dieselbe). OPNEXUS_WEB_CERT:
#      auto      (Standard) ein Zertifikat für alle Adressen aus der internen CA, wird vor Ablauf selbst erneuert.
#      internal  Caddys eigene Zertifikate je Adresse, wie vor 1.41.
#      custom    ein eigenes Zertifikat: /data/opnexus-web/custom.crt (Kette) und custom.key; ungültig oder fehlend -> Rückfall auf auto.
#    Jeder Fehler hier fällt auf das alte Verhalten zurück (Caddy stellt seine Zertifikate selbst aus): der Proxy startet immer.
set -eu

for var in OPNEXUS_SITES OPNEXUS_GUI_SITES; do
  eval "value=\${$var:-}"
  if [ -n "$value" ]; then
    # Eintrag je Zeile, Leerraum außen weg, leere Einträge (",,", führendes oder letztes Komma) weg, mit ", " wieder verbinden.
    value=$(printf '%s' "$value" | tr ',' '\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^$' | paste -sd, - | sed 's/,/, /g' || true)
    export "$var=$value"
  fi
done

WEB_DIR=${OPNEXUS_WEB_DIR:-/data/opnexus-web}
CA_DIR=${OPNEXUS_CA_DIR:-/data/caddy/pki/authorities/local}
WEB_MODE=${OPNEXUS_WEB_CERT:-auto}
# Erneuert wird, wenn das Zertifikat in weniger als so vielen Tagen abläuft; geprüft beim Start und danach täglich.
RENEW_DAYS=${OPNEXUS_WEB_CERT_RENEW_BEFORE_DAYS:-90}
CHECK_SECONDS=${OPNEXUS_WEB_CERT_CHECK_SECONDS:-86400}
LEAF_DAYS=${OPNEXUS_WEB_CERT_DAYS:-730}   # unter 825: Apple lehnt längere TLS-Zertifikate auch aus eigenen CAs ab

webcert_log() { echo "opnexus-proxy: $*" >&2; }

# Alle Adressen, für die das Zertifikat gelten soll, eine je Zeile: Adresse ohne Schema, Pfad, Klammern und Port, ohne Platzhalter.
webcert_names() {
  { printf '%s\n' "${OPNEXUS_SITES:-}" | tr ',' '\n'; printf '%s\n' "${OPNEXUS_DEFAULT_SNI:-}"; } \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's#^https\{0,1\}://##' -e 's#/.*$##' \
          -e 's/^\[\(.*\)\]\(:[0-9]\{1,5\}\)\{0,1\}$/\1/' -e 's/^\([^:]*\):[0-9]\{1,5\}$/\1/' \
    | grep -v '^$' | grep -v '[*]' | sort -u
}

# subjectAltName-Wert: IPv4 und IPv6 als IP:, alles andere als DNS:.
webcert_san() {
  webcert_names | while IFS= read -r n; do
    case "$n" in
      *:*) printf 'IP:%s\n' "$n" ;;
      *[!0-9.]*) printf 'DNS:%s\n' "$n" ;;
      *) printf 'IP:%s\n' "$n" ;;
    esac
  done | paste -sd, -
}

# Die Root-CA von Caddy liegt erst nach dem ersten ausgestellten Zertifikat vor: fehlt sie, startet kurz eine Hilfsinstanz auf anderen Ports.
webcert_bootstrap_ca() {
  [ -s "$CA_DIR/root.crt" ] && [ -s "$CA_DIR/root.key" ] && return 0
  cat > /tmp/opnexus-boot.caddy <<'BOOT'
{
	admin off
	local_certs
	http_port 18080
	https_port 18443
}
localhost:18443 {
	respond ok
}
BOOT
  caddy run --config /tmp/opnexus-boot.caddy --adapter caddyfile >/tmp/opnexus-boot.log 2>&1 &
  bpid=$!
  i=0
  while [ ! -s "$CA_DIR/root.key" ] && [ "$i" -lt 40 ]; do sleep 1; i=$((i + 1)); done
  kill -TERM "$bpid" 2>/dev/null || true
  wait "$bpid" 2>/dev/null || true
  [ -s "$CA_DIR/root.crt" ] && [ -s "$CA_DIR/root.key" ]
}

# 0 = vorhandenes Zertifikat gilt noch (richtige Adressen, von der aktuellen CA, nicht bald abgelaufen).
webcert_current() {
  [ -s "$WEB_DIR/web.crt" ] && [ -s "$WEB_DIR/web.key" ] || return 1
  [ "$(cat "$WEB_DIR/web.san" 2>/dev/null || true)" = "$1" ] || return 1
  openssl x509 -checkend $((RENEW_DAYS * 86400)) -noout -in "$WEB_DIR/web.crt" >/dev/null 2>&1 || return 1
  openssl verify -CAfile "$CA_DIR/root.crt" "$WEB_DIR/web.crt" >/dev/null 2>&1
}

# Erzeugt das Zertifikat für alle Adressen. 0 = ein gültiges liegt bereit (neu oder unverändert).
webcert_make() {
  san=$(webcert_san)
  [ -n "$san" ] || { webcert_log "keine Adresse für das Zertifikat gefunden"; return 1; }
  mkdir -p "$WEB_DIR" && chmod 700 "$WEB_DIR" || return 1
  webcert_bootstrap_ca || { webcert_log "die interne CA ließ sich nicht anlegen"; return 1; }
  if webcert_current "$san"; then return 0; fi
  tmp=$(mktemp -d /tmp/opnexus-web.XXXXXX) || return 1
  if openssl ecparam -name prime256v1 -genkey -noout -out "$tmp/web.key" 2>/dev/null \
     && printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$san" > "$tmp/ext.cnf" \
     && openssl req -new -key "$tmp/web.key" -subj "/CN=OPNexus" -out "$tmp/web.csr" 2>/dev/null \
     && openssl x509 -req -in "$tmp/web.csr" -CA "$CA_DIR/root.crt" -CAkey "$CA_DIR/root.key" -CAcreateserial -CAserial "$tmp/ca.srl" \
          -days "$LEAF_DAYS" -sha256 -extfile "$tmp/ext.cnf" -out "$tmp/web.crt" 2>/dev/null \
     && openssl verify -CAfile "$CA_DIR/root.crt" "$tmp/web.crt" >/dev/null 2>&1; then
    chmod 600 "$tmp/web.key"
    # Erst Schlüssel, dann Zertifikat: ein Abbruch dazwischen lässt kein Paar zurück, das nicht zusammenpasst (web.san wird zuletzt geschrieben).
    cp "$tmp/web.key" "$WEB_DIR/web.key" && cp "$tmp/web.crt" "$WEB_DIR/web.crt" && printf '%s' "$san" > "$WEB_DIR/web.san" || { rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
    webcert_log "Zertifikat für $san ausgestellt (gültig $LEAF_DAYS Tage)"
    return 0
  fi
  rm -rf "$tmp"
  webcert_log "das Zertifikat ließ sich nicht erzeugen"
  return 1
}

# Eigenes Zertifikat: Zertifikat und Schlüssel müssen lesbar sein, zusammenpassen und gültig sein.
webcert_custom_ok() {
  [ -s "$WEB_DIR/custom.crt" ] && [ -s "$WEB_DIR/custom.key" ] || { webcert_log "OPNEXUS_WEB_CERT=custom, aber custom.crt/custom.key fehlen"; return 1; }
  a=$(openssl x509 -in "$WEB_DIR/custom.crt" -noout -pubkey 2>/dev/null | openssl sha256 2>/dev/null) || a=""
  b=$(openssl pkey -in "$WEB_DIR/custom.key" -pubout 2>/dev/null | openssl sha256 2>/dev/null) || b=""
  [ -n "$a" ] && [ "$a" = "$b" ] || { webcert_log "das eigene Zertifikat und der Schlüssel passen nicht zusammen"; return 1; }
  openssl x509 -checkend 0 -noout -in "$WEB_DIR/custom.crt" >/dev/null 2>&1 || { webcert_log "das eigene Zertifikat ist abgelaufen"; return 1; }
}

# Schreibt die Datei, die die Caddyfile einbindet: leer = Caddys eigene Zertifikate.
webcert_snippet() {  # $1 = crt, $2 = key oder leer
  mkdir -p "$WEB_DIR" 2>/dev/null || return 0
  if [ -n "$1" ]; then printf 'tls %s %s\n' "$1" "$2" > "$WEB_DIR/tls.caddy"; else : > "$WEB_DIR/tls.caddy"; fi
}

webcert_setup() {
  case "$WEB_MODE" in
    internal) webcert_snippet "" ""; return 0 ;;
    custom)
      if webcert_custom_ok; then webcert_snippet "$WEB_DIR/custom.crt" "$WEB_DIR/custom.key"; webcert_log "eigenes Zertifikat in Benutzung"; return 0; fi
      webcert_log "Rückfall auf das Zertifikat aus der internen CA" ;;
    auto) : ;;
    *) webcert_log "OPNEXUS_WEB_CERT=$WEB_MODE ist unbekannt (auto, internal, custom): auto wird benutzt" ;;
  esac
  if webcert_make; then webcert_snippet "$WEB_DIR/web.crt" "$WEB_DIR/web.key"
  else webcert_snippet "" ""; webcert_log "Rückfall: Caddy stellt die Zertifikate je Adresse selbst aus"; fi
}

# 0 = es wurde ein neues Zertifikat ausgestellt (nur dann muss Caddy neu starten).
webcert_renew_if_needed() {
  case "$WEB_MODE" in auto) : ;; *) return 1 ;; esac
  [ -s "$WEB_DIR/web.crt" ] || return 1
  san=$(webcert_san)
  if webcert_current "$san"; then return 1; fi
  webcert_make
}

# Caddy als Kindprozess: ein neu ausgestelltes Zertifikat wird durch einen Neustart von Caddy geladen (die Admin-Schnittstelle ist aus).
# SIGTERM/SIGINT gehen an Caddy weiter; endet Caddy von selbst, endet der Container mit demselben Code (Neustart-Regel von Docker).
run_with_renewal() {
  term=0
  trap 'term=1; [ -n "${child:-}" ] && kill -TERM "$child" 2>/dev/null' TERM INT
  while :; do
    rm -f /tmp/opnexus-renewed
    "$@" &
    child=$!
    (
      while :; do
        sleep "$CHECK_SECONDS"
        if webcert_renew_if_needed; then touch /tmp/opnexus-renewed; kill -TERM "$child" 2>/dev/null; exit 0; fi
      done
    ) &
    watcher=$!
    code=0
    wait "$child" || code=$?
    if [ "$term" = 1 ]; then wait "$child" 2>/dev/null || true; kill "$watcher" 2>/dev/null || true; exit 0; fi
    kill "$watcher" 2>/dev/null || true
    wait "$watcher" 2>/dev/null || true
    if [ -e /tmp/opnexus-renewed ]; then webcert_snippet "$WEB_DIR/web.crt" "$WEB_DIR/web.key"; webcert_log "Zertifikat erneuert, Caddy startet neu"; continue; fi
    exit "$code"
  done
}

# Nur beim Start des eigentlichen Proxys (`caddy run` mit der Haupt-Caddyfile): nicht bei `caddy validate`, und nicht im HA-Gate, der dasselbe Image
# mit einer eigenen Caddyfile (Caddyfile.ha-gate), festem Zertifikat und eingeschränkten Rechten startet.
main_config=0
for arg in "$@"; do [ "$arg" = /etc/caddy/Caddyfile ] && main_config=1; done
if [ "${1:-}" = caddy ] && [ "${2:-}" = run ] && [ "$main_config" = 1 ]; then
  webcert_setup || true
  run_with_renewal "$@"
fi
exec "$@"
