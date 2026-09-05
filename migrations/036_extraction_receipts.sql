-- Extraction receipts do not participate in promotion policy.
CREATE TABLE IF NOT EXISTS extraction_runs (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    principal_id UUID NOT NULL REFERENCES principals(id),
    workspace_id UUID REFERENCES workspaces(id),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    receipt JSONB NOT NULL,
    receipt_hash TEXT NOT NULL CHECK (receipt_hash ~ '^sha256:[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES principals(tenant_id, id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id),
    UNIQUE NULLS NOT DISTINCT (tenant_id, principal_id, workspace_id, idempotency_key),
    CHECK ((receipt->>'schema_version' = 'engram.extraction.v1') IS TRUE),
    CHECK ((receipt->>'run_id' = id::text) IS TRUE),
    CHECK ((receipt->>'tenant_id' = tenant_id::text) IS TRUE),
    CHECK ((receipt->>'principal_id' = principal_id::text) IS TRUE),
    CHECK ((receipt->>'workspace_id') IS NOT DISTINCT FROM workspace_id::text),
    CHECK ((receipt->>'mode' = 'write_proposed') IS TRUE),
    CHECK (NOT (receipt ?| ARRAY['risk', 'consequence', 'admission']))
);
CREATE TABLE IF NOT EXISTS extraction_item_links (
    run_id UUID NOT NULL REFERENCES extraction_runs(id) ON DELETE CASCADE,
    candidate_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    principal_id UUID NOT NULL REFERENCES principals(id),
    workspace_id UUID REFERENCES workspaces(id),
    memory_item_id UUID NOT NULL REFERENCES memory_items(id),
    ingest_id UUID NOT NULL REFERENCES candidate_ingests(id)
);

CREATE OR REPLACE FUNCTION extraction_link_integrity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM extraction_runs r
        JOIN memory_items m ON m.id = NEW.memory_item_id
        JOIN candidate_ingests i ON i.id = NEW.ingest_id
        WHERE r.id = NEW.run_id
          AND r.tenant_id = NEW.tenant_id AND m.tenant_id = NEW.tenant_id
          AND i.tenant_id = NEW.tenant_id
          AND r.principal_id = NEW.principal_id AND m.principal_id = NEW.principal_id
          AND i.principal_id = NEW.principal_id
          AND r.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND m.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND i.workspace_id IS NOT DISTINCT FROM NEW.workspace_id
          AND i.content_hash = m.content_hash
          AND EXISTS (SELECT 1 FROM jsonb_array_elements(r.receipt->'candidates') c
              WHERE c->>'candidate_id' = NEW.candidate_id::text
                AND c->>'memory_item_id' = NEW.memory_item_id::text
                AND c->>'ingest_id' = NEW.ingest_id::text
                AND c->>'content_hash' = m.content_hash)
    ) THEN
        RAISE EXCEPTION 'extraction linkage mismatch' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS extraction_link_integrity ON extraction_item_links;
CREATE TRIGGER extraction_link_integrity BEFORE INSERT ON extraction_item_links
FOR EACH ROW EXECUTE FUNCTION extraction_link_integrity();

CREATE OR REPLACE FUNCTION extraction_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'extraction evidence is immutable' USING ERRCODE = '23514';
END $$;
DROP TRIGGER IF EXISTS extraction_immutable ON extraction_runs;
CREATE TRIGGER extraction_immutable BEFORE UPDATE ON extraction_runs
FOR EACH ROW EXECUTE FUNCTION extraction_immutable();
DROP TRIGGER IF EXISTS extraction_immutable ON extraction_item_links;
CREATE TRIGGER extraction_immutable BEFORE UPDATE ON extraction_item_links
FOR EACH ROW EXECUTE FUNCTION extraction_immutable();

ALTER TABLE extraction_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE extraction_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE extraction_item_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE extraction_item_links FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS extraction_owner ON extraction_runs;
CREATE POLICY extraction_owner ON extraction_runs USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    AND principal_id = NULLIF(current_setting('app.principal_id', true), '')::uuid
    AND (workspace_id IS NULL
    OR current_setting('app.extraction_admin', true) = 'true'
    OR EXISTS (
        SELECT 1 FROM workspace_members wm WHERE wm.workspace_id = extraction_runs.workspace_id
        AND wm.principal_id = extraction_runs.principal_id
    ))
);
DROP POLICY IF EXISTS extraction_owner ON extraction_item_links;
CREATE POLICY extraction_owner ON extraction_item_links USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    AND principal_id = NULLIF(current_setting('app.principal_id', true), '')::uuid
    AND EXISTS (SELECT 1 FROM extraction_runs r WHERE r.id = extraction_item_links.run_id)
);
GRANT SELECT, INSERT ON extraction_runs, extraction_item_links TO engram_app;
-- Migration 003 grants full DML to future tables. Remove inherited mutation privileges.
REVOKE UPDATE, DELETE ON extraction_runs, extraction_item_links FROM engram_app;
