-- Pin remember-time authority separately from immutable candidate origin provenance.
--
-- The execution row is 1:1 with its candidate ingest (PRIMARY KEY ingest_id, and
-- a composite FK (tenant_id, ingest_id) -> candidate_ingests). The ingest's own
-- principal_id is therefore the authoritative principal, so this table does NOT
-- duplicate principal_id: an execution row cannot disagree with its ingest about
-- the principal because the principal is not stored here at all. Workers derive
-- it from the ingest loaded through the same composite key.
--
-- Safe to re-apply: every object is guarded or uses IF NOT EXISTS, and the RLS
-- policy is recreated idempotently.

CREATE TABLE IF NOT EXISTS candidate_ingest_executions (
    ingest_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    api_key_id uuid,
    memory_profile_id uuid,
    memory_profile_revision_id uuid,
    memory_context_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_candidate_ingest_executions_ingest
        FOREIGN KEY (tenant_id, ingest_id)
        REFERENCES candidate_ingests (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT chk_candidate_ingest_executions_profile_pair CHECK (
        (memory_profile_id IS NULL) = (memory_profile_revision_id IS NULL)
    )
);

-- Prior revisions of this table carried a principal_id column. It was redundant
-- (the composite ingest FK already pins the ingest whose principal is
-- authoritative) and was not relationally tied to that ingest's principal. Drop
-- the column and any legacy constraint that referenced it, idempotently.
ALTER TABLE candidate_ingest_executions
    DROP CONSTRAINT IF EXISTS candidate_ingest_executions_principal_id_fkey;
ALTER TABLE candidate_ingest_executions DROP COLUMN IF EXISTS principal_id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_candidate_ingest_executions_tenant'
          AND conrelid = 'candidate_ingest_executions'::regclass
    ) THEN
        ALTER TABLE candidate_ingest_executions
            ADD CONSTRAINT fk_candidate_ingest_executions_tenant
            FOREIGN KEY (tenant_id)
            REFERENCES tenants (id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_candidate_ingest_executions_api_key'
          AND conrelid = 'candidate_ingest_executions'::regclass
    ) THEN
        ALTER TABLE candidate_ingest_executions
            ADD CONSTRAINT fk_candidate_ingest_executions_api_key
            FOREIGN KEY (tenant_id, api_key_id)
            REFERENCES api_keys (tenant_id, id) ON DELETE SET NULL (api_key_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_candidate_ingest_executions_profile_revision'
          AND conrelid = 'candidate_ingest_executions'::regclass
    ) THEN
        ALTER TABLE candidate_ingest_executions
            ADD CONSTRAINT fk_candidate_ingest_executions_profile_revision
            FOREIGN KEY (memory_profile_revision_id, memory_profile_id, tenant_id)
            REFERENCES memory_profile_revisions (id, profile_id, tenant_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_candidate_ingest_executions_context
    ON candidate_ingest_executions (
        tenant_id, memory_profile_id, memory_profile_revision_id
    );

ALTER TABLE candidate_ingest_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE candidate_ingest_executions FORCE ROW LEVEL SECURITY;

-- Match the tenant_isolation convention used by candidate_ingests (migration
-- 020) and the rest of the schema: the two-argument current_setting returns
-- NULL when the GUC is unset instead of raising, so a missing tenant context
-- simply sees zero rows. Recreate idempotently if a prior revision installed
-- the single-argument form.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'candidate_ingest_executions'
          AND policyname = 'tenant_isolation_candidate_ingest_executions'
    ) THEN
        DROP POLICY tenant_isolation_candidate_ingest_executions
            ON candidate_ingest_executions;
    END IF;
    CREATE POLICY tenant_isolation_candidate_ingest_executions
        ON candidate_ingest_executions
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
END $$;

GRANT SELECT, INSERT ON candidate_ingest_executions TO engram_app;
REVOKE UPDATE, DELETE ON candidate_ingest_executions FROM engram_app;
