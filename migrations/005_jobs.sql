-- Engram — Postgres-backed job queue (ENG-AUD-008)
-- 005_jobs.sql
--
-- Audit finding F20: the /v1/remember write path does expensive third-party
-- work inline — embedding generation, LLM classification refinement, and
-- (embedding-dependent) conflict detection — plus six DISTINCT vocab scans per
-- unclassified write. For chatty/agentic writers this makes memory a per-write
-- tax. This migration adds a durable, Postgres-only job queue so that work can
-- move off the request path while preserving correctness.
--
-- Design (see docs/plans/engram-memory-audit-2026-07.md §F20):
--   * Postgres is the only backing store — no Redis/Celery/SQS.
--   * Workers claim via FOR UPDATE SKIP LOCKED, safe for concurrency.
--   * Failures retry with exponential backoff; jobs go dead after max_attempts.
--   * Stale running jobs (worker crash) are reclaimable after a lease timeout.
--   * The table is tenant-scoped and RLS-protected (FORCE), consistent with
--     ENG-AUD-002. Claim/lock/retry bookkeeping runs through the owner role
--     (cross-tenant queue coordination); payload handlers re-scope to the
--     job's tenant via app-role sessions.
--
-- Run as: psql -f migrations/005_jobs.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

-- ============ 1. jobs table ============
CREATE TABLE IF NOT EXISTS jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_type      TEXT NOT NULL,
    -- pending | running | succeeded | failed | dead | cancelled
    status        TEXT NOT NULL DEFAULT 'pending',
    priority      INT NOT NULL DEFAULT 100,
    run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts      INT NOT NULL DEFAULT 0,
    max_attempts  INT NOT NULL DEFAULT 5,
    locked_at     TIMESTAMPTZ,
    locked_by     TEXT,
    last_error    TEXT,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'dead', 'cancelled')),
    CHECK (attempts >= 0),
    CHECK (max_attempts >= 1)
);

-- ============ 2. Indexes ============
-- The claim hot path: pending jobs due now, cheapest-first. A partial index
-- keeps it small (only pending rows) and ordered for the SKIP LOCKED scan.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_jobs_claim'
    ) THEN
        CREATE INDEX idx_jobs_claim
            ON jobs (run_after, priority, created_at)
            WHERE status = 'pending';
    END IF;
END
$$;

-- Per-tenant observability / filtering by type and status.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_jobs_tenant_type_status'
    ) THEN
        CREATE INDEX idx_jobs_tenant_type_status
            ON jobs (tenant_id, job_type, status);
    END IF;
END
$$;

-- Stale-lock reclamation: find running jobs whose lease has expired.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_jobs_stale_running'
    ) THEN
        CREATE INDEX idx_jobs_stale_running
            ON jobs (locked_at)
            WHERE status = 'running';
    END IF;
END
$$;

-- Idempotent enqueue: at most one pending/running job per
-- (tenant, job_type, dedupe_key). NULLS NOT DISTINCT keeps rows without a
-- dedupe_key from colliding (the WHERE clause excludes them anyway). Enqueue
-- stores the key in payload->>'dedupe_key' so the index covers it.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_jobs_dedupe'
    ) THEN
        CREATE UNIQUE INDEX idx_jobs_dedupe
            ON jobs (tenant_id, job_type, (payload->>'dedupe_key'))
            WITH NULLS NOT DISTINCT
            WHERE status IN ('pending', 'running') AND payload ? 'dedupe_key';
    END IF;
END
$$;

-- ============ 3. RLS ============
-- Tenant isolation consistent with ENG-AUD-002. The policy is the same shape as
-- every other tenant-scoped table (see 001_init.sql). A WITH CHECK clause is
-- added so an app-role session can only enqueue/claim jobs for its own tenant.
-- Claim/lock/retry/dead bookkeeping is performed through the owner role, which
-- bypasses RLS (the owner is a superuser in the default Compose image), so
-- cross-tenant queue coordination still works.
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'jobs'
          AND policyname = 'tenant_isolation_jobs'
    ) THEN
        CREATE POLICY tenant_isolation_jobs ON jobs
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

-- No explicit GRANT required: migration 003's
-- ``ALTER DEFAULT PRIVILEGES FOR ROLE engram ... GRANT ... ON TABLES TO engram_app``
-- auto-grants DML on tables the owner creates in future migrations. The owner
-- role (and superusers) bypass RLS by design for cross-tenant queue operations.
