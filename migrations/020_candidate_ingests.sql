-- ENG-METER-004: server-owned candidate ingest identity.
-- Safe to re-apply: all objects are guarded or use IF NOT EXISTS.

CREATE UNIQUE INDEX IF NOT EXISTS idx_principals_tenant_identity
    ON principals (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_tenant_identity
    ON workspaces (tenant_id, id);

CREATE TABLE IF NOT EXISTS candidate_ingests (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id          UUID NOT NULL,
    workspace_id          UUID NULL,
    source_type           TEXT NOT NULL,
    content_hash          TEXT NOT NULL,
    client_correlation_id UUID NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_candidate_ingests_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_candidate_ingests_tenant_principal
        FOREIGN KEY (tenant_id, principal_id)
        REFERENCES principals (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_candidate_ingests_tenant_workspace
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES workspaces (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT chk_candidate_ingests_source_type CHECK (source_type IN (
        'manual', 'sync_turn', 'pre_compress', 'session_end',
        'import', 'migration', 'extraction'
    ))
);

CREATE INDEX IF NOT EXISTS idx_candidate_ingests_tenant_created
    ON candidate_ingests (tenant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_candidate_ingests_tenant_principal_created
    ON candidate_ingests (tenant_id, principal_id, created_at);

ALTER TABLE candidate_ingests ENABLE ROW LEVEL SECURITY;
ALTER TABLE candidate_ingests FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'candidate_ingests'
          AND policyname = 'tenant_isolation_candidate_ingests'
    ) THEN
        CREATE POLICY tenant_isolation_candidate_ingests ON candidate_ingests
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

GRANT SELECT, INSERT ON candidate_ingests TO engram_app;
REVOKE UPDATE, DELETE ON candidate_ingests FROM engram_app;

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS ingest_id UUID NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_usage_events_tenant_ingest'
    ) THEN
        ALTER TABLE usage_events
            ADD CONSTRAINT fk_usage_events_tenant_ingest
            FOREIGN KEY (tenant_id, ingest_id)
            REFERENCES candidate_ingests (tenant_id, id)
            ON DELETE RESTRICT;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_ingest_type_created
    ON usage_events (tenant_id, ingest_id, event_type, created_at);

ALTER TABLE classification_runs ADD COLUMN IF NOT EXISTS ingest_id UUID NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_classification_runs_tenant_ingest'
    ) THEN
        ALTER TABLE classification_runs
            ADD CONSTRAINT fk_classification_runs_tenant_ingest
            FOREIGN KEY (tenant_id, ingest_id)
            REFERENCES candidate_ingests (tenant_id, id)
            ON DELETE RESTRICT;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_classification_runs_ingest
    ON classification_runs (tenant_id, ingest_id)
    WHERE ingest_id IS NOT NULL;

GRANT SELECT, INSERT ON usage_events TO engram_app;
REVOKE UPDATE, DELETE ON usage_events FROM engram_app;
