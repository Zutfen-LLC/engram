# Engram — Deployment Guide

This guide takes you from a clean machine to a running, authenticated,
backed-up Engram service. It covers Docker Compose (the supported path),
the first API key, backups, restore, upgrades, and troubleshooting.

Engram is intentionally hosting-agnostic: it runs anywhere Docker Compose (or
equivalent Postgres 16 + pgvector ≥ 0.8 + Python 3.11+) is available. It does
not require any specific host, VPN, or provider.

> **A real deployment exists.** These steps are not theoretical: Engram is
> running dogfood on a dedicated VM, auth-enabled, reachable over a Tailscale
> mesh, with nightly backups and a restore smoke test. The sanitized
> verification record (deployment, network health, authenticated remember→recall
> round trips, MCP adapter smoke, backup/restore) is in
> [`docs/ops/dogfood-verification.md`](ops/dogfood-verification.md). Hostnames,
> IPs, and credentials are deliberately omitted from the repo.

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

### Database roles (defense-in-depth tenant isolation)

Engram uses two Postgres roles, set in `.env`:

| Role                 | Env vars                              | Used for                                            | RLS         |
| -------------------- | ------------------------------------- | --------------------------------------------------- | ----------- |
| **Owner** (`engram`) | `POSTGRES_OWNER_USER` / `_PASSWORD`   | Migrations (`init-db`), `bootstrap-key`, cross-tenant CLI scans, backups | **bypassed** (superuser) |
| **App** (`engram_app`) | `POSTGRES_APP_USER` / `_PASSWORD`   | All runtime service traffic                         | **enforced** (no ownership, no `BYPASSRLS`) |

The app role is created by `migrations/003_app_role_and_force_rls.sql` with
only DML privileges; its password is set on first boot by
`migrations/app_role_password.sh`. Every tenant-scoped table uses `FORCE ROW
LEVEL SECURITY`, so a forgotten `WHERE tenant_id = ...` in the application
cannot cause a cross-tenant leak when the service connects through the app role.
Migrations and admin commands run as the owner (which bypasses RLS) via
`ENGRAM_OWNER_DATABASE_URL`.

---

## 2. Fresh deployment (Docker Compose) — full walkthrough

This is the complete, copy-pasteable path from clone to authenticated recall.

```bash
# 1. Clone
git clone https://github.com/Zutfen-LLC/engram.git
cd engram

# 2. Configure (set POSTGRES_OWNER_PASSWORD and POSTGRES_APP_PASSWORD at minimum!)
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
# The plaintext key is printed EXACTLY ONCE; only a digest of the secret is stored.
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

Engram authenticates API keys presented as a `Bearer` token in the
`Authorization` header. Keys resolve to `(tenant_id, principal_id, scopes)`.
Scopes are `read | write | admin | export`. `/health` and `/ready` are always
exempt from auth.

**New keys** (created from ENG-AUD-003 onward) use the format
`eng_<key_id>_<secret>`:

- The `<key_id>` is looked up by a unique database index, so verification is
  O(1): a single indexed query plus a constant-time digest check. No bcrypt,
  no full-table scan — this scales to many tenants/keys.
- The high-entropy `<secret>` is verified against a stored **deterministic
  digest** (SHA-256), not a bcrypt hash. This is appropriate because API keys
  are random secrets, not human passwords. The plaintext is shown once at
  creation and never persisted.
- A short in-process cache (default 60s, see
  `ENGRAM_API_KEY_CACHE_TTL_SECONDS`) lets repeated requests with the same key
  skip the lookup. Revocation therefore takes effect after at most that TTL.

**Legacy keys** (`eng_<random>`, bcrypt-hashed) created before this change keep
working through a transitional fallback: because bcrypt salts its hashes, a
legacy key cannot be looked up by value, so verification scans the legacy rows
and bcrypt-checks each one. This path is intentionally transitional and will be
removed in a future cleanup. Rotate legacy keys to the new format when
convenient.

> Do not construct API keys manually. Always generate them with
> `engram bootstrap-key`, `engram generate-key`, or the admin API so the
> `key_id`/digest are produced and stored correctly.

### Create additional keys (after the first)

Once auth is enabled and you have an admin key, create more keys via the admin
API (the plaintext is returned once):

```bash
curl -s -X POST http://localhost:8000/v1/admin/api-keys \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"tenant_id":"<tenant-uuid>","principal_id":"<principal-uuid>","scopes":["read","write"],"label":"agent-1"}'
```

For manual/offline insertion, `engram generate-key` prints a new-format key
along with its `key_id`, `secret_digest`, and `digest_algorithm`.

### Rotate or revoke a key

Revocation sets `revoked_at`. For new-format keys it takes effect immediately on
the next uncached lookup (or after at most the cache TTL). The cleanest rotation
is: create a new key, update your clients, then revoke the old one.

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
| `POSTGRES_OWNER_USER` / `POSTGRES_DB` / `POSTGRES_OWNER_PASSWORD` | from `.env` | Owner-role DB connection for the dump (falls back to `POSTGRES_USER`/`POSTGRES_PASSWORD` for older `.env` files) |

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
#    init-db connects as the OWNER role (ENGRAM_OWNER_DATABASE_URL), which can
#    run DDL. The runtime service stays on the APP role (ENGRAM_DATABASE_URL).
docker compose exec engram-service engram init-db
```

#### Upgrading an existing database to the app-role split (ENG-AUD-002)

A database created before this change connects as the owner at runtime. To move
it onto the enforced-RLS app-role posture:

> **Precondition:** the owner/migration role must be a **superuser** or hold
> `BYPASSRLS`. `FORCE ROW LEVEL SECURITY` makes policies apply to the table
> owner too, so an existing deployment whose owner is a non-privileged role
> (managed Postgres / bare-metal) would have every still-running query
> RLS-filtered the moment 003 applies. If your owner is not a superuser, **stop
> the service first** (`docker compose stop engram-service`) so no traffic runs
> against a FORCE-RLS'd, non-bypassing owner during the window, then complete
> all steps before restarting.

1. Apply migration `003` (creates `engram_app`, grants, `FORCE RLS`):

   ```bash
   # Baseline the pre-003 schema as applied, then apply 003:
   docker compose exec engram-service engram init-db --baseline 002_backfill_indexes.sql
   docker compose exec engram-service engram init-db
   ```

2. Set the app-role password (the password script only runs on first boot, so
   on an existing volume set it directly):

   ```bash
   docker compose exec postgres psql -U engram -d engram -c \
       "ALTER ROLE engram_app WITH LOGIN PASSWORD 'your-app-password' NOBYPASSRLS;"
   ```

3. Set `POSTGRES_APP_PASSWORD` (and the owner vars) in `.env`, then recreate the
   service so it connects as the app role:

   ```bash
   docker compose up -d
   ```

### Baseline an already-bootstrapped database

If your database was created by Docker's first-boot `initdb.d` (or a manual
`psql -f`), it has no `schema_migrations` tracking table yet. On the first
`engram init-db` run against it, Engram detects the schema already exists and
refuses to blindly re-run `CREATE TABLE`. Baseline it once to record the
current migrations as applied:

```bash
# Safest: baseline up to a specific file the DB is known to already have.
# A fresh `docker compose up` runs every file in migrations/ (including 003),
# so baseline at the latest file for a first-boot DB:
docker compose exec engram-service engram init-db --baseline 003_app_role_and_force_rls.sql
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
service directly. You need **two** roles: an owner/migration role and the
non-owner application role (`engram_app`).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Configure (copy .env.example and edit):
cp .env.example .env && $EDITOR .env
#   ENGRAM_DATABASE_URL       -> the app role (postgresql+asyncpg://engram_app:...)
#   ENGRAM_OWNER_DATABASE_URL -> the owner role (postgresql+asyncpg://engram:...)

# Create the app role if it does not exist yet (run once as the owner/superuser):
psql -U engram -d engram -f migrations/003_app_role_and_force_rls.sql
#   then set its password:  ALTER ROLE engram_app WITH LOGIN PASSWORD '...';

# Apply migrations (connects as the owner via ENGRAM_OWNER_DATABASE_URL):
engram init-db

# Run the service (connects as the app role via ENGRAM_DATABASE_URL):
engram serve
```

Both URLs must use the `postgresql+asyncpg://` scheme. Migrations require the
owner role (they run DDL and `FORCE ROW LEVEL SECURITY`); the service requires
the app role (so RLS is enforced). The owner role should be a **superuser** (or
have `BYPASSRLS`) so cross-tenant CLI scans (`promote-proposed`,
`backfill-embeddings`) and `bootstrap-key` work — `FORCE ROW LEVEL SECURITY`
applies to the owner too, and only superusers / `BYPASSRLS` roles bypass it.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `docker compose up` builds then the container runs the test suite forever and never serves `/health` | The `engram-service` build is missing `target: runtime`. The Dockerfile is multi-stage and its final stage is `ci` (whose CMD runs tests, not uvicorn). `docker-compose.yml` sets `build: { context: ., target: runtime }` — keep that if you fork/customize the compose file. |
| `/ready` returns 503 with `pgvector: missing` or a low version | The `vector` extension is missing or below 0.8. Use the `pgvector/pgvector:pg16` image (Compose does this) or install pgvector ≥ 0.8 and `CREATE EXTENSION vector;`. |
| `/ready` returns 503 `no_tenant_context` | The seed migration didn't run. On a fresh DB run `engram init-db`; on an existing-but-untracked DB run `engram init-db --baseline`. |
| `engram init-db` errors that `memory_items` already exists | The DB was bootstrapped externally (Docker first-boot). Run `engram init-db --baseline` once. |
| `401 Unauthorized` with auth enabled | Missing/invalid/revoked key. Bootstrap with `engram bootstrap-key`, or revoke+rotate an existing key. |
| Semantic recall returns empty / 500 | Embeddings disabled or not backfilled. Set `ENGRAM_EMBEDDING_PROVIDER=openai` and run `engram backfill-embeddings`. |
| First boot did not migrate | `initdb.d` only runs on an **empty** data volume. If a volume already exists, it is skipped — use `engram init-db`. |
| Backup fails with `pg_dump: command not found` | Install the `pg` client tools on the host, or run `pg_dump` inside the `postgres` container (see §4). |

### Production hardening checklist

- [ ] `POSTGRES_OWNER_PASSWORD` and `POSTGRES_APP_PASSWORD` both changed from the default
- [ ] Service connects as the non-owner app role (`engram_app`); migrations/admin use the owner role
- [ ] `ENGRAM_AUTH_ENABLED=true` and a non-bootstrap admin key in use
- [ ] Postgres port bound to `127.0.0.1` or not exposed on the public interface
- [ ] `deploy/backup.sh` scheduled with off-host/durable `BACKUP_DIR`
- [ ] A restore smoke test performed against a backup
- [ ] Embeddings enabled (if semantic recall is required)
