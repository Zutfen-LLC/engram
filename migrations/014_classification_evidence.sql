-- Server-attested classification and retention evidence receipts.
ALTER TABLE memory_items
    ADD COLUMN source_confidence_prior REAL,
    ADD COLUMN retention_confidence REAL,
    ADD COLUMN retention_disposition TEXT,
    ADD COLUMN retention_evidence_at TIMESTAMPTZ,
    ADD CONSTRAINT chk_source_confidence_prior_range CHECK (
        source_confidence_prior IS NULL OR source_confidence_prior BETWEEN 0.0 AND 1.0
    ),
    ADD CONSTRAINT chk_retention_confidence_range CHECK (
        retention_confidence IS NULL OR retention_confidence BETWEEN 0.0 AND 0.95
    ),
    ADD CONSTRAINT chk_retention_disposition CHECK (
        retention_disposition IS NULL OR retention_disposition IN
            ('retain', 'transient', 'noise', 'uncertain')
    ),
    ADD CONSTRAINT chk_retention_evidence_complete CHECK (
        (retention_confidence IS NULL AND retention_disposition IS NULL
            AND retention_evidence_at IS NULL)
        OR
        (retention_confidence IS NOT NULL AND retention_disposition IS NOT NULL
            AND retention_evidence_at IS NOT NULL)
    );

CREATE TABLE classification_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    memory_item_id UUID UNIQUE REFERENCES memory_items(id) ON DELETE SET NULL,
    content_hash TEXT NOT NULL,
    canonicalization_version TEXT NOT NULL,
    source_type TEXT NOT NULL,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    context_hash TEXT,
    context_length INTEGER,
    suggested_kind TEXT NOT NULL,
    suggested_wing TEXT,
    suggested_room TEXT,
    suggested_visibility TEXT,
    taxonomy_confidence REAL NOT NULL,
    retention_confidence REAL NOT NULL,
    retention_disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    provenance JSONB NOT NULL,
    classification_version TEXT NOT NULL,
    retention_policy_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT chk_classification_taxonomy_confidence CHECK (
        taxonomy_confidence BETWEEN 0.0 AND 0.95
    ),
    CONSTRAINT chk_classification_retention_confidence CHECK (
        retention_confidence BETWEEN 0.0 AND 0.95
    ),
    CONSTRAINT chk_classification_retention_disposition CHECK (
        retention_disposition IN ('retain', 'transient', 'noise', 'uncertain')
    ),
    CONSTRAINT chk_classification_context_length CHECK (
        context_length IS NULL OR context_length >= 0
    )
);

CREATE INDEX idx_classification_runs_tenant_content
    ON classification_runs (tenant_id, content_hash);
CREATE INDEX idx_classification_runs_expired_unbound
    ON classification_runs (tenant_id, expires_at) WHERE memory_item_id IS NULL;

ALTER TABLE classification_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_classification_runs ON classification_runs
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
ALTER TABLE classification_runs FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON classification_runs TO engram_app;
