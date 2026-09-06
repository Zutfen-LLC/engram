-- Append-only assessments do not replace classification or promotion authority.
CREATE TABLE IF NOT EXISTS assessment_requests (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    memory_item_id UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    principal_id UUID NOT NULL REFERENCES principals(id),
    execution_context_id UUID REFERENCES job_execution_contexts(id),
    purpose TEXT NOT NULL CHECK (purpose IN ('taxonomy','retention','epistemic','risk','combined')),
    reason TEXT NOT NULL CHECK (reason IN ('provider_recovery','model_upgrade','provenance_added',
        'human_correction','policy_rollout','manual')),
    target JSONB NOT NULL,
    contract_hash TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    evidence JSONB NOT NULL,
    job_id UUID NOT NULL REFERENCES jobs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES principals(tenant_id, id),
    UNIQUE (tenant_id, memory_item_id, purpose, contract_hash, input_digest),
    UNIQUE (tenant_id, memory_item_id, id)
);
CREATE TABLE IF NOT EXISTS memory_assessments (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    memory_item_id UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    request_id UUID,
    legacy_run_id UUID UNIQUE REFERENCES classification_runs(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    purpose TEXT NOT NULL CHECK (purpose IN ('taxonomy','retention','epistemic','risk','combined')),
    contract_hash TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('completed','failed','disabled','stale','legacy')),
    prior_assessment_id UUID REFERENCES memory_assessments(id) ON DELETE CASCADE,
    receipt JSONB NOT NULL CHECK (octet_length(receipt::text) <= 65536),
    canonical_hash TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, memory_item_id, request_id)
        REFERENCES assessment_requests(tenant_id, memory_item_id, id) ON DELETE CASCADE,
    UNIQUE (request_id, attempt),
    CHECK ((request_id IS NULL) <> (legacy_run_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_assessments_item
    ON memory_assessments(tenant_id, memory_item_id, purpose, created_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_completed
    ON memory_assessments(request_id) WHERE state = 'completed';
CREATE INDEX IF NOT EXISTS idx_assessment_queue_fairness ON jobs(tenant_id, updated_at)
    WHERE job_type = 'assessment.reassess' AND attempts > 0;

CREATE OR REPLACE FUNCTION assessment_item_eligible(item UUID, tenant UUID)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT tenant = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    AND EXISTS (SELECT 1 FROM memory_items m WHERE m.id = item AND m.tenant_id = tenant
        AND (m.visibility IN ('tenant','public')
        OR (m.visibility = 'private' AND m.principal_id =
            NULLIF(current_setting('app.principal_id', true), '')::uuid)
        OR (m.visibility = 'workspace' AND EXISTS (
            SELECT 1 FROM workspace_members w WHERE w.workspace_id = m.workspace_id
            AND w.principal_id = NULLIF(current_setting('app.principal_id', true), '')::uuid))));
$$;

-- Return linked evidence metadata under item eligibility. Never return transcript text.
CREATE OR REPLACE FUNCTION assessment_evidence_manifest(item UUID, tenant UUID)
RETURNS TABLE(candidate_id UUID, run_id UUID, receipt_hash TEXT, evidence_root TEXT,
    assertion_mode TEXT, origin TEXT, asserting_principal_id TEXT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT l.candidate_id, r.id, r.receipt_hash, c->>'evidence_root',
        c->>'assertion_mode', c->>'asserting_role', c->>'asserting_principal_id'
    FROM public.extraction_item_links l JOIN public.extraction_runs r ON r.id = l.run_id
    CROSS JOIN LATERAL jsonb_array_elements(r.receipt->'candidates') c
    WHERE l.memory_item_id = item AND l.tenant_id = tenant AND r.tenant_id = tenant
        AND c->>'candidate_id' = l.candidate_id::text
        AND public.assessment_item_eligible(item, tenant)
    ORDER BY l.candidate_id LIMIT 65;
$$;
REVOKE ALL ON FUNCTION assessment_evidence_manifest(UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION assessment_evidence_manifest(UUID, UUID) TO engram_app;

CREATE OR REPLACE FUNCTION assessment_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM memory_items WHERE id = NEW.memory_item_id
        AND tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'assessment item tenant mismatch' USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME = 'assessment_requests' THEN
        IF NOT EXISTS (SELECT 1 FROM jobs WHERE id = NEW.job_id AND tenant_id = NEW.tenant_id
            AND job_type = 'assessment.reassess'
            AND payload->>'request_id' = NEW.id::text
            AND payload->>'principal_id' = NEW.principal_id::text) THEN
            RAISE EXCEPTION 'assessment job mismatch' USING ERRCODE = '23514';
        END IF;
        IF NEW.execution_context_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM job_execution_contexts c WHERE c.id = NEW.execution_context_id
            AND c.tenant_id = NEW.tenant_id AND c.principal_id = NEW.principal_id) THEN
            RAISE EXCEPTION 'assessment authority mismatch' USING ERRCODE = '23514';
        END IF;
    ELSE
        IF NEW.request_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM assessment_requests r WHERE r.id = NEW.request_id
            AND r.tenant_id = NEW.tenant_id AND r.memory_item_id = NEW.memory_item_id
            AND r.purpose = NEW.purpose AND r.contract_hash = NEW.contract_hash
            AND r.input_digest = NEW.input_digest) THEN
            RAISE EXCEPTION 'assessment request mismatch' USING ERRCODE = '23514';
        END IF;
        IF NEW.legacy_run_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM classification_runs r WHERE r.id = NEW.legacy_run_id
            AND r.tenant_id = NEW.tenant_id AND r.memory_item_id = NEW.memory_item_id
            AND r.bound_at IS NOT NULL) THEN
            RAISE EXCEPTION 'assessment legacy receipt mismatch' USING ERRCODE = '23514';
        END IF;
        IF NEW.prior_assessment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM memory_assessments a WHERE a.id = NEW.prior_assessment_id
            AND a.tenant_id = NEW.tenant_id AND a.memory_item_id = NEW.memory_item_id
            AND a.purpose = NEW.purpose) THEN
            RAISE EXCEPTION 'assessment prior mismatch' USING ERRCODE = '23514';
        END IF;
        -- PostgreSQL 16 JSONB serialization is the versioned canonical encoding.
        NEW.canonical_hash := 'sha256:' || encode(sha256(convert_to(jsonb_build_object(
            'id', NEW.id, 'tenant_id', NEW.tenant_id, 'memory_item_id', NEW.memory_item_id,
            'request_id', NEW.request_id, 'legacy_run_id', NEW.legacy_run_id,
            'attempt', NEW.attempt, 'purpose', NEW.purpose, 'contract_hash', NEW.contract_hash,
            'input_digest', NEW.input_digest, 'state', NEW.state,
            'prior_assessment_id', NEW.prior_assessment_id, 'receipt', NEW.receipt,
            'created_at', to_char(NEW.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')
        )::text, 'UTF8')), 'hex');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS assessment_integrity ON assessment_requests;
CREATE TRIGGER assessment_integrity BEFORE INSERT ON assessment_requests
FOR EACH ROW EXECUTE FUNCTION assessment_integrity();
DROP TRIGGER IF EXISTS assessment_integrity ON memory_assessments;
CREATE TRIGGER assessment_integrity BEFORE INSERT ON memory_assessments
FOR EACH ROW EXECUTE FUNCTION assessment_integrity();

CREATE OR REPLACE FUNCTION assessment_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'assessment history is immutable' USING ERRCODE = '23514';
END $$;
DROP TRIGGER IF EXISTS assessment_immutable ON assessment_requests;
CREATE TRIGGER assessment_immutable BEFORE UPDATE ON assessment_requests
FOR EACH ROW EXECUTE FUNCTION assessment_immutable();
DROP TRIGGER IF EXISTS assessment_immutable ON memory_assessments;
CREATE TRIGGER assessment_immutable BEFORE UPDATE ON memory_assessments
FOR EACH ROW EXECUTE FUNCTION assessment_immutable();

ALTER TABLE assessment_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_assessments ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_assessments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS assessment_eligibility ON assessment_requests;
CREATE POLICY assessment_eligibility ON assessment_requests
USING (assessment_item_eligible(memory_item_id, tenant_id));
DROP POLICY IF EXISTS assessment_eligibility ON memory_assessments;
CREATE POLICY assessment_eligibility ON memory_assessments
USING (assessment_item_eligible(memory_item_id, tenant_id));
GRANT SELECT, INSERT ON assessment_requests, memory_assessments TO engram_app;
REVOKE UPDATE, DELETE ON assessment_requests, memory_assessments FROM engram_app;

ALTER TABLE memory_assessments DROP CONSTRAINT IF EXISTS assessment_dimensions_valid;
ALTER TABLE memory_assessments ADD CONSTRAINT assessment_dimensions_valid CHECK (
    (receipt->>'schema_version' = 'engram.assessment.v1') IS TRUE
    AND (receipt->'dimensions'->>'epistemic_state' IN
        ('supported','contested','insufficient_evidence','unknown','not_applicable')) IS TRUE
    AND (receipt->'dimensions'->>'risk' IN ('low','moderate','high','unknown')) IS TRUE
    AND COALESCE(receipt->'dimensions'->>'assertion_mode', 'unknown') IN
        ('direct_statement','tool_observation','quoted_source','derived_summary','inference','unknown')
    AND COALESCE(receipt->'dimensions'->>'origin', 'unknown') IN
        ('user','assistant','system','tool','unknown')
);

-- Backfill only bound receipts. Unknown versions and attribution stay unknown.
INSERT INTO memory_assessments(id, tenant_id, memory_item_id, legacy_run_id, purpose,
    contract_hash, input_digest, state, receipt, created_at)
SELECT r.id, r.tenant_id, r.memory_item_id, r.id, 'combined',
    'legacy:' || r.classification_version, r.content_hash, 'legacy',
    jsonb_build_object('schema_version', 'engram.assessment.v1',
        'input_content_hash', r.content_hash, 'context_hash', r.context_hash,
        'legacy_classification_version', r.classification_version,
        'legacy_retention_policy_version', r.retention_policy_version,
        'dimensions', jsonb_build_object(
            'taxonomy', jsonb_build_object('raw_value', r.taxonomy_confidence),
            'suggested_kind', r.suggested_kind,
            'retention', jsonb_build_object('raw_value', r.retention_confidence),
            'retention_disposition', r.retention_disposition,
            'epistemic_state', 'unknown', 'risk', 'unknown',
            'reason_codes', jsonb_build_array('legacy_receipt','uncalibrated')),
        'provider_details', jsonb_build_object('provider', r.provenance->>'provider',
            'model', r.provenance->>'model', 'prompt_version', NULL,
            'config_version', NULL, 'code_version', r.classification_version)),
    r.created_at
FROM classification_runs r WHERE r.memory_item_id IS NOT NULL AND r.bound_at IS NOT NULL
ON CONFLICT DO NOTHING;
