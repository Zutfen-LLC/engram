# Automatic Memory Loop Doctor (ENG-LOOP-001A)

> **Scope:** `engram doctor` — a read-only, operator-local CLI command. It
> answers whether Engram and its automatic memory loop are healthy, degraded,
> unhealthy, or unobservable, by composing evidence from existing endpoints
> and tables. It never mutates memory, configuration, or queues.

## What it is (and is not)

`engram doctor` composes existing `/health`, `/ready`, `/whoami`,
`/v1/review/stats`, and `/v1/review/queue` HTTP evidence with bounded,
read-only PostgreSQL evidence (reusing the same aggregation SQL as
`engram usage-report`) and Context Receipt repository verification. It is:

- **Operator-local.** It describes the environment running the CLI —
  its local `ENGRAM_*` configuration, and whatever tenant/database it is
  pointed at. It is not a certification of a different remote deployment.
- **Read-only.** No check inserts, updates, deletes, promotes, recalls,
  classifies, embeds, enqueues, retries, reclaims, archives, verifies,
  disputes, or supersedes anything. Database inspection runs inside an
  explicitly read-only transaction and is rolled back.
- **Evidence-based, not a live probe.** It never issues a recall (a recall
  writes recall logs, may run lazy promotion, and may create a Context
  Receipt) and never calls a live embedding or classification provider.
- **Bounded.** Every HTTP request is bounded by `--timeout-seconds`. All
  database evidence gathering — including acquiring a session, not just each
  statement — runs under one outer `--timeout-seconds` deadline, so a stalled
  connection or session factory cannot hang the report. One failed or
  unreachable check never aborts the rest of the report — the fullest safe
  report is always produced.

## Usage

```bash
# Human-readable report against the local default port.
engram doctor

# Against a specific deployment, with an API key from the environment.
ENGRAM_API_KEY=eng_... engram doctor --base-url https://engram.example.com

# Machine-readable report only (stdout is pure JSON — nothing else is mixed in).
engram doctor --json

# A specific tenant and a wider evidence window.
engram doctor --tenant 11111111-1111-1111-1111-111111111111 \
  --since 2026-07-01T00:00:00+00:00 --until 2026-07-25T00:00:00+00:00
```

### Arguments

| Argument             | Behavior |
|----------------------|----------|
| `--base-url`         | Engram API URL. Default: `ENGRAM_BASE_URL`, else a loopback URL on the configured service port. |
| `--tenant`            | Tenant UUID for database-level evidence. Default: the tenant returned by `/whoami`. When identity cannot be resolved, tenant-specific checks are marked `unknown` rather than silently reporting deployment-wide counts as tenant truth. |
| `--since` / `--until` | Timezone-aware ISO-8601 window bounds. Default: the last 24 hours. |
| `--database-url`      | Operator database URL. Default: `ENGRAM_OWNER_DATABASE_URL`, then `ENGRAM_DATABASE_URL`. Accepts `postgresql://` or `postgresql+asyncpg://` (the former is normalized to the latter). Never echoed, serialized, or logged. |
| `--timeout-seconds`   | Finite, strictly positive HTTP/diagnostic timeout (default 10s). Zero, negative, NaN, and infinite values are rejected, not coerced. |
| `--json`              | Emit only the stable `engram.doctor` JSON report. Human-readable output is the default. |

**Credentials:** the API key is read only from `ENGRAM_API_KEY`. There is no
`--api-key` flag — a plaintext secret on the command line would leak into
shell history and the process list.

### Exit codes

| Code | `overall_status` | Meaning |
|------|-------------------|---------|
| `0`  | `healthy`   | Every check passed. |
| `1`  | `degraded`  | At least one warning or unknown check exists; nothing failed. |
| `2`  | `unhealthy` | One or more checks failed, or the report could not be constructed safely. |

## Report schema

```json
{
  "schema": "engram.doctor",
  "schema_version": "1.0",
  "profile": "automatic_memory_loop",
  "engram_version": "0.1.0",
  "generated_at": "2026-07-25T12:00:00+00:00",
  "window": {"since": "2026-07-24T12:00:00+00:00", "until": "2026-07-25T12:00:00+00:00"},
  "scope": {"tenant_id": "11111111-1111-1111-1111-111111111111", "source": "whoami"},
  "overall_status": "degraded",
  "exit_code": 1,
  "checks": [
    {
      "id": "service.health",
      "status": "pass",
      "reason_code": "SERVICE_HEALTHY",
      "summary": "Service is reachable and healthy.",
      "evidence": {"http_status": 200},
      "remediation": []
    }
  ],
  "limitations": ["..."]
}
```

`checks` always contains exactly the 11 checks below, in this order, even
when a check could not run (it is emitted as `status="unknown"`, never
omitted). `evidence` never contains memory content, job payloads, raw
`last_error` values, receipt manifests, or credentials — only safe aggregate
counts, timestamps, status codes, and configuration-presence booleans.

## Checks

| # | Check ID | Evidence source | Status rule | Key limitation |
|---|----------|------------------|-------------|-----------------|
| 1 | `service.health` | `GET /health` | Fail on unreachable/timeout/malformed/non-`ok`. | Liveness only. |
| 2 | `service.readiness` | `GET /ready` | Fail when not `ready` (DB/RLS/pgvector). | Does not imply the automatic loop is active. |
| 3 | `identity.scopes` | `GET /whoami` | Fail on auth failure or missing read/write. Warn on tenant mismatch. `admin` satisfies every scope. | Does not prove an agent's key/scopes, only the CLI's. |
| 4 | `config.embeddings` | Local `ENGRAM_EMBEDDING_*` settings + storage counts | Warn if `provider=none`. Fail only for an inconsistent enabled provider. | No live provider call. |
| 5 | `config.classification` | Local `ENGRAM_CLASSIFICATION_*` settings | Warn if `provider=none` (rule-only). Fail only for an inconsistent enabled provider. | No live provider call. |
| 6 | `worker.queue` | `usage_report` worker aggregation + bounded dead/stale/due-pending queries | Fail on dead or stale-running jobs. Warn on an aged **due** pending backlog (`run_after <= now`) — an old `created_at` alone, for a job not yet due, is not a symptom. | Describes the queue, not a specific worker process. |
| 7 | `capture.lifecycle` | `usage_report` coverage/candidate-funnel + client `lifecycle_summary` events | Unknown when telemetry is disabled. Warn on no evidence or reported errors. | Client-reported, untrusted diagnostic evidence. |
| 8 | `capture.remember` | `usage_report` candidate-funnel (server-observed) | Warn when extraction is observed but nothing reaches the server, or outcomes are still pending (unresolved candidates and no success/failure yet). Fail only when every resolved attempt in the window failed, with none succeeded or still pending. | Requires `ENGRAM_USAGE_TELEMETRY_ENABLED=true`. |
| 9 | `recall.activity` | `recall_logs` (read-only) | Warn when no recall activity in the window. | Does not prove recalled context reached a model prompt. |
| 10 | `receipts.activity` | Local dark-write setting + `context_receipts`/`recall_logs` + repository verifier | Selects the latest receipt **within the requested window** (ties broken deterministically). Fails on an invalid receipt regardless of whether dark writes are currently disabled locally. Otherwise warns when disabled or a gap exists (writes are fail-open). Unknown when there is no startup recall to assess, or the latest receipt could not be verified. | Proves what was served, not factual truth or causality. |
| 11 | `review.backlog` | `GET /v1/review/stats` + bounded `GET /v1/review/queue?limit=100` | Passes only when both stats and queue evidence are present and strictly well-typed — malformed or nonsensical evidence is never coerced into a pass. Unknown when the credential lacks review authority, the call fails, or evidence fails validation. | `conflict_recheck_not_run` is excluded from blocker ranking and reported as a known preview limitation; no conflict recheck runs. |

## Example outputs (sanitized)

### Healthy

```
engram doctor — overall_status=healthy exit_code=0
...
[PASS   ] service.health           SERVICE_HEALTHY                  Service is reachable and healthy.
[PASS   ] service.readiness        SERVICE_READY                    Service reports ready.
[PASS   ] identity.scopes          IDENTITY_READY                   Identity resolved with read and write scope.
[PASS   ] config.embeddings        EMBEDDINGS_READY                 Embedding provider is configured and ready.
[PASS   ] config.classification    CLASSIFICATION_READY             Classification provider is configured.
[PASS   ] worker.queue             WORKER_HEALTHY                   No dead or stale jobs; pending backlog is fresh.
[PASS   ] capture.lifecycle        LIFECYCLE_EVIDENCE_RECENT        Recent, error-free lifecycle evidence observed.
[PASS   ] capture.remember         REMEMBER_PIPELINE_HEALTHY        Candidates are reaching the server and resolving successfully.
[PASS   ] recall.activity          RECALL_ACTIVITY_RECENT           4 recall(s) observed in the window.
[PASS   ] receipts.activity        RECEIPTS_HEALTHY                 Startup recalls have matching, valid Context Receipts.
[PASS   ] review.backlog           REVIEW_BACKLOG_OBSERVED          2 current item(s) observed (2 active, 0 proposed, 0 disputed).
```

### Degraded

```
engram doctor — overall_status=degraded exit_code=1
...
[PASS   ] service.health           SERVICE_HEALTHY                  Service is reachable and healthy.
[PASS   ] service.readiness        SERVICE_READY                    Service reports ready.
[PASS   ] identity.scopes          IDENTITY_READY                   Identity resolved with read and write scope.
[WARN   ] config.embeddings        EMBEDDINGS_DISABLED              Embeddings are intentionally disabled (provider=none).
           -> Set ENGRAM_EMBEDDING_PROVIDER=openai and configure ENGRAM_OPENAI_API_KEY to enable semantic enrichment.
[WARN   ] config.classification    CLASSIFICATION_RULE_ONLY         Classification is rule-based only (provider=none).
[PASS   ] worker.queue             WORKER_HEALTHY                   No dead or stale jobs; pending backlog is fresh.
[UNKNOWN] capture.lifecycle        USAGE_TELEMETRY_DISABLED         Usage telemetry is disabled on this process; lifecycle evidence is unavailable.
[UNKNOWN] capture.remember         REMEMBER_EVIDENCE_UNAVAILABLE    Usage telemetry is disabled; no authoritative remember-pipeline evidence exists.
[PASS   ] recall.activity          RECALL_ACTIVITY_RECENT           1 recall(s) observed in the window.
[WARN   ] receipts.activity        RECEIPTS_DISABLED                Context Receipt dark writes are disabled on this process.
[PASS   ] review.backlog           REVIEW_BACKLOG_OBSERVED          1 current item(s) observed (1 active, 0 proposed, 0 disputed).
```

### Unhealthy

```
engram doctor — overall_status=unhealthy exit_code=2
...
[FAIL   ] service.health           SERVICE_UNREACHABLE              GET /health was unreachable.
           -> Confirm the service is running and --base-url/ENGRAM_BASE_URL is correct.
[FAIL   ] service.readiness        SERVICE_READINESS_UNREACHABLE    GET /ready was unreachable.
[FAIL   ] identity.scopes          IDENTITY_UNREACHABLE             GET /whoami was unreachable.
...
[UNKNOWN] worker.queue             TENANT_SCOPE_UNRESOLVED          Tenant scope could not be resolved; worker evidence is deployment-wide only.
...
```

## Limitations (always present in the report)

- Does not inspect a running Hermes or other agent process — does not prove
  an adapter's automatic read/write hooks are active.
- Does not make a live embedding or classification provider call.
- Does not issue a recall — does not prove recalled context entered an
  agent's final model prompt.
- Lifecycle-summary telemetry is client-reported diagnostic evidence, not
  authoritative.
- Context Receipts prove what Engram served under a policy — not factual
  truth, model reliance, or causality.
- Local configuration checks describe the environment running the CLI, not a
  different remote deployment.

## Safety

`engram doctor` never inserts, updates, deletes, promotes, recalls,
classifies, embeds, enqueues, retries, reclaims, archives, verifies,
disputes, or supersedes anything. Database inspection runs inside an
explicitly read-only transaction (`SET TRANSACTION READ ONLY` on PostgreSQL)
and is rolled back on exit, never committed.

## FIX1 corrections (ENG-LOOP-001A-FIX1)

A post-review hardening pass corrected seven truthfulness, redaction, and
bounded-execution defects found in the initial implementation. The 11-check
report, its ordering, the `engram.doctor` v1.0 schema, exit-code mapping, and
read-only guarantees are unchanged; only the semantics below were corrected.

1. **`/ready` redaction.** `GET /ready`'s exception handler
   (`engram/api/routes/health.py`) no longer returns `str(exception)` — a
   driver/connection exception can embed a DSN, hostname, username, or
   password. It now returns only the exception's type name
   (`error_type`). `service.readiness` evidence is built from a strict
   allow-list of known categorical/version fields (`_safe_readiness_evidence`)
   and never copies `reason`/`detail`/`error`/`message` from the response
   body, even if `/ready` is later changed to include them.
2. **`review.backlog` strict validation.** Both `/v1/review/stats` and
   `/v1/review/queue` evidence are now validated for well-formed types before
   a pass is possible — a malformed count, a negative number, or an
   unexpected shape can no longer be silently coerced into a healthy report.
   Malformed evidence degrades to `unknown` (`REVIEW_BACKLOG_UNAVAILABLE` for
   malformed stats, `REVIEW_QUEUE_UNAVAILABLE` for a malformed queue sample)
   rather than aborting the report.
3. **`capture.remember` unresolved-candidate truthfulness.** The check now
   reads the full unresolved-candidate count (not just the ingest-specific
   subset) and truthfully distinguishes three states: at least one success
   (healthy, even with unresolved candidates pending), no success and no
   failure but unresolved candidates exist (`REMEMBER_OUTCOMES_PENDING`, not
   a failure), and every resolved attempt failed with nothing else pending
   (`REMEMBER_PIPELINE_FAILED`). It no longer calls an unresolved
   candidate "healthy" by omission, nor claims "every attempt failed" while
   unresolved candidates are still in flight.
4. **`worker.queue` due-pending backlog.** The aged-pending-backlog warning
   now requires the oldest pending job to actually be **due**
   (`run_after <= now`), computed via a dedicated bounded query. A job
   scheduled for the future is never flagged as backlog merely because it was
   *created* long ago.
5. **`receipts.activity` window-scoped selection + integrity precedence.**
   The "latest receipt" query now filters to the requested `[since, until)`
   window (deterministically tie-broken by `created_at DESC, id DESC`)
   instead of picking the newest receipt in the whole table regardless of
   window. An invalid receipt now fails the check
   (`LATEST_RECEIPT_INVALID`) **before** the disabled-dark-writes check, so a
   real integrity failure is never masked by "receipts are disabled here."
   A verification error that isn't a clean valid/invalid result is reported
   separately (`LATEST_RECEIPT_UNVERIFIABLE`) rather than erasing the
   evidence gathered so far.
6. **Bounded DB evidence gathering.** A `--timeout-seconds` value under one
   millisecond can no longer round down to `statement_timeout=0` (Postgres'
   spelling for *unlimited*) — timeouts round up to at least 1ms
   (`seconds_to_statement_timeout_ms`). The entire DB-evidence-gathering
   operation, including acquiring a session (not just each statement), now
   runs under one outer `asyncio.timeout(--timeout-seconds)` deadline, so a
   session factory whose connection acquisition itself stalls cannot hang
   the report.
7. **`--database-url` scheme acceptance + one-shot engine disposal.** An
   explicit `--database-url` now accepts both `postgresql://` and
   `postgresql+asyncpg://` (the former is normalized to the latter, since
   `create_async_engine` requires an async-capable driver encoded in the
   URL). The ad hoc one-shot engine built for an explicit `--database-url`
   is disposed exactly once — on success, on a raised exception, and on the
   internal DB-evidence timeout — and only when this run constructed it; the
   injected/global `owner_session_factory` path is never disposed.

**Explicitly unchanged (non-goals held throughout FIX1):** no new REST
endpoint; no database migration; no changes to recall, promotion, trust,
review, RLS, profile narrowing, or Context Manifest contracts; no diagnostic
recall; no live provider calls; no enabling of telemetry, receipts,
embeddings, classification, or Hermes; no Hermes process inspection; no
dashboard, tracing, or metrics framework; no changes to `engram usage-report`'s
existing JSON schema or human output; no broadening into session-summary or
live-dogfood slices; no `engram.doctor` schema version bump.

See also: [Context Receipt Inspection](context-receipt-inspection.md),
[dogfood usage metering](dogfood-usage-metering.md).
