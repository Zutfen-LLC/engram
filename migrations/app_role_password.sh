#!/bin/bash
# Engram — set the non-owner application role's password on FIRST BOOT.
#
# Postgres's docker-entrypoint-initdb.d runs *.sql then *.sh files in sorted
# order on an empty data volume only. ``003_app_role_and_force_rls.sql`` (which
# sorts before this file) creates the ``engram_app`` role WITHOUT a password; a
# passwordless role cannot authenticate, so the service cannot connect until one
# is set. This script sets that password from the environment.
#
# It runs ONCE (first boot). To change the app-role password later (existing
# volume), set it directly:
#     psql -U engram -d engram -c "ALTER ROLE engram_app WITH PASSWORD '...';"
# (Editing POSTGRES_APP_PASSWORD after first boot has no effect, exactly like
#  POSTGRES_PASSWORD itself.)
set -eo pipefail

# These are exported by the official postgres image during initdb.
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-postgres}"

APP_USER="${POSTGRES_APP_USER:-engram_app}"
APP_PASSWORD="${POSTGRES_APP_PASSWORD:-engram_app}"

# psql variable substitution: :"var" -> quoted identifier, :'var' -> quoted
# string literal. This keeps the password safe even if it contains SQL-special
# characters.
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
    -v app_user="${APP_USER}" -v app_password="${APP_PASSWORD}" <<'EOSQL'
ALTER ROLE :"app_user" WITH LOGIN PASSWORD :'app_password' NOBYPASSRLS;
EOSQL

echo "engram initdb: set password for application role ${APP_USER}"
