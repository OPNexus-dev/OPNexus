#!/bin/sh
# Installiert pg_dump/pg_restore/psql in der Hauptversion des Postgres-Servers
# (Standard 16, passend zu `postgres:16-alpine` in docker-compose.yml).
#
# Warum nicht einfach `apt install postgresql-client`: Debian 13 liefert
# Client 17, und ein von pg_dump 17 geschriebenes Backup lässt sich in einen
# Postgres-16-Server NICHT zurückspielen (`SET transaction_timeout`). Dump
# und Server müssen dieselbe Hauptversion haben. Wer den Server auf eine
# andere Hauptversion hebt, ändert PG_MAJOR hier UND das Image in
# docker-compose.yml gemeinsam (app/migrate.py bricht sonst vor der Migration ab).
#
# Genutzt vom backend/Dockerfile und von der CI (.github/workflows/ci.yml).
set -eu
PG_MAJOR="${1:-16}"
. /etc/os-release

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg
install -d /usr/share/postgresql-common/pgdg
curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get update
apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}"
rm -rf /var/lib/apt/lists/*
pg_dump --version
