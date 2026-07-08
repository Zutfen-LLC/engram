# Engram — Deployment Guide

This guide takes you from a clean machine to a running, authenticated,
backed-up Engram service. It covers Docker Compose (the supported path),
the first API key, backups, restore, upgrades, and troubleshooting.

Engram is intentionally hosting-agnostic: it runs anywhere Docker Compose (or
equivalent Postgres 16 + pgvector ≥ 0.8 + Python 3.11+) is available. It does
not require any specific host, VPN, or provider.

---

## 1. Requirements

- Docker + Docker Compose v2
- (For bare-metal/non-Compose) PostgreSQL 16 with the **pgvector ≥ 0.8**
  extension, Python ≥ 3.11
- The `pg` client tools (`pg_dump`) on any host that runs backups

> **pgvector ≥ 0.8 is required.** Engram uses `iterative_scan` for filtered
> HNSW queries in semantic recall/search. The `/ready` endpoint fails until a
> sufficient pgvector version is installed, so an old extension fails readiness
> *before* semantic queries 500 at runtime.

---

## 2. Fresh deployment (Docker Compose) — full walkthrough

This is the complete, copy-pasteable path from clone to authenticated recall.

```bash
# 1. Clone
git clone https://github.com/Zutfen-LLC/engram.git
cd engram

# 2. Configure (edit POSTGRES_PASSWORD and others!)
cp .env.example .env
$EDITOR .env

# 3. Start Postgres + Engram
#    On FIRST BOOT (empty data volume) Postgres runs the bundled migrations via
#    docker-entrypoint-initdb.d automatically.
docker compose up -d --build

# 4. Wait for the service to become ready (DB + RLS context + pgvector >= 0.8)
docker compose ps                       # both services should be "healthy"
curl http://localhost:8000/ready        # {"status":"ready","database":"connected","pgvector":"0.8.0"}
```

### 2a. Enable auth and create the first API key

By default `ENGRAM_AUTH_ENABLED=false` — every request runs as the seeded
`default`/`admin` principal. This is fine for local development but **unsafe
for production**. Enable auth and bootstrap the first key:

```bash
# Enable auth (edit .env or set inline)
ENGRAM_AUTH_ENABLED=true docker compose up -d

# Create the first API key for the seeded admin principal.
# The plaintext key is printed EXACTLY ONCE; only a bcrypt hash is stored.
docker compose exec engram-service engram bootstrap-key
```

You can label/scope the key:

```bash
docker compose exec engram-service engram bootstrap-key \
    --label "ops-admin" \
    --scopes read,write,admin,export
```

Save the printed key securely. It is the only time it is shown.

### 2b. Authenticated remember / recall smoke test

```bash
TOKEN="eng_..."   # the key from bootstrap-key

# Remember a memory (requires the 'write' scope)
curl -s -X POST http://localhost:8000/v1/remember \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"content":"Engram deploys with Docker Compose","kind":"fact"}'

# Startup recall (requires the 'read' scope)
curl -s http://localhost:8000/v1/recall \
    -H "Authorization: Bearer $TOKEN"
```

---

## 3. Auth model and key management

- API keys are `eng_<random>` tokens. The database stores **only a bcrypt
  hash** (`key_hash`); the plaintext is shown once at creation.
- Keys resolve to `(tenant_id, principal_id, scopes)`. Scopes are
  `read | write | admin | export`.
- `/health` and `/ready` are always exempt from auth.

### Create additional keys (after the first)

Once auth is enabled and you have an admin key, create more keys via the admin
API (the plaintext is returned once):

```bash
curl -s -X POST http://localhost:8000/v1/admin/api-keys \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"tenant_id":"<tenant-uuid>","principal_id":"<principal-uuid>","scopes":["read","write"],"label":"agent-1"}'
```

### Rotate or revoke a key

Revocation sets `revoked_at` so the key immediately stops authenticating. The
cleanest rotation is: create a new key, update your clients, then revoke the old
one.

```sql
-- Revoke by label (run inside the DB; bypasses RLS as the table owner)
UPDATE api_keys SET revoked_at = now() WHERE label = 'bootstrap';
```

There is no in-place "change the secret" — rotate by issuing a new key and
revoking the old one. The bootstrap key should be rotated to a per-purpose key
once normal admin tooling is available.

---

## 4. Backups

`deploy/backup.sh` creates a timestamped, gzipped `pg_dump` and prunes old
backups. Configure it via `.env` (all optional, safe defaults):

| Variable                 | Default       | Meaning                                            |
| ------------------------ | ------------- | -------------------------------------------------- |
| `BACKUP_DIR`             | `./backups`   | Output directory (created if missing)              |
| `BACKUP_RETENTION_DAYS`  | `14`          | Delete backups older than N days; `0` keeps all    |
| `BACKUP_PGHOST`          | `127.0.0.1`   | `host:port` override for pg_dump (else `PGHOST`)   |
| `POSTGRES_USER` / `POSTGRES_DB` / `POSTGRES_PASSWORD` | from `.env` | DB connection |

Run a backup (requires `pg_dump` on the host):

```bash
./deploy/backup.sh
# Backing up database 'engram' from 127.0.0.1:5432 -> backups/engram-<ts>.sql.gz
```

For an automated schedule, wrap it in cron or systemd and point `BACKUP_DIR`
at durable storage (for example):

```cron
# /etc/cron.d/engram-backup — daily at 03:00, keep 30 days
0 3 * * * engram BACKUP_DIR=/var/backups/engram BACKUP_RETENTION_DAYS=30 /opt/engram/deploy/backup.sh >> /var/log/engram-backup.log 2>&1
```

### Backing up from inside the Postgres container

If the host has no `pg_dump`, run it in the `postgres` container:

```bash
docker compose exec -T postgres pg_dump -U engram -d engram --no-owner --no-privileges \
    | gzip > backups/engram-$(date -u +%Y%m%dT%H%M%SZ).sql.gz
```

---

## 5. Restore

Restoring replaces the target database's contents. **Always restore into a
fresh/empty database** (or stop the service first) to avoid constraint
conflicts.

> **Destructive.** The steps below drop the live database. Before you do,
> take a safety snapshot and confirm the dump you are restoring is intact.

```bash
DUMP="backups/engram-<ts>.sql.gz"

# 0. Safety: take a fresh pre-restore snapshot (in case the restore is wrong)
./deploy/backup.sh

# 1. VERIFY the dump is intact BEFORE touching the live database.
#    gzip -t checks integrity; the size guard rejects an empty/truncated file.
gzip -t "$DUMP" && test "$(stat -c%s "$DUMP")" -gt 50 \
    || { echo "dump failed integrity check — ABORTING before any DROP"; exit 1; }

# 2. Stop the service (keep Postgres running)
docker compose stop engram-service

# 3. Drop & recreate the database (DESTROYS existing data)
docker compose exec postgres psql -U engram -d postgres -c \
    "DROP DATABASE IF EXISTS engram; CREATE DATABASE engram;"

# 4. Restore from the gzipped dump.
#    ON_ERROR_STOP=1 aborts on the first error instead of leaving a half-
#    restored database after the live data was already dropped.
gunzip -c "$DUMP" \
    | docker compose exec -T postgres psql -U engram -d engram -v ON_ERROR_STOP=1

# 5. Restart and verify
docker compose start engram-service
curl http://localhost:8000/ready
```

If step 4 fails, the live database is now empty — restore from the pre-restore
snapshot you took in step 0 (or another known-good dump) before restarting the
service.

### Restore smoke-test checklist

Run these after every restore to confirm the data round-tripped correctly:

```bash
# A. Readiness passes (DB + RLS + pgvector)
curl -fs http://localhost:8000/ready

# B. Row counts match expectations (run in the DB)
docker compose exec postgres psql -U engram -d engram -c \
    "SELECT
        (SELECT count(*) FROM tenants)        AS tenants,
        (SELECT count(*) FROM memory_items)   AS memories,
        (SELECT count(*) FROM api_keys WHERE revoked_at IS NULL) AS active_keys;"

# C. An authenticated recall returns results (use a valid, non-revoked key)
TOKEN="eng_..."
curl -fs http://localhost:8000/v1/recall -H "Authorization: Bearer $TOKEN"
```

---

## 6. Upgrades & migrations

**How migrations run:**

- **First boot (empty data volume):** Postgres's `docker-entrypoint-initdb.d`
  runs every file in `migrations/` automatically, in filename order. This is
  why a fresh `docker compose up` is ready immediately.
- **Existing database:** `initdb.d` does **not** re-run. Use the explicit
  `engram init-db` command to apply new migrations. It tracks applied
  migrations in a `schema_migrations` table, so it is idempotent.

### Standard upgrade procedure

```bash
# 1. Back up first
./deploy/backup.sh

# 2. Pull / rebuild the new image (which may bundle new migrations/)
docker compose build
docker compose up -d

# 3. Apply pending migrations
docker compose exec engram-service engram init-db
```

### Baseline an already-bootstrapped database

If your database was created by Docker's first-boot `initdb.d` (or a manual
`psql -f`), it has no `schema_migrations` tracking table yet. On the first
`engram init-db` run against it, Engram detects the schema already exists and
refuses to blindly re-run `CREATE TABLE`. Baseline it once to record the
current migrations as applied:

```bash
# Safest: baseline up to a specific file the DB is known to already have.
docker compose exec engram-service engram init-db --baseline 002_backfill_indexes.sql
# Future runs apply only newer migrations.
docker compose exec engram-service engram init-db
```

`--baseline` with no value records ALL current migration files as applied
without running them, and prints a warning. Prefer the explicit cutoff form
(`--baseline <filename>`) when a newer migration may have shipped after the
database was bootstrapped — baselining only up to a known-applied file prevents
a newer migration from being silently recorded as applied.

### Idempotency

`engram init-db` is safe to re-run: applied migrations are skipped. Running it
on an up-to-date database reports `Database is up to date`.

---

## 7. Enabling embeddings (semantic recall)

By default `ENGRAM_EMBEDDING_PROVIDER=none` — startup/keyword recall works, but
semantic recall/search is disabled. To enable:

1. Set in `.env`:
   ```
   ENGRAM_EMBEDDING_PROVIDER=openai
   ENGRAM_OPENAI_API_KEY=sk-...
   ```
2. Restart: `docker compose up -d`.
3. Backfill embeddings for existing memories:
   ```bash
   docker compose exec engram-service engram backfill-embeddings
   ```

OpenAI credentials are **not** required for the deployment smoke tests in this
guide. See `engram backfill-embeddings --help` for batching/retry options.

---

## 8. Bare-metal / non-Compose deployment

Without Compose, provide a Postgres 16 + pgvector ≥ 0.8 database and run the
service directly:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Configure
cp .env.example .env && $EDITOR .env   # set ENGRAM_DATABASE_URL explicitly

# Migrate (uses ENGRAM_DATABASE_URL)
engram init-db

# Run
engram serve
```

The `ENGRAM_DATABASE_URL` must use the `postgresql+asyncpg://` scheme.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `/ready` returns 503 with `pgvector: missing` or a low version | The `vector` extension is missing or below 0.8. Use the `pgvector/pgvector:pg16` image (Compose does this) or install pgvector ≥ 0.8 and `CREATE EXTENSION vector;`. |
| `/ready` returns 503 `no_tenant_context` | The seed migration didn't run. On a fresh DB run `engram init-db`; on an existing-but-untracked DB run `engram init-db --baseline`. |
| `engram init-db` errors that `memory_items` already exists | The DB was bootstrapped externally (Docker first-boot). Run `engram init-db --baseline` once. |
| `401 Unauthorized` with auth enabled | Missing/invalid/revoked key. Bootstrap with `engram bootstrap-key`, or revoke+rotate an existing key. |
| Semantic recall returns empty / 500 | Embeddings disabled or not backfilled. Set `ENGRAM_EMBEDDING_PROVIDER=openai` and run `engram backfill-embeddings`. |
| First boot did not migrate | `initdb.d` only runs on an **empty** data volume. If a volume already exists, it is skipped — use `engram init-db`. |
| Backup fails with `pg_dump: command not found` | Install the `pg` client tools on the host, or run `pg_dump` inside the `postgres` container (see §4). |

### Production hardening checklist

- [ ] `POSTGRES_PASSWORD` changed from the default
- [ ] `ENGRAM_AUTH_ENABLED=true` and a non-bootstrap admin key in use
- [ ] Postgres port bound to `127.0.0.1` or not exposed on the public interface
- [ ] `deploy/backup.sh` scheduled with off-host/durable `BACKUP_DIR`
- [ ] A restore smoke test performed against a backup
- [ ] Embeddings enabled (if semantic recall is required)
