-- Engram — Append-only metering/usage telemetry ledger (ENG-METER-001)
-- 017_usage_events.sql
--
-- Observability slice, not billing: this table records candidate-lifecycle,
-- provider-call, retrieval, and client-lifecycle-summary telemetry so dogfood
-- economics can be measured before hosted pricing allowances are chosen. It
-- never gates or throttles a request; the application only ever performs a
-- best-effort INSERT into this table.
--
-- Distinct from ``item_events`` (the trust/audit history of one memory item):
-- usage_events covers operations that never create a memory item (rejected
-- candidates, failed provider calls, retrieval requests) and is tenant-scoped
-- rather than item-scoped.
--
-- Run as: psql -f migrations/017_usage_events.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

-- ============ 1. Table ============

CREATE TABLE IF NOT EXISTS usage_events (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id        UUID NULL REFERENCES principals(id) ON DELETE SET NULL,
    workspace_id        UUID NULL REFERENCES workspaces(id) ON DELETE SET NULL,

    event_type          TEXT NOT NULL,   -- candidate.observed | candidate.outcome | provider.call | retrieval.request | client.lifecycle_summary
    operation           TEXT NOT NULL,   -- e.g. process_memory_candidate, classification, embedding_document, semantic_recall, sync_turn, ...
    status               TEXT NOT NULL,   -- e.g. accepted_for_processing, created, deduped, superseded, failed, succeeded, fallback, disabled, no_usage, partial

    correlation_id      UUID NULL,       -- one per extracted candidate; ties candidate.observed <-> candidate.outcome <-> classify/remember
    dedupe_key          TEXT NULL,       -- idempotency key for retried inserts (partial unique index below)
    job_id              UUID NULL,

    source_type         TEXT NULL,       -- manual | import | migration | extraction | sync_turn | pre_compress | session_end

    provider_adapter    TEXT NULL,       -- logical adapter, e.g. "openai"
    provider_host       TEXT NULL,       -- sanitized hostname only, e.g. "api.deepinfra.com" — never a path/query/credential
    model               TEXT NULL,
    embedding_profile   TEXT NULL,

    input_count         INTEGER NOT NULL DEFAULT 0,
    input_bytes         BIGINT NOT NULL DEFAULT 0,

    prompt_tokens       BIGINT NULL,
    completion_tokens   BIGINT NULL,
    total_tokens        BIGINT NULL,

    latency_ms          INTEGER NULL,
    reported_cost_usd   NUMERIC(20, 10) NULL,

    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_usage_events_input_count_nonneg CHECK (input_count >= 0),
    CONSTRAINT chk_usage_events_input_bytes_nonneg CHECK (input_bytes >= 0),
    CONSTRAINT chk_usage_events_prompt_tokens_nonneg CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    CONSTRAINT chk_usage_events_completion_tokens_nonneg CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
    CONSTRAINT chk_usage_events_total_tokens_nonneg CHECK (total_tokens IS NULL OR total_tokens >= 0),
    CONSTRAINT chk_usage_events_latency_ms_nonneg CHECK (latency_ms IS NULL OR latency_ms >= 0),
    CONSTRAINT chk_usage_events_reported_cost_nonneg CHECK (reported_cost_usd IS NULL OR reported_cost_usd >= 0)
);

-- ============ 2. Indexes ============

CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_created
    ON usage_events (tenant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_type_op_created
    ON usage_events (tenant_id, event_type, operation, created_at);

-- Per-principal time-window reports (report section D: breakdown by active principal).
CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_principal_created
    ON usage_events (tenant_id, principal_id, created_at);

-- Idempotent retries: a dedupe_key is unique per (tenant, event_type) when
-- present. NULL dedupe_key rows (most provider.call/retrieval.request events,
-- which are one-row-per-real-call and never intentionally retried at the
-- telemetry layer) are excluded from the uniqueness constraint.
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_events_dedupe
    ON usage_events (tenant_id, event_type, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

-- ============ 3. Row Level Security ============

ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'usage_events'
          AND policyname = 'tenant_isolation_usage_events'
    ) THEN
        CREATE POLICY tenant_isolation_usage_events ON usage_events
            USING (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

-- ============ 4. Append-only grants ============
-- The app role may SELECT and INSERT its own tenant's events (RLS scopes the
-- rows); UPDATE/DELETE are explicitly revoked so the ledger is append-only
-- from the application's perspective. Migration 003's
-- ``ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE`` already
-- granted the app role UPDATE/DELETE on this newly-created table (it is owned
-- by the migration role), so those must be revoked explicitly here.
-- Owner/superuser migration and CLI reporting paths bypass RLS entirely and
-- are unaffected by this grant, so cross-tenant platform reporting still works.

GRANT SELECT, INSERT ON usage_events TO engram_app;
REVOKE UPDATE, DELETE ON usage_events FROM engram_app;
