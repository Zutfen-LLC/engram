# Dogfood deployment verification — BL-009

**Date:** 2026-07-08
**Commit:** `1bb59f6` (origin/main, includes BL-001 through BL-008)
**Target host:** `engram01` (Debian 13 VM on LAN)
**Stack path:** `/opt/engram`
**Verifier:** second host on Tailscale mesh

> All endpoints, IPs, and credentials below are sanitized for public repo
> storage. Real values are in the operator's secret manager.

---

## Checklist

### Deployment from current main

- [x] Repo cloned/updated on `engram01` at `/opt/engram`
- [x] Updated to `1bb59f6` (current origin/main)
- [x] `.env` created from production-safe values (not `.env.example` defaults)
- [x] `docker compose up -d --build` — both containers running

### Docker build target fix (runbook correction)

- [x] **Bug found:** `docker-compose.yml` had `build: .` with no `target:`,
      causing Docker to build the last stage (`ci`) instead of `runtime`.
      The `ci` stage runs the full test suite as its CMD, never starting uvicorn.
- [x] **Fix applied:** changed to `build: { context: ., target: runtime }`.
      Without this fix, `docker compose up` produces a container that runs
      tests indefinitely and never serves requests.

### Database migration

- [x] Existing DB baselined via `engram init-db --baseline` (records 001 + 002 as applied)
- [x] **Bug found:** `--baseline` recorded 002 as applied without executing it.
      The `idx_memembed_backfill` and `idx_memitems_backfill` indexes were missing.
- [x] **Fix applied:** ran `psql -f migrations/002_backfill_indexes.sql` manually.
      The migration uses `CREATE INDEX IF NOT EXISTS`, making it safe to re-apply.
- [x] `schema_migrations` table now tracks both migrations for future upgrades.

### Auth

- [x] Bootstrap API key created via `engram bootstrap-key` CLI flow (not raw SQL)
- [x] `ENGRAM_AUTH_ENABLED=true` set in `.env`
- [x] Stack restarted; auth enforcement confirmed
- [x] Unauthenticated `/v1/recall` returns 401
- [x] Authenticated requests succeed with Bearer token

### Local health checks (on engram01)

- [x] `GET /health` → `{"status":"ok"}`
- [x] `GET /ready` → `{"status":"ready","database":"connected","pgvector":"0.8.4"}`

### Network verification (from second host over Tailscale)

- [x] `GET http://<redacted-ts-ip>:8000/health` → `{"status":"ok"}`
- [x] `GET http://<redacted-ts-ip>:8000/ready` → `{"status":"ready","database":"connected","pgvector":"0.8.4"}`
- [x] Network path verified over Tailscale mesh (not LAN)

### Authenticated round trip (from second host)

- [x] `POST /v1/remember` with Bearer token → 201 Created, `review_status: active`, `memory_confidence: 0.9`
- [x] `POST /v1/recall` with Bearer token → 200 OK, item returned in working set with scoring breakdown
- [x] Recall scoring verified: `importance=0.50, source_trust=0.90, memory_confidence=0.90, recency=0.00, score=0.555`

### MCP adapter

- [x] `engram_remember` → 201 Created via MCP → SDK → HTTP → FastAPI → DB
- [x] `engram_recall` → 200 OK, both dogfood items returned
- [x] `engram_search` → 200 OK, keyword search found smoke-test item
- [x] Plaintext HTTP warning is expected for Tailscale dogfood (not a production HTTPS deployment)

### Backups

- [x] `deploy/backup.sh` installed at `/usr/local/bin/engram-backup`
- [x] Cron job at 02:15 daily (`/etc/cron.d/engram-backup`)
- [x] Backup directory: `/srv/engram-backups` (14-day retention)
- [x] Manual backup run confirmed: produced valid gzipped SQL dump
- [x] 4 backup files exist (oldest 2026-07-07, latest 2026-07-08)

### Restore smoke test

- [x] Latest backup restored to temp database `engram_restore_test`
- [x] Restored DB contains expected data: 2 memory_items, 1 tenant (Default)
- [x] Schema objects (tables, indexes, policies, RLS) all recreated
- [x] Temp database dropped after verification

### Embeddings

- [x] Intentionally disabled for initial dogfood (`ENGRAM_EMBEDDING_PROVIDER=none`)
- [x] `/ready` confirms pgvector extension is present (0.8.4) for future enablement
- [x] Semantic search endpoints work in keyword/FTS mode without embeddings

### Hermes dogfood profile

- [x] `engram-dogfood` profile exists with SOUL.md pointing at the deployed instance
- [x] Prefill guidance instructs deliberate API usage (no false auto-integration claims)
- [x] `memory-first-recall` skill un-archived (was in `.archive/`, referenced in `always_load`)
- [x] `engram-development` and `hermes-agent` skills in always-load list

---

## Runbook corrections discovered during deployment

1. **docker-compose.yml missing `target: runtime`** — without this, Docker
   builds the `ci` stage (which runs tests, not the server). Fixed in this
   branch.

2. **`engram init-db --baseline` is a trap for 002** — baselining marks ALL
   migration files as applied without executing them. If the DB was bootstrapped
   before a migration shipped, that migration's DDL is silently skipped.
   Operators upgrading an existing instance should either:
   - Baseline only up to the known-applied migration: `engram init-db --baseline 001_init.sql`
   - Then run `engram init-db` to apply remaining migrations, OR
   - Manually `psql -f` any post-bootstrap migrations (safe when they use `IF NOT EXISTS`)

3. **`deploy/backup.sh` backup files are root-owned 600** — the `hermes` user
   cannot read them for restore testing. Restore procedures require sudo.

---

## Live host artifacts (sanitized)

| Artifact | Path |
|----------|------|
| Repo checkout | `/opt/engram` |
| Env file | `/opt/engram/.env` |
| Backup script | `/usr/local/bin/engram-backup` |
| Cron entry | `/etc/cron.d/engram-backup` |
| Backup directory | `/srv/engram-backups/` |
| Docker volume | `engram-db-data` |

## Verification commands (sanitized)

```bash
# Local health (on engram01)
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready

# Network health (from second host)
curl -fsS http://<redacted-ts-ip>:8000/health
curl -fsS http://<redacted-ts-ip>:8000/ready

# Authenticated round trip (from second host)
curl -H "authorization: Bearer <redacted>" \
     -H "content-type: application/json" \
     -d '{"content":"dogfood smoke memory","source_type":"manual"}' \
     http://<redacted-ts-ip>:8000/v1/remember

curl -H "authorization: Bearer <redacted>" \
     -H "content-type: application/json" \
     -d '{"mode":"startup"}' \
     http://<redacted-ts-ip>:8000/v1/recall

# MCP adapter smoke
ENGRAM_BASE_URL=http://<redacted-ts-ip>:8000 \
ENGRAM_API_KEY=<redacted> \
python -c "
import asyncio, sys
sys.path.insert(0, 'adapters/mcp-server')
from engram_mcp import build_server
from mcp.shared.memory import create_connected_server_and_client_session
async def main():
    s = build_server()
    async with create_connected_server_and_client_session(s) as session:
        await session.call_tool('engram_remember', {'content': 'mcp smoke', 'kind': 'fact'})
        print(await session.call_tool('engram_recall', {'mode': 'startup'}))
        print(await session.call_tool('engram_search', {'query': 'smoke'}))
asyncio.run(main())
"

# Backup
sudo /usr/local/bin/engram-backup
ls -lh /srv/engram-backups/

# Restore smoke test
LATEST=$(sudo ls -t /srv/engram-backups/engram-*.sql.gz | head -1)
sudo docker exec engram-db psql -U engram -d postgres -c "CREATE DATABASE engram_restore_test;"
sudo zcat "$LATEST" | sudo docker exec -i engram-db psql -U engram -d engram_restore_test
sudo docker exec engram-db psql -U engram -d engram_restore_test -c "SELECT count(*) FROM memory_items;"
sudo docker exec engram-db psql -U engram -d postgres -c "DROP DATABASE engram_restore_test;"
```
