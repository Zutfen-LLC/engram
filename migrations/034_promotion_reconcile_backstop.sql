-- Engram — Bounded promotion reconciliation backstop (ENG-PROMOTION-003B4 /
-- issue #155).
-- 034_promotion_reconcile_backstop.sql
--
-- The reconciliation backstop is orchestration, not a second promotion
-- implementation: it discovers live proposals whose targeted evaluation work
-- is missing/dead and enqueues canonical `promotion.evaluate` jobs (plus
-- re-enqueues the existing async `classification.refine` contract for
-- provider recovery). It never decides or performs `proposed -> active`
-- itself, and startup recall's lazy promotion pass is untouched in this
-- slice (removal is B5, after shadow parity).
--
-- This migration adds the smallest durable scheduler-only state the backstop
-- needs, plus indexes that make bounded item and queue lookups deterministic
-- over live proposals (this backstop *and* #164's startup rotation) a truly
-- index-bounded scan instead of a filter+sort behind a LIMIT:
--
-- * `promotion_reconcile_state` — one row per tenant holding the backstop's
--   own keyset cursor, distinct from #164's `promotion_reconciliation_state`
--   (the startup-rotation cursor). The differences are load-bearing: the
--   backstop cursor is NULLABLE (NULL = "next pass reads from the head",
--   which is exactly what a policy-change/operator reset must express), it
--   carries a `cursor_epoch` bumped on every reset so a stale in-flight
--   pass cannot overwrite a post-reset position, a `kind_policy_revision`
--   counter providing the stable revision identity for policy-change
--   dedupe/replay, and content-free last-pass counts for diagnostics. This
--   is orchestration bookkeeping, never an authoritative promotion
--   assessment (#159 owns that) and never memory content.
-- * `promotion_reconcile_terminal` — one identifier/generation marker per
--   terminal item, invalidated by relevant state/evidence events and ignored
--   after a reset epoch. It stores no assessment or policy result.
-- * `promotion_reconcile_scheduler_state` — owner-only, content-free keyset
--   cursors that bound automatic and CLI tenant enumeration across restarts.
-- * `idx_memitems_proposed_rotation` — partial index on
--   (tenant_id, created_at, id) WHERE review_status='proposed' AND
--   valid_to IS NULL, serving the strictly-after keyset predicate with
--   ORDER BY created_at, id ... LIMIT n directly from the index.
-- * `idx_jobs_reconcile_item_state` — targeted queue-state probes keyed by
--   each item in that bounded window; unrelated history cannot hide coverage.
--
-- Additive only: rolling application code back leaves an unused table and an
-- unused index. FORCE RLS from the first migration for this tenant-scoped
-- state; app-role grants limited to what the runtime needs.
--
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

-- ============ 1. Table ============

CREATE TABLE IF NOT EXISTS promotion_reconcile_state (
    -- One backstop bookkeeping row per tenant; tenant deletion cascades.
    tenant_id              UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    -- Keyset position of the last live proposal the last bounded pass
    -- examined. NULL (the default) means the next pass reads from the head.
    cursor_created_at      TIMESTAMPTZ,
    cursor_item_id         UUID,
    -- Bumped on every cursor reset; a pass may only advance the cursor while
    -- the epoch it read is still current (stale passes no-op instead).
    cursor_epoch           BIGINT NOT NULL DEFAULT 0,
    -- Monotonic revision identity for admission-affecting memory-kind
    -- changes; drives policy-change trigger provenance and dedupe/replay.
    kind_policy_revision   BIGINT NOT NULL DEFAULT 0,
    -- Content-free diagnostics from the last completed pass.
    last_pass_at           TIMESTAMPTZ,
    last_pass_reason       TEXT,
    last_pass_trigger_id   TEXT,
    last_window_size       INTEGER,
    last_wrapped           BOOLEAN NOT NULL DEFAULT FALSE,
    last_evaluations_enqueued INTEGER,
    last_dead_found        INTEGER,
    last_missing_found     INTEGER,
    last_recovery_enqueued INTEGER,
    last_terminal_skipped  INTEGER,
    last_healthy_skipped   INTEGER,
    last_suppressed        INTEGER,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Finite request chains have independent cursor progress.  In particular a
-- provider-recovery request cannot lose its reason-specific inspection work
-- because the periodic backstop (or another request) advanced a tenant-wide
-- cursor.  The durable terminal row also makes an explicit request_id honest:
-- completed and failed identities remain observable after queue history ages.
CREATE TABLE IF NOT EXISTS promotion_reconcile_chains (
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    reason             TEXT NOT NULL,
    trigger_id         TEXT NOT NULL,
    cursor_created_at  TIMESTAMPTZ,
    cursor_item_id     UUID,
    status             TEXT NOT NULL DEFAULT 'requested'
                       CHECK (status IN ('requested', 'running', 'completed', 'failed')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, reason, trigger_id)
);

-- A row means only "the backstop observed this item as terminal in this
-- cursor epoch".  It is scheduler suppression, not a durable promotion
-- assessment: no blocker, score, threshold, content, or decision is stored.
CREATE TABLE IF NOT EXISTS promotion_reconcile_terminal (
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    item_id        UUID NOT NULL,
    cursor_epoch   BIGINT NOT NULL,
    observed_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, item_id),
    CONSTRAINT fk_promotion_reconcile_terminal_item
        FOREIGN KEY (tenant_id, item_id)
        REFERENCES memory_items(tenant_id, id) ON DELETE CASCADE
);

-- Cross-tenant, owner-only, content-free scheduling position.  Separate keys
-- let the perpetual bootstrap and explicit paginated CLI requests resume
-- independently after restart without enumerating the tenant table.
CREATE TABLE IF NOT EXISTS promotion_reconcile_scheduler_state (
    scheduler_key       TEXT PRIMARY KEY,
    cursor_created_at   TIMESTAMPTZ,
    cursor_tenant_id    UUID,
    completed           BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ 2. Row Level Security ============
-- FORCE RLS: the table owner (migration role) is also subject to the policy.
-- Tenant-scoped only — the backstop reconciles the tenant's proposed set as
-- a whole, so the row is shared by all principals of the tenant (worker
-- handlers run under the tenant's routed app-role context). A missing
-- app.tenant_id GUC exposes zero rows and another tenant can neither read
-- nor move this tenant's state.

ALTER TABLE promotion_reconcile_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE promotion_reconcile_state FORCE ROW LEVEL SECURITY;
ALTER TABLE promotion_reconcile_chains ENABLE ROW LEVEL SECURITY;
ALTER TABLE promotion_reconcile_chains FORCE ROW LEVEL SECURITY;
ALTER TABLE promotion_reconcile_terminal ENABLE ROW LEVEL SECURITY;
ALTER TABLE promotion_reconcile_terminal FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'promotion_reconcile_state'
          AND policyname = 'tenant_isolation_promotion_reconcile_state'
    ) THEN
        CREATE POLICY tenant_isolation_promotion_reconcile_state
            ON promotion_reconcile_state
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'promotion_reconcile_chains'
          AND policyname = 'tenant_isolation_promotion_reconcile_chains'
    ) THEN
        CREATE POLICY tenant_isolation_promotion_reconcile_chains
            ON promotion_reconcile_chains
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'promotion_reconcile_terminal'
          AND policyname = 'tenant_isolation_promotion_reconcile_terminal'
    ) THEN
        CREATE POLICY tenant_isolation_promotion_reconcile_terminal
            ON promotion_reconcile_terminal
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

-- ============ 3. Grants ============
-- The app role reads and advances its own tenant's backstop state
-- (SELECT/INSERT/UPDATE under the RLS policy above). The state is internal
-- scheduling bookkeeping and is not deletable from the application.
-- Migration 003's ALTER DEFAULT PRIVILEGES already granted DELETE on this
-- newly-created table (it is owned by the migration role); revoke it
-- explicitly.

GRANT SELECT, INSERT, UPDATE ON promotion_reconcile_state TO engram_app;
REVOKE DELETE ON promotion_reconcile_state FROM engram_app;
GRANT SELECT, INSERT, UPDATE ON promotion_reconcile_chains TO engram_app;
REVOKE DELETE ON promotion_reconcile_chains FROM engram_app;
GRANT SELECT, INSERT, UPDATE ON promotion_reconcile_terminal TO engram_app;
REVOKE DELETE ON promotion_reconcile_terminal FROM engram_app;

-- Global scheduler state is intentionally not tenant-RLS data: it contains
-- only a scheduler key and tenant keyset position, and is used solely by the
-- owner-side queue coordinator.  Revoke migration-003 default privileges so
-- item-level app sessions cannot inspect or mutate global coordination.
REVOKE ALL ON promotion_reconcile_scheduler_state FROM engram_app;

-- ============ 4. Rotation index ============
-- Serves the bounded keyset rotation over live proposals (tenant_id = :t AND
-- review_status = 'proposed' AND valid_to IS NULL AND (created_at, id)
-- strictly after the cursor, ORDER BY created_at, id, LIMIT n) directly from
-- the index, for both this backstop and the #164 startup rotation, so the
-- per-pass bound is a true index bound rather than a LIMIT over a
-- filter+sort of the whole backlog. The proposed-only partial predicate
-- keeps the index proportional to the rotation set (the working set), not to
-- the whole live corpus; the planner may legitimately prefer the existing,
-- broader idx_memitems_backfill (migration 002, (tenant_id, created_at, id)
-- WHERE valid_to IS NULL) for the same shape — either choice is bounded.

CREATE INDEX IF NOT EXISTS idx_memitems_proposed_rotation
    ON memory_items (tenant_id, created_at, id)
    WHERE review_status = 'proposed' AND valid_to IS NULL;

-- Each bounded reconciliation item uses indexed EXISTS probes for current
-- queue coverage.  Historical volume for unrelated items therefore cannot
-- hide a qualifying job or turn a pass into a history scan.
CREATE INDEX IF NOT EXISTS idx_jobs_reconcile_item_state
    ON jobs (
        tenant_id,
        (payload->>'memory_item_id'),
        job_type,
        status,
        run_after,
        (payload->>'classification_run_id')
    )
    WHERE job_type IN ('promotion.evaluate', 'promotion.path_a', 'classification.refine')
      AND status IN ('pending', 'running', 'dead');

-- Relevant state changes make a prior terminal observation stale.  Database
-- triggers keep invalidation atomic even when a targeted producer crashes
-- before its queue enqueue, or an operator performs a supported direct SQL
-- change.  The bounded pass locks its selected memory rows while assessing,
-- closing insert-after-invalidation races for FK-backed event/evidence rows.
CREATE OR REPLACE FUNCTION invalidate_promotion_reconcile_terminal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    affected_tenant UUID;
    affected_item UUID;
BEGIN
    affected_tenant := NEW.tenant_id;
    IF TG_TABLE_NAME = 'classification_runs' THEN
        affected_item := NEW.memory_item_id;
        IF affected_item IS NULL AND TG_OP = 'UPDATE' THEN
            affected_item := OLD.memory_item_id;
        END IF;
    ELSE
        affected_item := NEW.item_id;
    END IF;
    IF affected_tenant IS NOT NULL AND affected_item IS NOT NULL THEN
        DELETE FROM promotion_reconcile_terminal
        WHERE tenant_id = affected_tenant AND item_id = affected_item;
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION invalidate_promotion_reconcile_terminal() FROM PUBLIC, engram_app;

DROP TRIGGER IF EXISTS trg_promotion_reconcile_item_event ON item_events;
CREATE TRIGGER trg_promotion_reconcile_item_event
AFTER INSERT ON item_events
FOR EACH ROW EXECUTE FUNCTION invalidate_promotion_reconcile_terminal();

DROP TRIGGER IF EXISTS trg_promotion_reconcile_feedback ON feedback_events;
CREATE TRIGGER trg_promotion_reconcile_feedback
AFTER INSERT ON feedback_events
FOR EACH ROW EXECUTE FUNCTION invalidate_promotion_reconcile_terminal();

DROP TRIGGER IF EXISTS trg_promotion_reconcile_classification ON classification_runs;
CREATE TRIGGER trg_promotion_reconcile_classification
AFTER INSERT OR UPDATE OF memory_item_id, bound_at ON classification_runs
FOR EACH ROW EXECUTE FUNCTION invalidate_promotion_reconcile_terminal();

CREATE OR REPLACE FUNCTION invalidate_promotion_reconcile_terminal_item()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    DELETE FROM promotion_reconcile_terminal
    WHERE tenant_id = NEW.tenant_id AND item_id = NEW.id;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION invalidate_promotion_reconcile_terminal_item() FROM PUBLIC, engram_app;

DROP TRIGGER IF EXISTS trg_promotion_reconcile_item_state ON memory_items;
CREATE TRIGGER trg_promotion_reconcile_item_state
AFTER UPDATE OF review_status, valid_to, superseded_by, kind, memory_confidence,
    source_trust, source_confidence_prior, retention_confidence,
    retention_disposition, retention_evidence_at, conflict_resolution_status,
    human_verified
ON memory_items
FOR EACH ROW EXECUTE FUNCTION invalidate_promotion_reconcile_terminal_item();
