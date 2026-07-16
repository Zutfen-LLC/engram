-- ENG-SCOPE-001: truthful scope invariants and safe write defaults.
-- Coordinated-maintenance migration: stop/drain all memory writers before
-- applying it, and do not run old application code against the migrated
-- schema. Application-only rollback is unsupported.
-- Safe to re-apply: the normalization predicate is self-limiting (matches
-- zero rows once already normalized), the default change is idempotent by
-- nature, and the CHECK constraint is added through a guarded existence
-- check + VALIDATE CONSTRAINT (a no-op once already validated). The workspace
-- FK replacement inspects the catalog and changes only the FK attached to
-- memory_items.workspace_id when its delete action is not already restrictive.
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
          AND conrelid = 'memory_items'::regclass
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

-- ============ D. Workspace deletion lifecycle ============
-- A memory association deliberately prevents workspace deletion. ON DELETE
-- SET NULL would conflict with the visibility CHECK for workspace-visible
-- memories and would also erase provenance for private/tenant/public memories
-- associated with a workspace. Operators must explicitly resolve associated
-- memories before deleting the workspace.
DO $$
DECLARE
    existing_fk_name text;
    existing_delete_action "char";
BEGIN
    SELECT c.conname, c.confdeltype
    INTO existing_fk_name, existing_delete_action
    FROM pg_constraint AS c
    JOIN pg_attribute AS source_col
      ON source_col.attrelid = c.conrelid
     AND source_col.attnum = ANY (c.conkey)
    JOIN pg_class AS target_table ON target_table.oid = c.confrelid
    JOIN pg_namespace AS target_ns ON target_ns.oid = target_table.relnamespace
    WHERE c.contype = 'f'
      AND c.conrelid = 'memory_items'::regclass
      AND source_col.attname = 'workspace_id'
      AND array_length(c.conkey, 1) = 1
      AND target_table.relname = 'workspaces'
      AND target_ns.nspname = current_schema()
    LIMIT 1;

    IF existing_fk_name IS NOT NULL AND existing_delete_action NOT IN ('a', 'r') THEN
        EXECUTE format('ALTER TABLE memory_items DROP CONSTRAINT %I', existing_fk_name);
        existing_fk_name := NULL;
    END IF;

    IF existing_fk_name IS NULL THEN
        ALTER TABLE memory_items
            ADD CONSTRAINT fk_memory_items_workspace_restrict
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT;
    END IF;
END
$$;
