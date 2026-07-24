#!/usr/bin/env bash
# control-db-bootstrap: idempotently create the dedicated Control Plane database
# and a least-privilege runtime role, and enforce database-connect isolation
# (ENG-PORTAL-001A, alteration 5).
#
# Runs once (restart: "no") against the shared PostgreSQL cluster as the
# owner/migration role. It:
#   1. Creates the `engram_control` database if absent.
#   2. Creates/updates the `engram_control_app` runtime role
#      (NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, NOSUPERUSER).
#   3. REVOKE CONNECT ON DATABASE ... FROM PUBLIC for both databases, then
#      GRANTs each runtime role CONNECT ONLY to its own database. This is the
#      enforceable boundary: neither runtime role can connect to the other DB.
#   4. Grants engram_control_app only CONNECT + public schema USAGE + SELECT on
#      the readiness tables (control_plane_meta, alembic_version) inside
#      engram_control. It has NO schema CREATE and NO migration authority.
#
# Environment:
#   PGHOST/PGUSER/PGPASSWORD/PGPORT — owner connection (set by compose).
#   POSTGRES_DB          — Core database name (default: engram).
#   ENGRAM_CONTROL_DB    — Control Plane database name (default: engram_control).
#   ENGRAM_CONTROL_APP_USER    — runtime role name (default: engram_control_app).
#   ENGRAM_CONTROL_APP_PASSWORD— runtime role password (REQUIRED).
#   ENGRAM_APP_USER      — Core runtime role name (default: engram_app).

set -euo pipefail

POSTGRES_DB="${POSTGRES_DB:-engram}"
CONTROL_DB="${ENGRAM_CONTROL_DB:-engram_control}"
CONTROL_APP_USER="${ENGRAM_CONTROL_APP_USER:-engram_control_app}"
CONTROL_APP_PASSWORD="${ENGRAM_CONTROL_APP_PASSWORD:?ENGRAM_CONTROL_APP_PASSWORD is required}"
CORE_APP_USER="${ENGRAM_APP_USER:-engram_app}"

echo "[control-db-bootstrap] ensuring dedicated Control Plane database boundary"

psql -v ON_ERROR_STOP=1 --dbname="$POSTGRES_DB" <<SQL
-- 1. Dedicated Control Plane database (idempotent).
SELECT 'CREATE DATABASE $CONTROL_DB OWNER $PGUSER'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$CONTROL_DB')\gexec

-- 2. Runtime role (idempotent create + password refresh). Least privilege: no
--    bypass, no db/role creation, not a superuser.
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$CONTROL_APP_USER') THEN
    CREATE ROLE $CONTROL_APP_USER LOGIN PASSWORD '$CONTROL_APP_PASSWORD'
      NOBYPASSRLS NOCREATEDB NOCREATEROLE NOSUPERUSER;
  END IF;
END \$\$;
ALTER ROLE $CONTROL_APP_USER WITH LOGIN PASSWORD '$CONTROL_APP_PASSWORD'
  NOBYPASSRLS NOCREATEDB NOCREATEROLE NOSUPERUSER;

-- 3. Database-connect isolation. Revoke PUBLIC CONNECT on both databases so
--    no role connects merely by existing; then grant each runtime role CONNECT
--    ONLY to its own database.
REVOKE CONNECT ON DATABASE $POSTGRES_DB FROM PUBLIC;
REVOKE CONNECT ON DATABASE $CONTROL_DB FROM PUBLIC;
GRANT CONNECT ON DATABASE $POSTGRES_DB TO $CORE_APP_USER;
GRANT CONNECT ON DATABASE $CONTROL_DB TO $CONTROL_APP_USER;

-- Belt-and-suspenders: explicitly REVOKE the cross-database connect from each
-- runtime role, so the grant above is the ONLY path.
REVOKE CONNECT ON DATABASE $CONTROL_DB FROM $CORE_APP_USER;
REVOKE CONNECT ON DATABASE $POSTGRES_DB FROM $CONTROL_APP_USER;
SQL

echo "[control-db-bootstrap] granting minimal in-database privileges to $CONTROL_APP_USER"

# In-database privileges must be granted while connected TO the control DB.
psql -v ON_ERROR_STOP=1 --dbname="$CONTROL_DB" <<SQL
-- Runtime role may use the public schema and SELECT only the readiness tables.
-- It must NOT be able to CREATE schema objects or run migrations.
GRANT USAGE ON SCHEMA public TO $CONTROL_APP_USER;
REVOKE CREATE ON SCHEMA public FROM $CONTROL_APP_USER;

-- Readiness reads control_plane_meta (created by the initial migration) and
-- alembic_version (created by Alembic). If the tables do not exist yet, the
-- grants are skipped; the migration step runs next and the runtime role never
-- needs DDL.
DO \$\$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='control_plane_meta') THEN
    GRANT SELECT ON control_plane_meta TO $CONTROL_APP_USER;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='alembic_version') THEN
    GRANT SELECT ON alembic_version TO $CONTROL_APP_USER;
  END IF;
END \$\$;
SQL

echo "[control-db-bootstrap] boundary established"
echo "  - $CONTROL_APP_USER: CONNECT on $CONTROL_DB only; USAGE public; SELECT readiness tables"
echo "  - $CORE_APP_USER: CONNECT on $POSTGRES_DB only"
