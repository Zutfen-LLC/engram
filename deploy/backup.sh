#!/usr/bin/env bash
# shellcheck disable=SC1091
#
# Engram — pg_dump backup script
#
# Creates a timestamped, gzipped pg_dump of the Engram database and prunes
# backups older than BACKUP_RETENTION_DAYS. Safe defaults; override via
# environment or .env.
#
# Usage:
#   ./deploy/backup.sh                     # uses env / .env defaults
#   BACKUP_DIR=/var/backups/engram \
#     BACKUP_RETENTION_DAYS=30 ./deploy/backup.sh
#
# Required: the pg client tools (pg_dump) must be installed on the host, and the
# target Postgres must be reachable. Connection defaults match docker-compose:
#   host 127.0.0.1, port 5432, user/db from POSTGRES_USER/POSTGRES_DB.
#
# To run inside the Postgres container instead:
#   docker compose exec -T postgres pg_dump ...  (see docs/deployment.md)

set -euo pipefail

# ---------------------------------------------------------------------------
# Load .env if present (same file the service / Compose use).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -a
    . "${REPO_ROOT}/.env"
    set +a
fi

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment).
# ---------------------------------------------------------------------------
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

POSTGRES_DB="${POSTGRES_DB:-engram}"
POSTGRES_USER="${POSTGRES_USER:-engram}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-engram}"

# Connection: BACKUP_PGHOST may be "host" or "host:port"; PGHOST/PGPORT are the
# libpq-standard fallbacks. Defaults match docker-compose (127.0.0.1:5432).
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
if [[ -n "${BACKUP_PGHOST:-}" ]]; then
    if [[ "${BACKUP_PGHOST}" == *:* ]]; then
        PGHOST="${BACKUP_PGHOST%%:*}"
        PGPORT="${BACKUP_PGHOST##*:}"
    else
        PGHOST="${BACKUP_PGHOST}"
    fi
fi

# Validate retention is a non-negative integer.
if ! [[ "${BACKUP_RETENTION_DAYS}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: BACKUP_RETENTION_DAYS must be a non-negative integer, got: ${BACKUP_RETENTION_DAYS}" >&2
    exit 2
fi

mkdir -p "${BACKUP_DIR}"

# pg_dump reads PGPASSWORD for password auth.
export PGHOST PGPORT PGPASSWORD="${POSTGRES_PASSWORD}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="${BACKUP_DIR}/engram-${TIMESTAMP}.sql.gz"

echo "Backing up database '${POSTGRES_DB}' from ${PGHOST}:${PGPORT} -> ${FILE}"
# --no-owner / --no-privileges make restores portable across roles/hosts.
pg_dump \
    --no-owner \
    --no-privileges \
    --host="${PGHOST}" \
    --port="${PGPORT}" \
    --username="${POSTGRES_USER}" \
    --dbname="${POSTGRES_DB}" \
    | gzip > "${FILE}"

# Sanity check: refuse to report success on an empty/truncated dump.
BYTES="$(wc -c < "${FILE}")"
if [[ "${BYTES}" -lt 50 ]]; then
    echo "ERROR: backup file is suspiciously small (${BYTES} bytes). Aborting (file kept for inspection)." >&2
    exit 1
fi

echo "Backup complete: ${FILE} (${BYTES} bytes)"

# Retention: prune backups older than N days (0 keeps everything).
if [[ "${BACKUP_RETENTION_DAYS}" -gt 0 ]]; then
    echo "Pruning backups older than ${BACKUP_RETENTION_DAYS} day(s) in ${BACKUP_DIR}"
    find "${BACKUP_DIR}" \
        -maxdepth 1 \
        -type f \
        -name 'engram-*.sql.gz' \
        -mtime +"${BACKUP_RETENTION_DAYS}" \
        -print -delete || true
fi
