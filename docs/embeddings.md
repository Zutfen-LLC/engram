# Embeddings

> **Implementation status (BL-006):** the embedding pipeline (write-path
> generation, semantic search, conflict similarity) and the
> `engram backfill-embeddings` command are **implemented** and verified with a
> **mocked** provider. The **live OpenAI path has not been recorded-verified** —
> the checklist at the bottom of this file still has blank `Observed:` fields and
> must be run before claiming live embeddings are verified. The dogfood
> deployment intentionally runs with `ENGRAM_EMBEDDING_PROVIDER=none`; keyword
> (FTS) recall and search work without embeddings, while semantic recall/search
> and write-time conflict detection stay inert until a provider is configured.

Engram stores embeddings in a separate `memory_embeddings` table keyed by
`(memory_item_id, embedding_model)` with a denormalized `tenant_id` for RLS.
This keeps the vector column out of the hot read path for non-semantic queries
and lets a model change be new rows, not a migration.

- **Provider:** `ENGRAM_EMBEDDING_PROVIDER` — `none` (defer, the default) or
  `openai`.
- **Model:** `text-embedding-3-small` (1536-dim). The model name is a single
  source of truth at `engram.embeddings.EMBEDDING_MODEL`; the write path,
  semantic search, and conflict detection all key off it.
- **API key:** `ENGRAM_OPENAI_API_KEY` when the provider is `openai`. Never
  commit a key or print one in logs.

## Enabling embeddings

Set the provider and key in the environment (e.g. in your `.env` or deploy
config), then restart the service:

```bash
ENGRAM_EMBEDDING_PROVIDER=openai
ENGRAM_OPENAI_API_KEY=sk-...
```

Once enabled, every `POST /v1/remember` creates a `memory_embeddings` row,
generates the vector, and marks it `ready`. Items written **before** the
provider was enabled (or whose generation crashed) have either no embedding
row or a `pending`/`failed` row with no vector. The backfill command
populates that backlog.

### Status vocabulary

`memory_embeddings.embedding_status` uses the live application vocabulary:

| Status    | Meaning                                                  |
|-----------|----------------------------------------------------------|
| `pending` | Row created; vector not yet generated.                  |
| `ready`   | Vector populated; eligible for semantic search/recall.   |
| `failed`  | Generation errored or returned no vector. Not retried by default. |

> The migration comment on the column lists `complete | failed | stale` and
> the column `DEFAULT` is `'complete'`, but there is no CHECK constraint and
> the application writes only `pending`/`ready`/`failed`. Semantic search and
> recall filter on `embedding IS NOT NULL`, so the status string does not
> gate retrieval — a populated vector is used regardless of status.

## `engram backfill-embeddings`

Populate `pending`/`missing` embeddings for the configured model, across all
tenants (or one with `--tenant`). Idempotent and safe to rerun.

```bash
# 1. See how much work there is (no writes). Scans even when the provider
#    is 'none', so you can size the backlog before configuring a key.
engram backfill-embeddings --dry-run

# 2. Small live batch first.
ENGRAM_EMBEDDING_PROVIDER=openai ENGRAM_OPENAI_API_KEY=$KEY \
  engram backfill-embeddings --limit 10

# 3. Full run.
ENGRAM_EMBEDDING_PROVIDER=openai ENGRAM_OPENAI_API_KEY=$KEY \
  engram backfill-embeddings
```

### Flags

| Flag            | Default | Description                                                        |
|-----------------|---------|--------------------------------------------------------------------|
| `--tenant ID`   | all     | Restrict to one tenant.                                            |
| `--limit N`     | none    | Cap total candidates per tenant. The budget is shared across the pending and missing-row populations (pending is served first, the rest goes to missing rows). |
| `--batch-size N`| `100`   | Items embedded per provider call / transaction (capped at the provider's per-request limit of 2048). A failed call only fails its own batch; use `--batch-size 1` to isolate a single bad item. |
| `--dry-run`     | off     | Report planned work without writing (returns `0`).                |
| `--fail-fast`   | off     | Abort on the first failure instead of marking the row `failed`.   |
| `--retry-failed`| off     | Re-attempt rows previously marked `failed`.                       |

### Batching, memory, and concurrency

- Candidates are **streamed one batch at a time** (keyset pagination), so both
  the number of rows held in memory and the size of one transaction stay
  bounded by `--batch-size` — even when `--limit` is unset. Processed rows are
  expunged from the session after each batch (so their vectors don't
  accumulate), and summary counts (`scanned`, `would_*`, `skipped_*`) come from
  cheap `count(*)` queries, so a `--dry-run` never loads the candidate rows.
- Each batch is one provider call, flushed and committed as it completes, so a
  failed call only rolls back its own batch (and completed batches persist).
- Overlapping runs divide work rather than collide: pending rows are fetched
  `FOR UPDATE SKIP LOCKED`, and missing-row inserts tolerate the unique
  constraint, so a second run started while another is in flight skips the
  rows it's processing instead of double-embedding or erroring.

> **Large backlogs:** migration `002_backfill_indexes.sql` adds the btree
> indexes the streaming/count queries rely on (`memory_embeddings(tenant_id,
> embedding_model, embedded_at, id)` and a partial `memory_items` index).
> Apply it (`psql -f migrations/002_backfill_indexes.sql`) before backfilling a
> large backlog — without it each streamed page is a full scan.

### What gets backfilled

- **Existing rows** for the configured model that are `pending`, or `ready`
  but missing their vector (an anomaly), or at the migration-default status
  with no vector.
- **Items** (`valid_to IS NULL`) with **no** embedding row for the configured
  model — a row is created then embedded.

Any row that already has a populated vector is counted as `skipped_ready`
(regardless of its status string, including the legacy `complete` default) and
never touched, so a repeat run reports nothing left to do.

### Failed rows are skipped by default

A row that fails embedding is marked `failed` and is **not** retried on
subsequent runs by default — it's counted as `skipped_failed`. This prevents a
broken provider or piece of content from creating an endless failure loop on
every run. Re-attempt them explicitly:

```bash
engram backfill-embeddings --retry-failed
```

Per-item failures are logged at `WARNING` and listed in the result's
`failed_items`; the batch continues unless `--fail-fast` is set.

### Provider disabled

- `--dry-run` with the provider disabled still scans and reports the backlog
  (no writes) and returns `0`.
- A **real** run with the provider disabled writes nothing, prints actionable
  guidance, and returns a **nonzero** exit code (`2`) so cron/callers can tell
  the backfill was a no-op. Set `ENGRAM_EMBEDDING_PROVIDER` and the API key
  before running for real.

### Tenant safety

New embedding rows read `tenant_id` from the parent `memory_item`, satisfying
the composite FK `(memory_item_id, tenant_id) → memory_items(id, tenant_id)`.
Every query filters `tenant_id` explicitly, so the command is correct under
RLS too (it connects as the table-owning role by default, which bypasses RLS,
and filters by explicit tenant id).

---

## Live OpenAI verification checklist

CI runs entirely against mocked embeddings. Before declaring embeddings
shipped, run this checklist once against a live OpenAI-backed deployment and
record the results below (or in the PR). **Redact any key material before
committing** — never paste an API key, a request body containing a key, or
raw provider credentials.

Setup: a running Postgres 16 + pgvector ≥ 0.8 with `migrations/001_init.sql`
applied, and:

```bash
export ENGRAM_EMBEDDING_PROVIDER=openai
export ENGRAM_OPENAI_API_KEY=sk-...   # do not record this value
export ENGRAM_DATABASE_URL=postgresql+asyncpg://engram:engram@localhost:5432/engram
```

### 1. `remember` creates a `ready` embedding

```bash
curl -sS -X POST localhost:8000/v1/remember \
  -H 'content-type: application/json' \
  -d '{"content":"live verify: the deployment region is us-east-1","source_type":"manual"}'
# then check the row:
psql "$DATABASE_URL" -c "SELECT embedding_status, embedding_dim, \
  embedding IS NOT NULL AS has_vector FROM memory_embeddings \
  ORDER BY embedded_at DESC LIMIT 1;"
```

- Expected: `embedding_status=ready`, `embedding_dim=1536`, `has_vector=t`.
- Observed: _

### 2. Backfill populates pending/missing embeddings

Create a pending/missing backlog (write a couple items with the provider
temporarily `none`, or leave rows from before enablement), then:

```bash
engram backfill-embeddings --dry-run
ENGRAM_EMBEDDING_PROVIDER=openai ENGRAM_OPENAI_API_KEY=$KEY \
  engram backfill-embeddings --limit 10
```

- Expected: dry-run reports the backlog; real run moves rows to `ready`; a
  second run reports `scanned=0`.
- Observed: _

### 3. Semantic search/recall works over real vectors

```bash
curl -sS -X POST localhost:8000/v1/search \
  -H 'content-type: application/json' \
  -d '{"mode":"semantic","query":"deployment region"}'
curl -sS -X POST localhost:8000/v1/recall \
  -H 'content-type: application/json' \
  -d '{"mode":"semantic","query":"deployment region"}'
```

- Expected: the item from step 1 surfaces with a positive `score`.
- Observed: _

### 4. LLM classification runs

(Requires `ENGRAM_CLASSIFICATION_PROVIDER=openai` and a classification model.)

```bash
curl -sS -X POST localhost:8000/v1/classify \
  -H 'content-type: application/json' \
  -d '{"content":"all API responses must be JSON"}'
```

- Expected: a `suggested_kind`/`confidence` response from the LLM path
  (not the rule-only path).
- Observed: _  (if blocked, record the error and reproduction)

### 5. Conflict classification exercised end-to-end

Write an item that semantically overlaps an existing active item (similarity >
0.85), with `ENGRAM_CONFLICT_CHECK_ON_WRITE=true`:

```bash
curl -sS -X POST localhost:8000/v1/remember \
  -H 'content-type: application/json' \
  -d '{"content":"<variant of an existing active item>","source_type":"manual"}'
```

- Expected: conflict detection runs (embeddings + classifier) and the response
  reflects duplicate/refine/contradict handling.
- Observed: _  (if blocked, record the error and reproduction)
