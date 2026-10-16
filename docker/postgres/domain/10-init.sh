#!/bin/sh
# Runs once on first start of a domain Postgres instance.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -f /docker-entrypoint-initdb.d/sql/domain.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_pw="$(cat /run/secrets/pg_app_password)" \
    -v agent_pw="$(cat /run/secrets/pg_agent_ro_password)" <<'SQL'
ALTER ROLE app LOGIN PASSWORD :'app_pw';
ALTER ROLE agent_ro LOGIN PASSWORD :'agent_pw';
SQL
