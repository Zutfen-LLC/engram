-- ENG-SCOPE-001: truthful scope invariants and safe write defaults.
-- Safe to re-apply: the normalization predicate is self-limiting (matches
-- zero rows once already normalized), the default change is idempotent by
-- nature, and the CHECK constraint is added through a guarded existence
-- check + VALIDATE CONSTRAINT (a no-op once already validated).
--
-- Does not touch FORCE ROW LEVEL SECURITY, tenant isolation policies, app-role
-- grants, or append-first/update-delete restrictions on any table.

-- ============ A. Historical normalization ============
-- Existing visibility='workspace' AND workspace_id IS NULL rows have always
-- behaved tenant-wide (the read-eligibility fallback removed by this same
-- slice treated them that way). Relabel them truthfully to visibility=
-- 'tenant' — a semantic-preserving label correction, not a content mutation.
-- workspace_id is left untouched (already NULL).
--
-- The UPDATE predicate only ever matches unnormalized legacy rows: once a
-- row's visibility becomes 'tenant' it no longer satisfies
-- ``visibility = 'workspace'``, so re-running this statement updates zero
-- rows and the paired INSERT ... SELECT inserts zero events — idempotent by
-- construction, no auxiliary "already applied" bookkeeping required.
WITH normalized AS (
    UPDATE memory_items
    SET visibility = 'tenant'
    WHERE visibility = 'workspace' AND workspace_id IS NULL
    RETURNING id
)
INSERT INTO item_events (
    item_id, event_type, field_name, old_value, new_value,
    actor_principal_id, reason
)
SELECT
    id,
    'visibility_change',
    'visibility',
    'workspace',
    'tenant',
    NULL,
    'ENG-SCOPE-001 migration: normalize legacy workspace-null visibility'
FROM normalized;

-- ============ B. Database default ============
-- New rows default to private (ENG-SCOPE-001): missing/ambiguous scope must
-- never widen access. Application-layer defaulting (engram/memory_scope.py)
-- is authoritative for /v1/remember; this is the database-level backstop for
-- any other writer that omits visibility.
ALTER TABLE memory_items ALTER COLUMN visibility SET DEFAULT 'private';

-- ============ C. Database invariant ============
-- visibility='workspace' must always carry a real workspace_id. Added NOT
-- VALID (no blocking scan against concurrent writers) then validated in a
-- separate, guarded step — validating an already-valid constraint is a cheap
-- no-op, so this whole block is safe to re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_memitems_workspace_visibility_requires_workspace'
    ) THEN
        ALTER TABLE memory_items
            ADD CONSTRAINT chk_memitems_workspace_visibility_requires_workspace
            CHECK (visibility <> 'workspace' OR workspace_id IS NOT NULL)
            NOT VALID;
    END IF;
END
$$;

ALTER TABLE memory_items
    VALIDATE CONSTRAINT chk_memitems_workspace_visibility_requires_workspace;
