# Caddy mit eingebackener Caddyfile, damit ein Release-Image ohne
# Repo-Checkout lauffähig ist (nur docker-compose.yml + .env nötig).
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
# Glättet Kommas in OPNEXUS_SITES/OPNEXUS_GUI_SITES, bevor Caddy startet (B20); `docker run IMAGE caddy validate ...` läuft ebenso darüber.
COPY docker-entrypoint.sh /usr/local/bin/opnexus-proxy-entrypoint
ENTRYPOINT ["/usr/local/bin/opnexus-proxy-entrypoint"]
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
