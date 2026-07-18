# Context-receipt startup dark writes — dogfood rollout (ENG-CONTEXT-002B)

This document describes the staged rollout of the startup context-receipt
dark write (ENG-CONTEXT-002B). It is a **default-off, fail-open** shadow /
dogfood slice: when enabled, a successful startup recall additionally
persists one immutable `context_receipts` row, but a receipt failure never
fails recall or modifies the served response.

ENG-CONTEXT-002A provides the storage substrate (migration 026,
`context_receipts` table, repository). ENG-CONTEXT-002B wires it into the
production startup-recall path. Receipt identifiers and hashes remain
**invisible to clients** until ENG-CONTEXT-002C.

## What the dark write proves

For each enabled startup recall, Engram records a content-addressed
`ContextManifestV1` (built from the *finalized* `RecallResponse` and the
*actual* resolved execution context) and persists it as an immutable receipt.
The manifest proves **what Engram served and which Engram policy/version
admitted it**. It does **not** prove that the memory was factually true or
that an agent relied on it.

## Prerequisites

- Migration 026 (`context_receipts` table) applied. This slice adds **no
  migration** — it reuses the 026 table.
- The API service image contains the ENG-CONTEXT-002B code.

## Stage 0 — deploy disabled

```bash
ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=false
```

Expected behavior:

- **no receipt writes** (the recall route performs no receipt work at all);
- **no response change** — `RecallResponse` is byte-identical to a pre-002B
  deployment;
- migration 026 remains available (and may already be applied).

This stage verifies the disabled path performs no database work and that the
default-off deployment is behaviorally identical to the previous release.

## Stage 1 — enable on dogfood

Set the following on the **API service only** (these are API-only settings;
they are not propagated to the worker):

```bash
ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=true
ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS=1.0
ENGRAM_USAGE_TELEMETRY_ENABLED=true
```

Restart only the API service:

```bash
docker compose up -d --no-deps engram-service
```

`ENGRAM_USAGE_TELEMETRY_ENABLED=true` is recommended so the bounded
`context_receipt.dark_write` usage events are recorded. Structured logs
remain the rollout signal even when usage telemetry is off.

## Stage 2 — verify

Run the diagnostic queries below as the **owner** role (they aggregate across
tenants/principals, so the app role cannot run them). Replace
`<enable_time>` with the timestamp the API restarted in Stage 1 (use
`created_at >= '<enable_time>'`).

### Startup recall logs since enable time

```sql
SELECT count(*) AS startup_recalls
FROM recall_logs
WHERE mode = 'startup' AND created_at >= '<enable_time>';
```

### Receipts since enable time

```sql
SELECT count(*) AS receipts
FROM context_receipts
WHERE created_at >= '<enable_time>';
```

### Recall logs without receipts (gaps)

A gap is expected when a dark-write attempt failed or timed out (fail-open).
Sustained zero receipts means the feature is silently failing.

```sql
SELECT count(*) AS missing_receipts
FROM recall_logs rl
LEFT JOIN context_receipts cr
  ON cr.recall_log_id = rl.id
  AND cr.tenant_id = rl.tenant_id
  AND cr.principal_id = rl.principal_id
WHERE rl.mode = 'startup'
  AND rl.created_at >= '<enable_time>'
  AND cr.id IS NULL;
```

### Duplicate receipt impossibility

There is exactly one receipt per recall log (unique `recall_log_id`):

```sql
SELECT count(*) AS duplicates
FROM (
  SELECT recall_log_id, count(*) AS n
  FROM context_receipts
  GROUP BY recall_log_id
  HAVING count(*) > 1
) d;
-- expected: 0
```

### Receipt usage-event counts by status

```sql
SELECT status, count(*) AS n
FROM usage_events
WHERE event_type = 'context_receipt.dark_write'
  AND created_at >= '<enable_time>'
GROUP BY status;
-- expected statuses: created, idempotent, failed, timed_out
```

### p50 / p95 receipt latency

```sql
SELECT
  percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
  max(latency_ms) AS max_ms
FROM usage_events
WHERE event_type = 'context_receipt.dark_write'
  AND created_at >= '<enable_time>';
```

Compare p95 against `ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_TIMEOUT_SECONDS`. A
p95 near the timeout indicates the attempt is regularly being bounded by the
timeout (fail-open `timed_out` results).

### Manifest verification failures

```sql
SELECT count(*) AS verification_failures
FROM usage_events
WHERE event_type = 'context_receipt.dark_write'
  AND metadata->>'verification_status' = 'failed'
  AND created_at >= '<enable_time>';
-- expected: 0 (any non-zero value is a storage integrity signal)
```

### Per-principal isolation spot check

Receipts are tenant/principal-owned (FORCE RLS). Confirm a principal's
receipts reference only that principal's recall logs:

```sql
SELECT cr.tenant_id, cr.principal_id, count(*) AS receipts
FROM context_receipts cr
WHERE cr.created_at >= '<enable_time>'
GROUP BY cr.tenant_id, cr.principal_id
ORDER BY receipts DESC
LIMIT 20;
```

> Do **not** publish raw manifest content in operational examples. The
> manifest JSONB contains served-context metadata; only aggregate counts and
> hashes belong in shared dashboards.

## Stage 3 — disable safely

```bash
ENGRAM_CONTEXT_RECEIPT_DARK_WRITE_ENABLED=false
```

Restart the API:

```bash
docker compose up -d --no-deps engram-service
```

Existing receipts remain **immutable**. No data rollback is required — the
`context_receipts` rows are retention evidence and stay in place. Future
startup recalls stop writing receipts.

Receipt failures are expected to be visible (in logs and usage events) but
**fail-open** during the dark-write phase: the recall response is always
served successfully regardless of receipt outcome.

## Limits during the dark-write phase

- **No client-visible receipt fields.** `RecallResponse` is unchanged. No
  `receipt_id`, `manifest_hash`, `packet_hash`, `receipt_status`,
  `receipt_error`, or `verification_status` is added to any client response,
  SDK, MCP, or Hermes contract. Exposure is decided in ENG-CONTEXT-002C.
- **Semantic recall is excluded.** Only `mode=startup` writes receipts. A
  semantic Context Manifest is not implemented in this slice.
- **No inspect/verify API.** Retrieval and authorized rehydration land in
  ENG-CONTEXT-003. The diagnostic queries above run as the owner role.

## Privacy

The dark write stores only the manifest JSONB (no raw memory content, raw
`working_set`, or raw query text). Structured logs and usage events carry
only bounded aggregate metadata: mode, status, item/byte counts, latency,
failure stage, exception *type*, and verification status. Exception
messages, stack values, raw content, `working_set`, query text, manifest
JSON, and canonical JSON are never logged or stored in usage metadata.
`logger.exception` is prohibited on this fail-open path because exception
representations may include bound values.