-- Engram — Fair promotion reconciliation cursor (ENG-PROMOTION-003B, first slice)
-- 032_promotion_reconciliation_state.sql
--
-- The bounded lazy promotion pass on startup recall (settings.
-- startup_promotion_limit, default 20) selects live proposals ordered by
-- created_at ASC with a row LIMIT. Proposals that are terminal under current
-- policy — kind-blocked, below-threshold, missing evidence — stay at the head
-- of that ordering forever, so once there are >= limit such rows, every later
-- eligible proposal is starved from the lazy pass (issue #155, baseline
-- item 4: bounded-scan starvation).
--
-- This migration adds the persisted per-tenant keyset cursor the lazy pass
-- rotates over: each pass examines the next `limit` live proposals strictly
-- after (cursor_created_at, cursor_item_id) and wraps to the head when that
-- page is empty. Every live proposal is therefore examined at least once per
-- rotation without raising the per-pass bound or loading the full backlog.
-- Rows whose kind can never admit under current policy are additionally
-- excluded from the window outright (see engram.promotion), so they cannot
-- consume scan budget at all.
--
-- Additive only: rolling application code back leaves an unused table.
--
-- Run as: psql -f migrations/032_promotion_reconciliation_state.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

-- ============ 1. Table ============

CREATE TABLE IF NOT EXISTS promotion_reconciliation_state (
    -- One rotation cursor per tenant; deleting the tenant deletes its cursor.
    tenant_id         UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    -- Keyset position: strictly-after predicate over (created_at, id) of the
    -- last live proposal the bounded pass examined.
    cursor_created_at TIMESTAMPTZ NOT NULL,
    cursor_item_id    UUID NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ 2. Row Level Security ============
-- FORCE RLS: the table owner (migration role) is also subject to the policy.
-- Tenant-scoped only — the cursor is shared by all principals of the tenant
-- because the lazy pass reconciles the tenant's proposed set as a whole. A
-- missing app.tenant_id GUC exposes zero rows and another tenant can neither
-- read nor move this tenant's cursor.

ALTER TABLE promotion_reconciliation_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE promotion_reconciliation_state FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'promotion_reconciliation_state'
          AND policyname = 'tenant_isolation_promotion_reconciliation_state'
    ) THEN
        CREATE POLICY tenant_isolation_promotion_reconciliation_state
            ON promotion_reconciliation_state
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

-- ============ 3. Grants ============
-- The app role reads and advances its own tenant's cursor (SELECT/INSERT/
-- UPDATE under the RLS policy above). The cursor is internal scheduling
-- state and is not deletable from the application. Migration 003's
-- ALTER DEFAULT PRIVILEGES already granted DELETE on this newly-created
-- table (it is owned by the migration role); revoke it explicitly.

GRANT SELECT, INSERT, UPDATE ON promotion_reconciliation_state TO engram_app;
REVOKE DELETE ON promotion_reconciliation_state FROM engram_app;
