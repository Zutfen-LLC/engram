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
| `--database-url`      | Operator database URL. Default: `ENGRAM_OWNER_DATABASE_URL`, then `ENGRAM_DATABASE_URL`. Only `postgresql://` and `postgresql+asyncpg://` are accepted (the former is normalized to the latter); every other scheme/driver is rejected. A rejected or unconstructable URL degrades only the database-backed checks — it never aborts the report. Never echoed, serialized, or logged. |
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
| 8 | `capture.remember` | `usage_report` candidate-funnel (server-observed) | Pass only when at least one candidate succeeded, none failed, and none remain unresolved. Warn when any candidates remain unresolved (**including** when successes are also present — an unresolved candidate is never silently counted as healthy), when both successes and failures occurred, or when extraction was observed but nothing reached the server. Fail only when every resolved attempt failed and none remain unresolved. | Requires `ENGRAM_USAGE_TELEMETRY_ENABLED=true`. |
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
   subset) and truthfully distinguishes every combination of outcome:
   success-only with nothing unresolved is the only `pass`
   (`REMEMBER_PIPELINE_HEALTHY`); an unresolved candidate is `warn`
   (`REMEMBER_OUTCOMES_PENDING`) **regardless of whether a success is also
   present** — an unresolved candidate is never silently counted as healthy
   just because something else succeeded; a mix of success and failure is
   `warn` (`REMEMBER_PARTIAL_FAILURES`); and only failures with nothing
   unresolved and no success is `fail` (`REMEMBER_PIPELINE_FAILED`). It no
   longer calls an unresolved candidate "healthy" by omission (ENG-LOOP-001A-FIX2
   / FIX2-4 corrected this same claim in the FIX1 documentation and PR
   description, which had drifted from the implementation), nor claims
   "every attempt failed" while unresolved candidates are still in flight.
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

## FIX2 corrections (ENG-LOOP-001A-FIX2)

A second hardening pass closed four remaining truthfulness/fail-safety
defects found in review of the FIX1 head. The 11-check report, ordering,
schema, exit-code mapping, and read-only guarantees remain unchanged.

1. **Database-resource construction and disposal are now subordinate to the
   report's failure boundary.** Previously, constructing the database
   resource (an explicit `--database-url` engine or the process-owned
   factory) happened *before* `run_doctor`'s failure boundary — a malformed
   or unsupported URL, a missing driver module, or an engine-construction
   error could abort the entire report before `service.health`,
   `service.readiness`, `identity.scopes`, or `review.backlog` ever ran.
   Resource construction (`_prepare_database_resource`) now never raises: a
   construction failure degrades every DB-backed check to `unknown`
   (`DATABASE_RESOURCE_UNAVAILABLE`) while every HTTP-only check still
   executes normally. Separately, disposing a one-shot engine
   (`_dispose_owned_engine_safely`) is now bounded by an async timeout and
   fully exception-suppressed, so a disposal failure or an indefinitely
   stalled disposal can never alter, replace, or prevent an
   already-completed report. `--database-url` acceptance is also now
   *strict*: only `postgresql://` and `postgresql+asyncpg://` are accepted
   (the former normalized to the latter); every other scheme or driver
   (`postgresql+psycopg://`, `postgresql+psycopg2://`, `mysql://`,
   `sqlite:///...`, or a malformed URL) is now explicitly **rejected**
   rather than silently passed through to `create_async_engine`, which
   previously could raise unpredictably from deep inside SQLAlchemy or an
   unavailable driver module. URL parsing uses SQLAlchemy's own
   `make_url`/`URL.set()`, so percent-encoded credentials, IPv6 hosts,
   ports, database names, and query parameters all survive normalization
   unchanged.
2. **`/whoami` is now strictly validated as untrusted input.** Previously,
   `scopes` was coerced with `str(...)` per entry (a string became a set of
   characters, a mapping became its keys, a non-iterable could raise and
   abort the report), and `tenant_id`, `principal_type`, and the memory
   profile's slug/version were copied directly from the response into
   evidence with no validation — a malicious or incompatible remote service
   could place credential material or arbitrary prose there. A strict
   internal response model now requires `principal_id`/`tenant_id` to be
   real UUIDs, `principal_type` to be a member of the canonical principal-type
   vocabulary (`engram.auth.VALID_PRINCIPAL_TYPES`), `scopes` to be a JSON
   array containing only canonical scope strings
   (`engram.auth.VALID_SCOPES`), and an optional `memory_profile` to have a
   canonical-grammar slug (`engram.memory_profiles.validate_slug`, bounded
   to 255 characters) and a strictly positive integer version (not a
   boolean or a numeric string — pydantic's default `int` coercion accepts
   both, so this is validated explicitly). A malformed response now yields
   `identity.scopes` `fail`/`IDENTITY_RESPONSE_INVALID` with a locally
   generated summary — never pydantic's raw validation error text, which
   can echo the rejected value verbatim. An explicit `--tenant` is still
   honored for database-level scope even when `/whoami` is malformed.
3. **Review stats/queue evidence now requires complete payloads, and
   blocker strings are allow-listed against the canonical vocabulary.**
   `total` in `/v1/review/stats` was previously defaulted to `0` when
   absent, letting an incomplete response pass as an empty backlog; it is
   now required, and the selected status-bucket counts may never sum to
   more than it. Each `/v1/review/queue` item's `promotion_blockers` was
   previously defaulted to `[]` when absent; it is now required on every
   item. Every blocker string must now be one of the canonical promotion
   blocker codes exported from `engram.promotion.PROMOTION_BLOCKER_CODES`
   (the same `BLOCK_*` constants the promotion engine itself produces) or
   the review-preview marker `conflict_recheck_not_run` — an unrecognized
   string (which could be memory content, a URL, or a credential placed
   there by a buggy or hostile server) now invalidates the queue sample
   (`REVIEW_QUEUE_UNAVAILABLE`) instead of being counted and echoed into
   `top_blockers`.
4. **Remember-pipeline documentation corrected to match the implemented
   state machine.** The FIX1 documentation and PR description incorrectly
   stated that a success is healthy even when unresolved candidates are
   still pending. The implementation (and its tests) have always treated an
   unresolved candidate as `warn`/`REMEMBER_OUTCOMES_PENDING` regardless of
   whether a success is also present in the window — this was a
   documentation defect, not an implementation defect, and no check
   behavior changed as part of this correction. See the corrected
   `capture.remember` row in the Checks table above.

**Explicitly unchanged (non-goals held throughout FIX2):** no new doctor
check; no `engram.doctor` schema-version bump; no database migration; no new
REST endpoint; no changes to recall, ranking, budgets, promotion decisions,
review transitions, trust, authority, RLS, profile narrowing, or Context
Manifest contracts; no diagnostic recall; no external provider call; no
automatic configuration or remediation; no Hermes process inspection or
lifecycle change; no `engram usage-report` schema or rendering change.

See also: [Context Receipt Inspection](context-receipt-inspection.md),
[dogfood usage metering](dogfood-usage-metering.md).
