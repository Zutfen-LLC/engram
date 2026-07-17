-- Pin remember-time authority separately from immutable candidate origin provenance.

CREATE TABLE IF NOT EXISTS candidate_ingest_executions (
    ingest_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    principal_id uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
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

DO $$
BEGIN
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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'candidate_ingest_executions'
          AND policyname = 'tenant_isolation_candidate_ingest_executions'
    ) THEN
        CREATE POLICY tenant_isolation_candidate_ingest_executions
            ON candidate_ingest_executions
            USING (tenant_id = current_setting('app.tenant_id')::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
    END IF;
END $$;

GRANT SELECT, INSERT ON candidate_ingest_executions TO engram_app;
REVOKE UPDATE, DELETE ON candidate_ingest_executions FROM engram_app;
