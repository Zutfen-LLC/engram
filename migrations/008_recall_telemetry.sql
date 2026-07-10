-- Engram — bounded recall candidate selection + async telemetry (ENG-AUD-011 / F18)
-- 008_recall_telemetry.sql
--
-- Audit finding: startup recall loaded the entire eligible corpus into Python
-- and wrote recall-counter updates (last_recalled_at, startup_recall_count) in
-- the same transaction as the read, which made recall cost grow linearly with
-- corpus size and made the core recall query unsafe to route to a read
-- replica. See docs/plans/engram-memory-audit-2026-07.md F18.
--
-- This migration:
--   1. Adds recall_logs.telemetry_applied_at — the idempotency claim column
--      for the async recall.telemetry job (engram/worker.py
--      handle_recall_telemetry). NULL until the worker has applied this
--      recall's counter/timestamp updates; the worker claims by
--      transactionally setting it together with the item updates in one
--      commit, so a retried/redelivered job that finds it already set is a
--      safe no-op. See engram/models.py RecallLog for the full rationale.
--   2. Adds supporting indexes for the new SQL-side candidate sub-queries
--      (freshest / least-recently-recalled / importance-ordered scans),
--      scoped by tenant + review_status + valid_to, matching the predicate
--      shape used throughout engram/recall.py's candidate selection.
--
-- Run as: psql -f migrations/008_recall_telemetry.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

-- ============ 1. Telemetry idempotency claim column ============

ALTER TABLE recall_logs ADD COLUMN IF NOT EXISTS telemetry_applied_at TIMESTAMPTZ;

-- ============ 2. Candidate sub-query indexes ============
-- All three mirror the existing tenant/review_status/valid_to shape already
-- indexed for the primary active-item scan; they add the ORDER BY column so
-- the freshest / least-recently-recalled / highest-importance sub-pools in
-- engram.recall._fetch_startup_candidates can each run as an index scan with
-- a small LIMIT rather than a sequential scan + sort.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_memitems_recall_freshness'
    ) THEN
        CREATE INDEX idx_memitems_recall_freshness
            ON memory_items (tenant_id, valid_from DESC, created_at DESC)
            WHERE valid_to IS NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_memitems_recall_importance'
    ) THEN
        CREATE INDEX idx_memitems_recall_importance
            ON memory_items (tenant_id, importance DESC)
            WHERE valid_to IS NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_memitems_recall_least_recalled'
    ) THEN
        CREATE INDEX idx_memitems_recall_least_recalled
            ON memory_items (tenant_id, last_recalled_at NULLS FIRST)
            WHERE valid_to IS NULL;
    END IF;
END
$$;
