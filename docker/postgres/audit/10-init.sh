#!/bin/sh
# Runs once on first start of pg-audit. Passwords come from decrypted SOPS secrets.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -f /docker-entrypoint-initdb.d/sql/audit.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v writer_pw="$(cat /run/secrets/pg_audit_writer_password)" \
    -v reader_pw="$(cat /run/secrets/pg_audit_reader_password)" <<'SQL'
ALTER ROLE audit_writer LOGIN PASSWORD :'writer_pw';
ALTER ROLE audit_reader LOGIN PASSWORD :'reader_pw';
SQL
