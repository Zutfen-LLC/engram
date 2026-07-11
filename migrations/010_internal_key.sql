-- Engram — server-owned internal principal identity (V2-BL-003B)
-- 010_internal_key.sql
--
-- Audit finding: V2-BL-003A identified the trusted internal review actor by
-- the mutable, caller-facing principal name ``system``. Because the
-- ``principals`` table allows agent/user/admin types to share that name, and
-- the admin API permits creating principals with arbitrary names/types, an
-- ordinary agent principal named ``system`` could be returned by the trusted-
-- actor upsert (keyed on ``(tenant_id, name)``). Promotion events would then
-- be attributed to that agent — recreating the false self-approval audit trail
-- V2-BL-003A was designed to eliminate.
--
-- This migration adds a nullable, server-owned ``internal_key`` column to
-- ``principals``. The trusted review actor is identified by
-- ``internal_key = 'review_automation'`` (per tenant), NOT by its display name.
-- A CHECK constraint restricts non-null internal keys to ``type = 'system'``,
-- and a partial unique index on ``(tenant_id, internal_key)`` guarantees one
-- canonical principal per tenant and internal role.
--
-- Existing principals — including any named ``system`` — are NOT modified:
-- they remain ``internal_key = NULL`` and ordinary. The trusted actor is
-- created lazily by the server on first use with a generated display name.
--
-- Run as: psql -f migrations/010_internal_key.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).
--
-- DEPLOY ORDERING: This migration MUST be applied (via `engram init-db`) before
-- deploying code that references the ``internal_key`` column. The auth lookup
-- queries (engram/auth.py), the trusted-actor resolver (engram/promotion.py),
-- and the bootstrap-key CLI (engram/cli.py) all SELECT ``internal_key``; if
-- the column does not exist, those queries raise UndefinedColumn and break all
-- authentication (including the auth-disabled fallback). This is the standard
-- migration-first ordering for this project — see docs/deployment.md
-- (Upgrades & migrations).

-- ============ 1. internal_key column ============

ALTER TABLE principals ADD COLUMN IF NOT EXISTS internal_key TEXT;

-- ============ 2. CHECK: internal_key only for system principals ============
-- A non-null internal_key marks a server-owned internal identity. Only
-- ``type = 'system'`` principals may carry one, so an agent/user/admin row
-- can never be (or become) an internal actor. NULL is always allowed.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_schema = 'public'
          AND table_name = 'principals'
          AND constraint_name = 'principals_internal_key_system_check'
    ) THEN
        ALTER TABLE principals
            ADD CONSTRAINT principals_internal_key_system_check
            CHECK (internal_key IS NULL OR type = 'system');
    END IF;
END
$$;

-- ============ 3. Partial unique index on (tenant_id, internal_key) ============
-- One canonical internal principal per tenant per internal role. NULL
-- internal_key rows (ordinary principals) are excluded so any number of them
-- coexist. This index is the concurrency authority for trusted-actor creation:
-- concurrent first use cannot create duplicate rows.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_principals_internal_key'
    ) THEN
        CREATE UNIQUE INDEX idx_principals_internal_key
            ON principals (tenant_id, internal_key)
            WHERE internal_key IS NOT NULL;
    END IF;
END
$$;

-- No privilege/RLS changes: the new column belongs to an existing table, and a
-- table-level GRANT (migration 003 grants SELECT/INSERT/UPDATE/DELETE on all
-- principals columns, present and future) already covers it. RLS continues to
-- isolate principals by tenant as before; internal-key resolution runs through
-- the owner role (bypasses RLS) the same way principal/key resolution already
-- does, so the trusted-actor upsert sees the canonical row regardless of the
-- caller's tenant context.
--
-- No data migration: existing principals keep internal_key = NULL (the column
-- default). Any existing principal named ``system`` remains an ordinary
-- principal; the server creates a new canonical internal principal lazily.