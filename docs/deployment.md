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

Optionally, `ENGRAM_READ_DATABASE_URL` (ENG-AUD-011) points startup recall's
bounded candidate selection at a read replica instead of the primary. Unset
(the default) falls back to `ENGRAM_DATABASE_URL` — there is no bundled
read-replica deployment in this repo; this is a hook for operators who add
one. Writes (promotion, item events, job enqueue, recall telemetry) always
use the primary connection regardless of this setting.

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

# 3. Start Postgres + Engram API + Engram worker
#    On FIRST BOOT (empty data volume) Postgres runs the bundled migrations via
#    docker-entrypoint-initdb.d automatically.
docker compose up -d --build

# 4. Wait for the service to become ready (DB + RLS context + pgvector >= 0.8)
docker compose ps                       # postgres + engram-service should be "healthy"
                                        # engram-worker should be "Up" (no healthcheck)
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
Scopes are `read | write | review | export | admin`, and every caller-facing
route enforces one of them (`admin` is a super-scope that satisfies all the
others — see the README's "API-Key Scopes & Authorization" section for the
full route matrix and the mixed review-transition endpoint's conditional
rule). `/health` and `/ready` are always exempt from auth and scope
enforcement.

New-key issuance (the admin API and `bootstrap-key --scopes`) validates the
requested list against that vocabulary, rejects unknown/misspelled scopes
with `422`, and persists a deduplicated, canonically-ordered list. An
explicit empty scope list is allowed (the key authenticates but can only
reach the exempt health/readiness routes); omitting `scopes` defaults to
`["read", "write"]`.

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

# A human-reviewer key (review queues, activation/rejection, verification):
curl -s -X POST http://localhost:8000/v1/admin/api-keys \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"tenant_id":"<tenant-uuid>","principal_id":"<reviewer-principal-uuid>","scopes":["review"],"label":"human-reviewer"}'
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
3. Inspect the seeded legacy active profile, enqueue its backfill, and run workers:
   ```bash
   docker compose exec engram-service engram embedding-profiles list
   docker compose exec engram-service engram backfill-embeddings \
     --profile openai:text-embedding-3-small:1536
   docker compose exec engram-service engram worker \
     --job-type embedding.generate --max-jobs 1000
   ```

OpenAI credentials are **not** required for the deployment smoke tests in this
guide. See `engram backfill-embeddings --help` for batching/retry options.
For a model change, create a candidate profile, ensure its index, backfill it,
inspect coverage, then activate it. New writes are dual-written during the
backfill and the retired profile remains intact for rollback. See
`docs/embeddings.md` for the complete workflow.

> **ENG-AUD-008 — async write path.** As of this change, `/v1/remember` no
> longer calls the embedding provider inline: it creates the embedding
> placeholder and enqueues an `embedding.generate` job. (When `kind` is omitted
> it also runs rule-based classification only — OpenAI LLM refinement runs
> later as an async `classification.refine` job.) To actually populate
> embeddings / run LLM refinement / run semantic conflict detection, start the
> worker (see "Background worker" below). Exact (content-hash) dedup remains
> synchronous. The service still works without a worker; semantic recall /
> refinement / semantic conflict detection simply lag until jobs are processed.

### 7a. Background worker

The standard Docker Compose stack (`docker compose up -d`) starts a dedicated
`engram-worker` container alongside Postgres and the API. The worker drains the
`jobs` table and runs the off-request-path work: `embedding.generate`,
`conflict.check`, `classification.refine`, `promotion.path_a`,
`retention.sweep`, and (ENG-AUD-011) `recall.telemetry`. It is Postgres-only
(no Redis/Celery/SQS): workers claim with `FOR UPDATE SKIP LOCKED`, retry
failures with exponential backoff, and dead-letter after
`ENGRAM_JOB_MAX_ATTEMPTS`.

Worker logs are visible with:

```bash
docker compose logs -f engram-worker
```

The worker ID in logs defaults to `<hostname>:<pid>` (not literally
`engram-worker`) since the container hostname is generated by Compose.

The worker has no healthcheck. Process existence would not prove queue progress
— a worker can be alive but stalled, or repeatedly failing jobs. The
`restart: unless-stopped` policy handles process crashes, but it does not
detect a live-but-stalled or repeatedly failing worker. Queue depth monitoring,
worker heartbeat, and alerting are planned follow-up work.

The stale-lease recovery (`ENGRAM_JOB_LEASE_STALE_AFTER_SECONDS`, default 300s)
is crash recovery, not graceful shutdown. When Docker sends SIGTERM the process
exits immediately; any in-flight job's lease expires after the stale interval
and is reclaimed by the next worker pass. This means handlers that take longer
than the stale interval risk double-processing. Keep the stale-lease interval
above your longest expected handler duration until lease renewal/fencing is
implemented.

Horizontal scaling: `FOR UPDATE SKIP LOCKED` prevents duplicate claims across
multiple workers. To scale, either remove the container name (if set) or use
`docker compose up -d --scale engram-worker=N`. However, the lease is not
renewed while a handler runs, so keep the stale-lease interval above legitimate
handler duration until lease heartbeat/fencing exists.

The API and worker share the same provider/database configuration via Compose
environment anchors. Provider settings (embedding, classification) and job
tunables are injected at container runtime from `.env` — no image rebuild is
required when changing configuration.

`recall.telemetry` applies startup recall's `last_recalled_at`/`recall_count`/
`startup_recall_count` updates — moved off the synchronous recall path so
recall latency/memory no longer scale with corpus size and the read path can
run through a read-oriented session. Recall itself works correctly without
a worker running; only these counters (and the anti-feedback-loop penalty they
drive) lag until a worker processes the queue. Process just that job type
with:

```bash
docker compose exec engram-service engram worker --job-type recall.telemetry --once
```

Claim/lock bookkeeping runs through the table-owning role (cross-tenant queue
coordination); each job's payload runs through an app-role session scoped to the
job's tenant (RLS-enforced — see ENG-AUD-002).

For ad-hoc / one-shot processing (without the dedicated worker container):

```bash
docker compose exec engram-service engram worker --once
docker compose exec engram-service engram worker --job-type embedding.generate --max-jobs 100
```

For bare-metal / non-Compose deployments, run the worker under systemd:

```ini
# /etc/systemd/system/engram-worker.service
[Unit]
Description=Engram background job worker
After=network-online.target postgresql.service

[Service]
WorkingDirectory=/opt/engram
EnvironmentFile=/opt/engram/.env
ExecStart=/opt/engram/.venv/bin/engram worker
Restart=always
User=engram

[Install]
WantedBy=multi-user.target
```

Or via cron (process the queue every minute):

```cronfile
* * * * * engram /opt/engram/.venv/bin/engram worker --max-jobs 100 >> /var/log/engram-worker.log 2>&1
```

Flags: `--once`, `--poll-interval <s>`, `--job-type <t>` (repeatable),
`--max-jobs <n>`, `--worker-id <id>`. The worker exits nonzero only on fatal
setup errors — ordinary job failures retry/dead-letter without stopping the
loop.

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

Hermes lifecycle deployments should set `ENGRAM_HOOKS_STORE_THRESHOLD` (default `0.65`) to
the minimum retention confidence for remembering a `retain` candidate as proposed.
`ENGRAM_HOOKS_PROMOTE_THRESHOLD` is accepted as a deprecated fallback for one release.

Promotion Path A v2 adds `tenant_config.auto_promote_evidence_enabled` and
`auto_promote_evidence_threshold` (default `0.70`). Migration 016 leaves existing
tenants disabled; newly-created tenants receive an enabled config row. Operators should
run `engram promote-proposed --dry-run` before explicitly enabling an existing tenant.
Dry-run applies the same policy admission as execution, rolls its transaction back, and prints
per-lane totals plus stable blocker counts and bounded candidate details without memory content.
Qualifying bound evidence also creates a delayed `promotion.path_a` worker job, so the
worker must be running for scheduled promotion; startup recall and manual CLI/admin runs
remain supported triggers.

---

## 9. Migration 021 deployment and rollback

Migration `021_scope_write_defaults.sql` is a coordinated-maintenance schema
transition. It is intentionally not compatible with mixed old and new Engram
writers: old code can insert `visibility='workspace'` with a NULL
`workspace_id`, while the migrated schema correctly rejects that invalid shape.
No compatibility trigger silently rewrites it.

Deployment procedure:

1. Stop or drain every Engram service and worker capable of writing memory.
2. Deploy the new code and apply migration 021 as the database owner.
3. Restart services and workers only after the migration succeeds.
4. Never run old application code against the migrated schema.

Application-only rollback after migration 021 is unsupported. Reverting the
application also requires an explicit database rollback plan that accounts for
the private default, validated workspace-visibility CHECK, normalized audit
rows, and restrictive workspace foreign key. Take and verify a database backup
before the maintenance window. The normal recovery choice is to fix-forward;
do not restart an old fleet against the migrated database.

If rollback is unavoidable, keep every writer stopped, restore the verified
pre-021 database backup (or apply an operator-reviewed reverse migration with
equivalent restoration semantics), verify the restored schema and data as the
owner and application roles, then deploy the matching old application revision
before restarting any process. Do not merely drop the CHECK: normalized rows
and their audit events, the column default, and the workspace FK policy must be
handled as one database rollback. There is no supported code-only shortcut.

After migration 021, a workspace referenced by any memory cannot be deleted.
Inspect its memories and explicitly re-scope, archive, export/delete, or
otherwise resolve them first; deletion succeeds only when no memory reference
remains. This is a deliberate foreign-key restriction, not an incidental CHECK
failure.

---

## 10. Migrations 024 + 025 — profile write context and execution authority (ENG-SCOPE-002C)

Migrations `024_profile_write_context.sql` and `025_candidate_execution_context.sql`
are a coordinated rollout. They split candidate provenance into two immutable
layers:

- `candidate_ingests` — the **origin** context under which a candidate entered
  the pipeline (set at classify time);
- `candidate_ingest_executions` (migration 025) — the **execution authority**
  under which `/v1/remember` actually accepted/executed it, durable on the first
  successful remember.

Workers (conflict-check, promotion, deduplication) reconstruct the
**execution authority** when present and use it as their cross-item boundary.
For a `memory-context-v2` ingest whose execution authority is unavailable,
cross-item worker behavior **fails closed**: conflict checking and targeted
promotion intentionally skip before any cross-item scan, lock, or mutation, and
the job is treated as a completed fail-closed no-op rather than retried toward
a dead letter. This covers queued work produced by a pre-025 or partially
rolled-out instance, and any missing/corrupt execution row; operators may
re-submit the underlying work through a current 002C API if processing is still
desired. Audit events for such a row record neutral `internal-system-v1`
provenance, never `legacy-unprofiled-v0` and never a fabricated profile/API-key
identity. Legacy (`legacy-unprofiled-v0`) ingests with no execution row retain
compatibility behavior.

### Rollout order (strict)

```
022 → 023 → 024 → 025
       → deploy all API instances
       → deploy all worker instances
       → account for pre-002C queued work
       → verify fleet version
       → declare profile-bound keys read/write isolated
```

**Application code that references `candidate_ingest_executions` must never be
deployed before migration 025.** Deploying the 002C API before 025 exists will
fail on every remember execution that pins the execution context.

### Procedure

1. Back up first (`./deploy/backup.sh`).
2. Apply migrations through 025 as the database owner
   (`engram init-db`, which connects via `ENGRAM_OWNER_DATABASE_URL`).
3. Deploy **all** API instances, then **all** worker instances. A mixed API
   fleet where some instances pin execution rows and others do not is not a
   supported steady state.
4. Drain or explicitly classify/re-submit pre-002C queued work. Jobs enqueued
   by a pre-025 instance may reference a `memory-context-v2` ingest that has no
   execution row; those cross-item jobs will skip under the fail-closed rule
   rather than scan under the origin profile. Re-submitting the work through a
   002C API records the execution authority durably.
5. Verify every API and worker instance reports the 002C version before
   declaring profile-bound credentials read/write isolated.

### Mixed-version and rollback behavior

- **Rolling application code back after 025 is applied** widens write behavior
  back to pre-002C semantics even though the schema remains. Profile-bound
  credentials must be **disabled/revoked** (or the loss of enforcement
  explicitly accepted) before running the old fleet against the migrated
  database. Execution rows already recorded are historical provenance — they
  are **not** proof that rolled-back code continues to enforce profiles.
- Schema 025 may safely remain during an application rollback (it is additive:
  a new table with RLS, least-privilege grants, and no change to existing
  columns). There is no supported code-only shortcut that restores enforcement
  once the rolled-back fleet is running; the only safe states are "fix-forward"
  or "old fleet with profile-bound keys revoked."
- Do not run a pre-025 API against a database where 025 has not been applied,
  and do not run a 002C API against a database where 025 has not been applied.
  Both are unsupported; the latter fails on remember.

### Idempotency

Both migrations are safe to re-apply: every object is guarded (`IF NOT EXISTS`
or `DO $$ IF NOT EXISTS ...`), the 025 RLS policy is recreated idempotently
(drop-if-exists + create), and the redundant `principal_id` column (present in
an earlier revision of 025) is dropped idempotently. `engram init-db` skips
already-applied migrations via the `schema_migrations` table.

---

## 10a. Migration 026 — durable context receipt storage substrate (ENG-CONTEXT-002A)

Migration `026_context_receipts.sql` is an **additive, storage-only** slice that
introduces the `context_receipts` table: the immutable, tenant/principal-
isolated persistence envelope for the deterministic `ContextManifestV1`
(ENG-CONTEXT-001). It is the foundation for the Engram Context Ledger.

### What it adds

- `context_receipts` table — receipt ID, `recall_log_id` (one-to-one with
  `recall_logs`), tenant/principal ownership, envelope protocol markers, the
  stored manifest JSONB, `manifest_hash`, `packet_hash`, `retention_expires_at`,
  and `created_at`.
- A composite unique identity on `recall_logs (tenant_id, principal_id, id)` and
  a composite foreign key
  `context_receipts(tenant_id, principal_id, recall_log_id) →
  recall_logs(tenant_id, principal_id, id)` with `ON DELETE RESTRICT
  DEFERRABLE INITIALLY DEFERRED`.
- Protocol CHECK constraints (schema/mode/hash-format/JSON-shape/envelope-
  agreement/ownership/packet/no-`manifest_hash`-field/retention).
- FORCE RLS requiring **both** tenant and principal GUCs.
- App-role `SELECT`/`INSERT` only; `UPDATE`/`DELETE` explicitly revoked
  (migration 003's default privileges would otherwise grant them).
- Indexes: unique `recall_log_id`, principal timeline, tenant manifest-hash
  lookup, and a partial retention-sweep index.

### What it does NOT add

This slice is storage-only. It does **not**:

- create receipts during a production recall (dark writes land in
  ENG-CONTEXT-002B);
- add receipt IDs or hashes to any API response;
- add inspect/verify/diff/drift/exposure/impact endpoints (ENG-CONTEXT-003);
- add SDK, MCP, or Hermes receipt surfaces;
- delete expired rows or run a retention worker;
- introduce tenant retention configuration;
- store raw memory content, raw `working_set`, raw query text, or a second copy
  of the canonical JSON.

### Migration order

```
001 → ... → 025 → 026
```

### Deployment order for this slice

```
migration 026
  → verify constraints / RLS / privileges
  → deploy model / repository code
  → no behavioral change expected
```

No behavioral change is expected because no production path writes receipts yet.
The new table is additive and the repository module has no FastAPI dependencies
and is not wired into any route.

### Rollback guidance

- Application code may be rolled back while leaving migration 026 in place.
- Do **not** drop the table if receipts have been inserted — receipts are
  immutable retention evidence. (This slice itself creates no production
  receipts, so on a pre-002B deployment the table is empty and the rollback is
  trivially safe.)
- Rolling back does not require rolling back migration 026.

### Idempotency

Migration 026 is safe to re-apply: `CREATE TABLE IF NOT EXISTS`,
`CREATE [UNIQUE] INDEX IF NOT EXISTS`, guarded constraint creation through
`pg_constraint`, guarded policy recreation through `pg_policies`, safe
two-argument `current_setting(..., true)`, explicit FORCE RLS, and explicit
grants/revocations. Reapplying does not duplicate constraints/policies, rewrite
rows, weaken privileges, or lose FORCE RLS.

### Verification

After applying migration 026, verify (as the owner):

- `context_receipts` exists with FORCE RLS active;
- the app role (`engram_app`) has `SELECT`/`INSERT` but **not** `UPDATE`/`DELETE`;
- the composite recall-log FK and the `uq_recall_logs_tenant_principal_id`
  unique identity exist;
- a row whose manifest subject tenant/principal disagrees with the envelope
  columns is rejected;
- a second receipt for one recall log is rejected;
- recall-log deletion is restricted while its receipt remains.

The real-PostgreSQL proof suite
(`tests/test_context_receipts_postgres.py`,
`tests/test_context_receipt_store_postgres.py`) covers all of the above against
the non-owner app role.

---

## 10b. Startup context-receipt dark writes (ENG-CONTEXT-002B)

ENG-CONTEXT-002B wires the canonical `ContextManifestV1` (ENG-CONTEXT-001) and
the durable `context_receipts` storage substrate (ENG-CONTEXT-002A) into the
production startup-recall path as a **default-off, fail-open** dark write. No
migration is added by this slice — it uses the migration 026 table.

### Configuration (API-only)

| Setting | Env var | Default |
| --- | --- | --- |
| `context_receipt_dark_write_enabled` | `ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED` | `false` |
| `context_receipt_dark_write_timeout_seconds` | `ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS` | `1.0` |

These are API-only settings — they are **not** propagated to the worker. The
timeout must be strictly positive; an invalid (`<=0`) value fails settings
load with no silent negative-to-positive coercion.

### Disabled behavior (default)

When `ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=false`:

- no executed-result provenance is parsed;
- no manifest is built;
- no context-receipt database session is opened;
- no receipt query is executed;
- no dark-write telemetry event is emitted;
- no receipt-related structured log is emitted;
- startup and semantic recall behavior is **unchanged**.

The route's outer guard checks the flag before any receipt-specific parsing
or validation, so a disabled deployment is behaviorally identical to a
pre-002B deployment. The orchestrator's own disabled check is retained as
defense in depth.

### Enabled behavior (startup only)

When enabled, a successful `POST /v1/recall` with `mode=startup` additionally:

1. executes startup recall normally and finalizes one `RecallResponse` object;
2. parses the required executed-result provenance from the raw startup
   engine result (no inferred defaults; public `RecallResponse` defaults
   never feed the manifest) — a missing/malformed key fails open with
   `failure_stage=build_decision_context` and no receipt;
3. builds `ContextManifestV1` from that finalized response and the actual
   resolved execution context (no re-reads of mutable memory rows);
4. opens a dedicated, short-lived, non-owner app-role session
   (`engram.db.async_session_factory`) with tenant/principal RLS applied;
5. persists one immutable `context_receipts` row linked to the committed
   recall log (idempotent on retry);
6. forces a database reload of the stored JSONB through PostgreSQL;
7. recanonicalizes and verifies the reloaded manifest and hashes;
8. commits the receipt **only after** verification succeeds;
9. records a bounded `context_receipt.dark_write` usage event **best-effort**
   (when usage telemetry is enabled and sufficient deadline remains);
10. returns the **original** `RecallResponse` unchanged.

All of steps 2–9 run under one monotonic deadline
(`ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS`) that starts before
executed-result validation; each awaited stage runs against the remaining
deadline. When the primary operation exhausts the deadline, the wrapper
records `telemetry_status=skipped_deadline` in the structured log and writes
**no** usage-event row.

Semantic recall (`mode=semantic`) **never** invokes the dark write. No
semantic Context Manifest support is implemented in this slice.

### Fail-open contract

All ordinary manifest, database, integrity, telemetry, and timeout failures
are fail-open — including a `build_decision_context` failure from missing or
malformed executed-result provenance:

- the route still returns the exact successful startup `RecallResponse`;
- a receipt failure never fails the recall request, modifies the response,
  deletes or rolls back the already-committed recall log, poisons the
  caller's request session, or suppresses retrieval-success telemetry;
- asyncio cancellation is **not** swallowed — it propagates normally;
- no raw content, `working_set`, query text, manifest JSON, canonical JSON,
  or exception messages are logged or stored in usage metadata — only
  bounded aggregate metadata and the exception *type*.

### Observability

**Structured logs are authoritative for every enabled attempt.** Exactly
one bounded structured log per enabled attempt carries `event`, `status`,
`tenant_id`, `principal_id`, `mode=startup`, `latency_ms`, `item_count`,
`failure_stage`, `exception_type`, `verification_status`, and
`telemetry_status`. Usage events are best-effort: a hard timeout that
exhausts the total deadline may have **no** usage-event row
(`telemetry_status=skipped_deadline`). Correlate gap calculations with both
sources.

### Dogfood rollout

See [`docs/ops/context-receipt-dark-writes.md`](ops/context-receipt-dark-writes.md)
for the staged rollout (deploy disabled → enable on dogfood → verify →
disable safely), owner-role diagnostic queries, and p50/p95 measurement
steps. Receipt failures are expected to be visible but fail-open during the
dark-write phase.

### Compose propagation

`docker-compose.yml` explicitly passes both
`ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED` and
`ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS` to `engram-service` only
(API-only). They are not added to the shared `x-env` anchor, so the worker
container never sees them.

---

## 11. Memory profiles (control plane)

Memory profiles are reusable, tenant-scoped policy identities. They are created and revised by an
`admin`-scoped credential. ENG-SCOPE-002B/002C enforce the active revision as a narrowing boundary
on every MemoryItem-backed read, prospective write, and mutation; profiles never grant workspace
membership, principal authority, review authority, or an API scope.

Create a profile with safe private-only defaults:

```sh
curl -X POST "$ENGRAM_URL/v1/memory-profiles" -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'content-type: application/json' \
  -d '{"name":"Support","slug":"engram-support","reason":"initial policy","policy":{}}'
```

Issue a bound key only at creation (`POST /v1/admin/api-keys` or `POST /v1/agents`) by adding
`"memory_profile_id":"<profile UUID>"`. Existing keys remain unprofiled. The one-time key
response reports the selected stable profile and its revision at issuance; `GET /whoami` reports
the key ID and current profile identity, not its full policy. A profile revision becomes current for
all bound keys on their next request. Disabling a profile makes bound keys return the same generic
401 as invalid credentials; re-enable restores them. There is no bind/rebind endpoint—rotate a key
to change profiles. `engram bootstrap-key --memory-profile <slug-or-uuid>` is available after a
profile exists in the default tenant.

Profile administration is deliberately absent from the MCP server. No memory request accepts a
profile selector/header/query parameter.

A profile-bound request resolves one immutable `ResolvedMemoryContext` on the primary request
session. Private, tenant, and public items require their corresponding include flag. Every item
with a non-NULL workspace association additionally requires a matching `can_read=true` grant;
workspace-visible items also retain the existing principal-membership requirement. A grant never
widens an item's audience. `admin` scope does not bypass these data-plane rules. Unknown,
non-member, and profile-ungranted explicit workspaces return the same empty collection/recall or
item 404 behavior and never fall back to an unscoped read. Existing unprofiled keys and
auth-disabled development retain compatibility behavior.

Recall audit rows record the exact profile and revision enforced by the request. Search telemetry
records only the context/profile identifiers and profile version, never policy JSON, workspace
grants, query text, or memory content. When a profile or explicit workspace makes a semantic set
impossible, Engram skips the embedding provider and reports `not_attempted`.

For writes, fully omitted visibility/workspace uses the revision's exact default. Explicit tenant
and public writes require their `allow_*_write` flags. Private/no-workspace creation remains
available to a credential with API `write` scope even when `include_private=false`; that read flag
does not create a second write-scope switch. Every workspace association requires `can_write=true`
plus established membership (or the existing admin membership bypass). Existing-item mutation
requires both profile read and write eligibility, and profile denial uses the normal non-disclosing
missing-item response. `admin` never bypasses profile policy.

Classify stores the classify-time **origin** context on its candidate ingest; remember re-authorizes
the final scope and pins the **execution authority** durably (migration 025). Cross-item workers
reconstruct the remember-time execution authority only — never the candidate-origin revision — so a
narrower remember execution is honored even when classify ran under a broad profile. For a
`memory-context-v2` ingest whose execution authority is unavailable (a pre-025 queued job, a
deleted/corrupt execution row), cross-item worker behavior fails closed: conflict checking and
targeted promotion intentionally skip before any cross-item scan, lock, or mutation, and the job is
treated as a completed fail-closed no-op rather than retried toward a dead letter. Audit events for
such a row record neutral `internal-system-v1` provenance (never `legacy-unprofiled-v0`, and never a
fabricated profile/API-key identity); genuine legacy ingests keep `legacy-unprofiled-v0`. Operators
may re-submit the underlying work through a current 002C API if processing is still desired. Trusted
scheduled maintenance remains explicitly internal and is not an API-key profile sandbox.

### Migration 022 rollout and rollback

Migration 022 is additive: it adds profile tables and a nullable `api_keys.memory_profile_id`, so
existing credentials remain compatible and no data backfill is required. Apply it before deploying
profile-aware application code. Roll back application code only after confirming it tolerates the
new nullable column; do not remove the schema while bound keys exist. The safe rollback is to leave
the additive schema in place and stop issuing bound keys, or restore a verified pre-migration backup
if complete removal is required.

### Migration 023 profile-read rollout and rollback

Migration 023 is additive. It adds nullable profile/revision provenance and a non-null context
version to `recall_logs`, labeling historical rows `legacy-unprofiled-v0`. Apply it before the
002B application code.

Mixed old/new service instances are not a valid enforcement deployment: an old instance would
retain pre-002B reads. Roll out in this order:

1. Apply migration 023.
2. Drain or replace every old service instance.
3. Verify every serving instance runs 002B code.
4. Only then treat profile-bound keys as read-isolated.

Rolling application code back from 002B widens reads for profile-bound keys and is not
security-neutral. Before rollback, disable or revoke every bound key, or explicitly accept the
loss of read enforcement. The additive migration may remain in place.

### Profile-write rollout and rollback (migrations 024 + 025)

Migration 024 adds immutable context provenance to candidate ingests and item events; migration
025 adds the durable remember-time execution authority (`candidate_ingest_executions`) that
workers reconstruct for cross-item behavior. Historical and omitted rows remain
`legacy-unprofiled-v0`; current caller work records `memory-context-v2`, and trusted maintenance
records `internal-system-v1`.

Apply `022 → 023 → 024 → 025` before deploying 002C — see [§10](#10-migrations-024--025--profile-write-context-and-execution-authority-eng-scope-002c)
for the full rollout order, mixed-version behavior, and rollback guidance. The 002C API must not
be deployed before 025 exists (it reads/writes `candidate_ingest_executions` on every successful
remember). Drain or replace every pre-002C API instance and drain, complete, or explicitly
classify queued pre-002C candidate jobs. Only after every API and worker runs 002C may
profile-bound credentials be described as complete read/write sandboxes. Mixed old/new instances
are not an enforcement deployment.

Application rollback widens writes even if migrations 024/025 remain installed. Before rollback,
disable or revoke profile-bound keys, or explicitly accept the loss of write enforcement.
Recorded execution rows are historical provenance, not proof that rolled-back code keeps
enforcing profiles. Direct trusted operator database maintenance is outside the API-key profile
contract.

## 12. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `docker compose up` builds then the container runs the test suite forever and never serves `/health` | The `engram-service` build is missing `target: runtime`. The Dockerfile is multi-stage and its final stage is `ci` (whose CMD runs tests, not uvicorn). `docker-compose.yml` sets `build: { context: ., target: runtime }` — keep that if you fork/customize the compose file. |
| `/ready` returns 503 with `pgvector: missing` or a low version | The `vector` extension is missing or below 0.8. Use the `pgvector/pgvector:pg16` image (Compose does this) or install pgvector ≥ 0.8 and `CREATE EXTENSION vector;`. |
| `/ready` returns 503 `no_tenant_context` | The seed migration didn't run. On a fresh DB run `engram init-db`; on an existing-but-untracked DB run `engram init-db --baseline`. |
| `engram init-db` errors that `memory_items` already exists | The DB was bootstrapped externally (Docker first-boot). Run `engram init-db --baseline` once. |
| `401 Unauthorized` with auth enabled | Missing/invalid/revoked key. Bootstrap with `engram bootstrap-key`, or revoke+rotate an existing key. |
| `403 Forbidden` with `"Requires scope: ..."` | The key authenticated fine but lacks a required scope for that route. Issue a new key with the needed scope (see "API-Key Scopes & Authorization" in the README), or use an `admin`-scoped key, which satisfies every scope. |
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
## Feedback integrity and rate limit

Feedback is transactionally canonicalized: each principal has one current
verdict per item while superseded verdicts remain as history. Each active
tenant configuration has `feedback_daily_limit` (default `500`, range
1–100000), counted per principal over UTC calendar days. The database locks
the item and then the principal, so the limit and importance contribution stay
correct across processes and across multiple API keys. Exhaustion returns
HTTP `429` with `Retry-After` set to the next UTC midnight. This setting is
currently managed directly in versioned `tenant_config`; there is no public
configuration endpoint.
