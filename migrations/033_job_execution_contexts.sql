-- Durable execution-authority records for non-ingest job producers (#155).
--
-- ``candidate_ingest_executions`` (migration 025) pins the remember-time
-- execution authority of an ingest, so ingest-bound queued work (embedding,
-- classification, promotion) can reconstruct the exact profile boundary that
-- authorized the request. Producers that are not an ingest -- starting with
-- the manual ``promotion.evaluate`` trigger (POST /v1/admin/items/{id}/evaluate)
-- -- have no ingest to hang authority on, so this table is the generic analog:
-- one immutable row per authorized producer request, recording the pinned
-- memory-context identity (principal, API key, profile revision) under which
-- the request passed its write-eligibility boundary.
--
-- Unlike candidate_ingest_executions this table carries principal_id: there is
-- no ingest row whose principal could be authoritative, so the row itself must
-- record who authorized the request. The row is a reference to pinned,
-- immutable authorization state (revision rows are append-only), never a copy
-- of mutable policy: workers re-load the pinned revision at execution time
-- exactly as memory_context_from_ingest does.
--
-- Safe to re-apply: every object is guarded or uses IF NOT EXISTS, and the RLS
-- policy is recreated idempotently.

CREATE TABLE IF NOT EXISTS job_execution_contexts (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    api_key_id uuid,
    memory_profile_id uuid,
    memory_profile_revision_id uuid,
    memory_context_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_job_execution_contexts_profile_pair CHECK (
        (memory_profile_id IS NULL) = (memory_profile_revision_id IS NULL)
    )
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_job_execution_contexts_tenant'
          AND conrelid = 'job_execution_contexts'::regclass
    ) THEN
        ALTER TABLE job_execution_contexts
            ADD CONSTRAINT fk_job_execution_contexts_tenant
            FOREIGN KEY (tenant_id)
            REFERENCES tenants (id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_job_execution_contexts_principal'
          AND conrelid = 'job_execution_contexts'::regclass
    ) THEN
        ALTER TABLE job_execution_contexts
            ADD CONSTRAINT fk_job_execution_contexts_principal
            FOREIGN KEY (tenant_id, principal_id)
            REFERENCES principals (tenant_id, id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_job_execution_contexts_api_key'
          AND conrelid = 'job_execution_contexts'::regclass
    ) THEN
        ALTER TABLE job_execution_contexts
            ADD CONSTRAINT fk_job_execution_contexts_api_key
            FOREIGN KEY (tenant_id, api_key_id)
            REFERENCES api_keys (tenant_id, id) ON DELETE SET NULL (api_key_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_job_execution_contexts_profile_revision'
          AND conrelid = 'job_execution_contexts'::regclass
    ) THEN
        ALTER TABLE job_execution_contexts
            ADD CONSTRAINT fk_job_execution_contexts_profile_revision
            FOREIGN KEY (memory_profile_revision_id, memory_profile_id, tenant_id)
            REFERENCES memory_profile_revisions (id, profile_id, tenant_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

ALTER TABLE job_execution_contexts ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_execution_contexts FORCE ROW LEVEL SECURITY;

-- Same tenant_isolation convention as candidate_ingest_executions (migration
-- 025): the two-argument current_setting returns NULL when the GUC is unset
-- instead of raising, so a missing tenant context simply sees zero rows.
-- Recreate idempotently if a prior revision installed another form.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'job_execution_contexts'
          AND policyname = 'tenant_isolation_job_execution_contexts'
    ) THEN
        DROP POLICY tenant_isolation_job_execution_contexts
            ON job_execution_contexts;
    END IF;
    CREATE POLICY tenant_isolation_job_execution_contexts
        ON job_execution_contexts
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
END $$;

-- Append-only from the application's perspective, mirroring the usage-event
-- and execution-context conventions: the app role may reference the row but
-- never rewrite or erase the recorded authority.
GRANT SELECT, INSERT ON job_execution_contexts TO engram_app;
REVOKE UPDATE, DELETE ON job_execution_contexts FROM engram_app;
