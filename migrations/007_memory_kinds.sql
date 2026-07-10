-- Engram — Governed memory-kind registry (ENG-AUD-010)
-- 007_memory_kinds.sql
--
-- Audit finding F17: ``chk_kind`` hard-coded the kind vocabulary at the DDL
-- level (7 values), contradicting the design's "tenant-configurable taxonomy"
-- claim and omitting the design kinds ``procedure``/``summary``. The kind
-- vocabulary was ALSO duplicated in Python (_DEFAULT_KIND_TAXONOMY), in ad hoc
-- kind-name sets (_SINGLETON_KINDS, _CCA_KINDS), and in a bare-string
-- review-status override (``suggested_kind == "decision"``).
--
-- This migration replaces all of that with a tenant-scoped ``memory_kinds``
-- registry: the source of truth for which kinds are valid and what behavior
-- (singleton supersession, initial review status, disputed-recall inclusion)
-- they carry. Builtin kinds are seeded per tenant; tenants may add governed
-- custom kinds via the admin API without a schema migration.
--
-- Run as: psql -f migrations/007_memory_kinds.sql  (owner/migration role)
-- Safe to re-apply: every statement is idempotent (IF NOT EXISTS / guarded).

-- ============ 1. memory_kinds table ============

CREATE TABLE IF NOT EXISTS memory_kinds (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    description     TEXT,
    is_builtin      BOOLEAN NOT NULL DEFAULT FALSE,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    singleton       BOOLEAN NOT NULL DEFAULT FALSE,
    stays_in_recall_when_disputed BOOLEAN NOT NULL DEFAULT FALSE,
    requires_review BOOLEAN NOT NULL DEFAULT FALSE,
    default_importance DOUBLE PRECISION,
    sort_order      INTEGER NOT NULL DEFAULT 100,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, name),
    CONSTRAINT chk_memory_kind_name CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$'),
    CONSTRAINT chk_memory_kind_importance CHECK (
        default_importance IS NULL OR (default_importance >= 0.0 AND default_importance <= 1.0)
    )
);

CREATE INDEX IF NOT EXISTS idx_memory_kinds_tenant_enabled
    ON memory_kinds (tenant_id, enabled, sort_order);

-- ============ 2. Seed builtin kinds for every existing tenant ============
--
-- Behavior mapping (documented in the PR — audited against actual code, not
-- blindly copied from the design doc's suggested table):
--
--   fact         singleton=F stays=F requires_review=F  — no prior special-casing.
--   preference   singleton=T stays=F requires_review=F  — matches _SINGLETON_KINDS.
--   doctrine     singleton=F stays=T requires_review=T  — design.md documents
--                doctrine/invariant as staying in recall while disputed (never
--                actually implemented — this migration implements it); promoted
--                to requires_review=T as a high-stakes governed kind.
--   decision     singleton=F stays=F requires_review=T  — generalizes the prior
--                bare `suggested_kind == "decision"` review-status override
--                (memory.py) from "classifier-suggested only" to "any write of
--                this kind", per this ticket's explicit instruction that the
--                kind's governance flag — not the classification path — decides.
--   invariant    singleton=T stays=T requires_review=T  — matches _SINGLETON_KINDS
--                plus the same disputed-recall/high-stakes treatment as doctrine.
--   observation  singleton=F stays=F requires_review=F  — no prior special-casing.
--   diary_entry  singleton=F stays=F requires_review=F  — diary write path locks
--                kind/visibility itself; no registry-driven behavior needed.
--   procedure    singleton=F stays=F requires_review=F  — new design kind (F17).
--   summary      singleton=F stays=F requires_review=F  — new design kind (F17).

INSERT INTO memory_kinds (
    tenant_id, name, display_name, description, is_builtin, enabled,
    singleton, stays_in_recall_when_disputed, requires_review,
    default_importance, sort_order
)
SELECT
    t.id, k.name, k.display_name, k.description, TRUE, TRUE,
    k.singleton, k.stays, k.requires_review, k.default_importance, k.sort_order
FROM tenants t
CROSS JOIN (VALUES
    ('fact',        'Fact',        'An observed or stated fact.',
        FALSE, FALSE, FALSE, 0.5::double precision, 10),
    ('preference',  'Preference',  'A stated preference or convention.',
        TRUE,  FALSE, FALSE, 0.5, 20),
    ('doctrine',    'Doctrine',    'A standing policy or rule that governs behavior.',
        FALSE, TRUE,  TRUE,  0.7, 30),
    ('decision',    'Decision',    'A decision that was made and should be remembered.',
        FALSE, FALSE, TRUE,  0.6, 40),
    ('invariant',   'Invariant',   'A rule that must always hold; violations are high-stakes.',
        TRUE,  TRUE,  TRUE,  0.8, 50),
    ('observation', 'Observation', 'Something noticed but not yet trusted or reviewed.',
        FALSE, FALSE, FALSE, 0.4, 60),
    ('diary_entry', 'Diary Entry', 'A private agent diary entry.',
        FALSE, FALSE, FALSE, 0.4, 70),
    ('procedure',   'Procedure',   'A how-to, runbook, or operational procedure.',
        FALSE, FALSE, FALSE, 0.5, 80),
    ('summary',     'Summary',     'A condensed summary derived from other memories.',
        FALSE, FALSE, FALSE, 0.4, 90)
) AS k(name, display_name, description, singleton, stays, requires_review, default_importance, sort_order)
ON CONFLICT (tenant_id, name) DO NOTHING;

-- ============ 3. Auto-register any existing kind not covered above ============
-- Defensive: chk_kind already restricted memory_items.kind to the 7 legacy
-- values, all of which are seeded above, so this should be a no-op on any
-- database that only ever went through chk_kind. It exists so the FK added in
-- step 5 can never fail on pre-existing data, even if a database was seeded
-- out-of-band.
INSERT INTO memory_kinds (
    tenant_id, name, display_name, description, is_builtin, enabled,
    singleton, stays_in_recall_when_disputed, requires_review,
    default_importance, sort_order
)
SELECT DISTINCT
    mi.tenant_id, mi.kind, initcap(replace(mi.kind, '_', ' ')),
    'Auto-registered from existing data during ENG-AUD-010 migration.',
    FALSE, TRUE, FALSE, FALSE, FALSE, NULL::double precision, 100
FROM memory_items mi
WHERE NOT EXISTS (
    SELECT 1 FROM memory_kinds mk
    WHERE mk.tenant_id = mi.tenant_id AND mk.name = mi.kind
)
ON CONFLICT (tenant_id, name) DO NOTHING;

-- ============ 4. Drop chk_kind — the registry is now the authority ============

ALTER TABLE memory_items DROP CONSTRAINT IF EXISTS chk_kind;

-- ============ 5. Composite FK: memory_items(tenant_id, kind) -> memory_kinds(tenant_id, name) ============

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_memory_items_kind') THEN
        ALTER TABLE memory_items
            ADD CONSTRAINT fk_memory_items_kind
            FOREIGN KEY (tenant_id, kind) REFERENCES memory_kinds (tenant_id, name);
    END IF;
END
$$;

-- ============ 6. RLS ============
-- Tenant isolation consistent with ENG-AUD-002 — same shape as every other
-- tenant-scoped table (see 001_init.sql, 005_jobs.sql). No explicit GRANT is
-- required: migration 003's ``ALTER DEFAULT PRIVILEGES FOR ROLE engram ...``
-- auto-grants DML on tables the owner creates in future migrations.

ALTER TABLE memory_kinds ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_kinds FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'memory_kinds'
          AND policyname = 'tenant_isolation_memory_kinds'
    ) THEN
        CREATE POLICY tenant_isolation_memory_kinds ON memory_kinds
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;
