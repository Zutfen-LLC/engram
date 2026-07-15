# Usage metering (ENG-METER-001 / ENG-METER-002)

> **Status:** this is an OBSERVABILITY slice, not a billing slice. It exists so
> real dogfood data can inform hosted pricing allowances later. Nothing here
> implements invoices, quotas, spending caps, or a customer portal, and
> nothing here changes model choice, recall ranking, classification decisions,
> trust policy, review behavior, or conflict semantics.

## What this is for

Before committing to a hosted pricing model, we need real answers to
questions like:

- How many candidate memories does each lifecycle event produce?
- How much does classification and conflict adjudication cost in tokens?
- How much does embedding generation cost, split by document write, backfill,
  and query-time recall/search?
- What is the relationship between candidates observed, memories retained,
  embeddings generated, and database/index storage?
- Is "one processed memory per candidate, rounded to 1 KiB" a plausible meter?

The `usage_events` table (migrations/017_usage_events.sql) is a durable,
append-only ledger that answers these questions from real usage instead of
guesswork. `engram usage-report` turns it into a report (see
`docs/ops/dogfood-usage-metering.md` for the runbook).

## Event taxonomy

All events share one table, `usage_events`, distinguished by `event_type` +
`operation` + `status`:

| event_type | operation (examples) | status (examples) | meaning |
|---|---|---|---|
| `candidate.observed` | `process_memory_candidate` | `accepted_for_processing` | One candidate memory entered the pipeline (via `/v1/classify` and/or `/v1/remember`). Recorded exactly once per `correlation_id`. |
| `candidate.outcome` | `process_memory_candidate` | `created` / `deduped` / `superseded` / `failed` | The terminal outcome of ONE `/v1/remember` attempt for a candidate. **Append-only per attempt** — every invocation appends its own row; the report derives a single logical outcome per `correlation_id` (earliest non-`failed` attempt, else `failed`). |
| `provider.call` | `classification`, `conflict_classification`, `embedding_document`, `embedding_backfill`, `embedding_query_recall`, `embedding_query_search`, `embedding_setup` | `succeeded` / `failed` / `disabled` | One instrumented provider operation. A batched embedding operation is one row with `input_count=N`, never N rows. `succeeded` and `failed` represent actual external calls; `disabled` means configuration prevented an external request. |
| `retrieval.request` | `startup_recall`, `semantic_recall`, `keyword_search`, `semantic_search`, `hybrid_search` | `succeeded` / `failed` | One recall/search request (success or failure), with result counts and the canonical embedding outcome described below. |
| `client.lifecycle_summary` | `sync_turn`, `pre_compress`, `session_end` | `succeeded` / `partial` | A client-reported aggregate for one hooks-adapter lifecycle invocation. **Diagnostic and non-authoritative** — see below. |

## Column meanings

See `migrations/017_usage_events.sql` for the full DDL. Notable columns:

- `correlation_id` — one UUID per extracted candidate. Shared by the
  `candidate.observed` event, the `candidate.outcome` event, and (when the
  candidate originated from `/v1/remember`) the worker-driven
  `provider.call` events for that candidate's embedding generation and
  conflict classification — the correlation id is threaded through the job
  payload so async worker calls are attributable to the original candidate.
  `provider.call` events also carry `job_id` for worker-driven calls.
  `retrieval.request` events do not set `correlation_id`.
- `dedupe_key` — the idempotency key backing the partial unique index on
  `(tenant_id, event_type, dedupe_key)`. Used by `candidate.observed`
  (`str(correlation_id)`) and `client.lifecycle_summary`
  (`str(invocation_id)`). `candidate.outcome` is append-only per attempt and
  carries **no** `dedupe_key` (see "Correlation and deduplication semantics").
- `provider_host` — a bare hostname (e.g. `api.deepinfra.com`), never a path,
  query string, or credential. See "Privacy rules" below.
- `input_count` / `input_bytes` — UTF-8 byte counts, never Python character
  counts. For `provider.call`, `input_count` is the number of prompts/texts in
  that one call (so a batched embedding request reports `input_count=N`).
- `prompt_tokens` / `completion_tokens` / `total_tokens` / `reported_cost_usd`
  — nullable. Missing provider usage is a valid, expected state (not every
  provider/proxy returns usage), and is recorded as `NULL`, not as an error.
- `metadata` — a small JSONB bag of safe, non-content dimensions (e.g.
  classification confidence, conflict verdict, embedding vector count/
  dimensions, sanitized exception class for failures). Never raw content.

## Retrieval embedding outcomes

`metadata.embedding_outcome` has one canonical vocabulary. The legacy
nullable `metadata.embedding_call_occurred` is derived exactly as follows:

| `embedding_outcome` | `embedding_call_occurred` | meaning |
|---|---:|---|
| `not_required` | `false` | The retrieval mode does not use embeddings (keyword search and startup recall). |
| `not_attempted` | `false` | Embeddings were relevant, but execution failed before the provider call. |
| `disabled` | `false` | The embedding abstraction was reached, but configuration prevented an external call. |
| `succeeded` | `true` | An external embedding call returned a usable vector. Later database/search failure does not change this outcome. |
| `failed` | `true` | An external embedding call failed or returned an unusable response. |
| `unknown` | `null` | The recording layer cannot truthfully determine which embedding stage was reached. |

The requested retrieval mode is never used as a proxy for whether an external
embedding call occurred.

## Correlation and deduplication semantics

One `correlation_id` is generated per extracted candidate — by the client
(hooks adapter) when one exists, or by the server when a caller doesn't
supply one. It is threaded through:

1. `POST /v1/classify` (optional request field, echoed in the response).
2. `POST /v1/remember` (optional request field, echoed in the response).

Both endpoints call `record_candidate_once`, which inserts a
`candidate.observed` row keyed by `dedupe_key = str(correlation_id)`. The
partial unique index on `(tenant_id, event_type, dedupe_key)` makes this
idempotent: whichever of classify/remember (or a retry of either) reaches the
database first wins; the other is a silent no-op. This is why a direct
`/v1/remember` call with no preceding `/v1/classify` still produces exactly
one `candidate.observed` event, and why a classify-then-remember pair never
double-counts.

`candidate.outcome` is now **append-only per attempt**: every `/v1/remember`
invocation appends its own row (no `dedupe_key`), so a transiently `failed`
attempt followed by a successful retry is recorded honestly as two rows.
`candidate.observed` stays unique per `correlation_id` (one observation per
candidate). The report derives a single **logical outcome** per
`correlation_id` — the earliest non-`failed` attempt's status, or `failed`
when no attempt succeeded — for the failure/create funnel, and reports raw
attempt-level diagnostics (`remember_attempts`, `failed_attempts`,
`attempts_per_candidate_avg`) separately. This corrects the earlier
first-outcome-wins model, which silently suppressed a later successful retry
once a `failed` row existed.

In the report, `candidate_observations` counts unique observed candidate
correlation IDs, `logical_candidates` counts retry-resolved outcomes, and
`remember_attempts` counts literal `candidate.outcome` rows (one per
`/v1/remember` invocation). Logical `created`/`deduped`/`superseded`/`failed`,
per-principal `created_count`, and semantic queries per created memory all use
the retry-safe logical outcome. Compatibility aliases remain:
`total_attempts == remember_attempts` and
`distinct_candidates == logical_candidates`.

## Provider operations, calls, and failures

A `provider.call` row is an instrumented **provider operation**. It is an
**actual external provider call** only when `status != 'disabled'`. Every
report section therefore distinguishes:

- `provider_operations`: all `provider.call` rows;
- `actual_provider_calls`: non-disabled rows;
- `disabled_provider_operations`: disabled rows where no external request
  occurred.

For provider-economics groups, retained `calls` is a compatibility field that
counts all operations; `actual_calls` excludes disabled rows and `disabled_n`
counts them. Conflict economics similarly exposes
`conflict_classifications` (all operations) and `conflict_actual_calls`;
query embeddings expose compatibility `query_embedding_calls` (all
operations) and `query_embedding_actual_calls`. Hourly `provider_calls` is the
compatibility all-operation field, while `actual_provider_calls` excludes
disabled rows. Token and cost coverage denominators exclude disabled rows.

Every production `provider.call` with `status='failed'` carries only safe,
categorical failure metadata:

- `failure_stage='provider_error'` for transport, timeout, authentication,
  HTTP/API, or SDK failure before a usable response;
- `failure_stage='response_parse'` when a required JSON response cannot be
  parsed or is not the required structural type;
- `failure_stage='response_validation'` when a parsed provider response
  violates a contract such as embedding vector count or dimensions;
- `error_type=type(exc).__name__`, never the exception message;
- `application_fallback=true` only for classification and conflict
  classification failures that use the existing rule/heuristic fallback.

Failed response parsing or validation may still carry token counts and
provider-reported cost because provider inference was incurred even though the
response was unusable. Embedding failures do not claim an application
fallback because Engram substitutes no embedding vector.

## Privacy rules (data minimization)

This table never stores:

- raw memory content, classification prompts, conversation context, or
  search/recall query text;
- API keys, auth headers, secrets, or credential material;
- full provider URLs with paths, query strings, or credentials — only a
  sanitized hostname (`safe_provider_identity`);
- provider response bodies;
- unsanitized exception messages — only the exception *class name*
  (`type(exc).__name__`), for both provider-call failures and telemetry
  insert failures logged internally.

Only counts, byte totals (UTF-8), token counts, latency, provider-reported
cost, and a small set of safe categorical dimensions (verdict, confidence,
kind, review status, source type, etc.) are ever stored.

## RLS and append-only posture

`usage_events` follows the same tenant-isolation shape as every other
tenant-scoped table (see `migrations/003_app_role_and_force_rls.sql`):

- `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`, so isolation holds
  even for the table-owning role.
- `CREATE POLICY tenant_isolation_usage_events ON usage_events USING
  (tenant_id::text = current_setting('app.tenant_id', true))`.
- The app role (`engram_app`) is granted `SELECT, INSERT` only —
  `UPDATE`/`DELETE` are explicitly revoked, so the ledger is append-only from
  the application's perspective. Only the owner/migration role (a superuser
  in the default Compose image) can alter or delete rows, and it does so only
  via explicit migrations, never as part of normal operation.
- Owner/migration operations bypass RLS entirely (as with every other admin/
  reporting path in this codebase), which is how `engram usage-report`
  produces cross-tenant platform reporting.

## Telemetry configuration

- `ENGRAM_USAGE_TELEMETRY_ENABLED` (default `false`) controls server-side
  collection. When `false`, every helper in `engram.usage` is a cheap no-op
  that returns immediately without opening a database session — telemetry
  can never be the reason a request is slow or fails.
- `ENGRAM_HOOKS_REPORT_LIFECYCLE_TELEMETRY` (default `false`, engram-hooks)
  controls whether the hooks adapter reports `client.lifecycle_summary`
  events. Independent of the server-side flag — a dogfood deployment
  typically enables both.
- Telemetry writes use a short-lived, tenant-scoped **app-role** session
  (`engram.db.async_session_factory`), never the owner role and never the
  caller's own request session — so an incurred provider cost is durably
  recorded even if the surrounding business transaction later rolls back, and
  a telemetry database error can never poison the caller's session.
- A telemetry insert failure is logged (`operation`, event UUID, tenant ID,
  and the exception *type* only) and swallowed. It can never fail classify,
  remember, recall, search, a worker job, or a lifecycle hook.

## Known limitations

- **SDK-internal HTTP retries may not be separately observable.** If the
  Engram SDK's underlying HTTP client silently retries a request at the
  transport layer, that retry is invisible to `engram.usage` — the caller
  only sees (and can only instrument) the call it made. Provider-level
  retries inside the OpenAI SDK have the same limitation. Documented here
  honestly rather than claiming perfect provider-attempt accounting.
- **`reported_cost_usd` is frequently `NULL`.** Not every OpenAI-compatible
  provider/proxy reports cost. `engram.usage.extract_openai_compatible_usage`
  looks in several plausible locations (`usage.cost`, `usage.total_cost`,
  `usage.estimated_cost`, and the same names on the top-level response) but a
  `NULL` here is an expected, valid outcome — token counts remain the durable
  basis for later cost modeling.
- **Client lifecycle summaries are diagnostic and untrusted.** They are
  reported by the hooks adapter based on its own local counts (extraction,
  guard-rejection, parking) that the server cannot independently observe or
  verify. They are stored with `metadata.authoritative = false` and must
  never be treated as an authoritative billing record, nor used to gate or
  reconcile anything server-side.
- **`flat_candidate_units` and `kib_candidate_units` are hypothetical meter
  scenarios for analysis only.** Neither is an invoice or authoritative
  billable usage. Pricing/quota decisions are explicitly out of scope for
  this slice.
- **`recall_logs` remains the audit source of what was recalled.** This
  telemetry layer is a metering summary layered alongside it, not a
  replacement — it does not alter `recall_logs`' trust/audit semantics.
- **`item_events` is not reused for metering.** It is the trust/audit history
  of one memory item; usage telemetry covers operations (rejected candidates,
  provider calls, retrieval requests) that never create a memory item.

## Distinction from future billing

This slice deliberately does not implement: pricing tiers, quotas, spending
caps, per-agent charges, invoices, a customer portal, external analytics, a
provider price table, or automatic charge calculation. Cost modeling from the
durable token-usage data collected here is future work, once real dogfood
volume has been observed.
