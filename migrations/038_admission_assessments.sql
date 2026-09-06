-- Durable admission decisions (issue #159, ENG-PROMOTION-003D).
--
-- An admission assessment records what current Path A promotion policy
-- decided, from which exact inputs, under which policy identity, what blocked
-- or authorized the result, and what must happen next. It is a decision
-- artifact, not a new promotion policy and not an evidence assessment: the
-- #157 memory_assessments referenced here stay diagnostic in v1.
--
-- admission_assessments is append-only immutable history. Current state is a
-- separate mutable one-row projection (admission_assessment_current); no
-- pointer column is added to memory_items.

CREATE TABLE IF NOT EXISTS admission_assessments (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    memory_item_id UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL
        CHECK (schema_version = 'engram.admission-assessment.v1'),
    mode TEXT NOT NULL CHECK (mode IN ('authoritative','shadow','legacy_import')),
    evaluation_id UUID,
    job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    trigger_type TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    invocation_source TEXT NOT NULL,
    actor_principal_id UUID,
    evaluated_at TIMESTAMPTZ NOT NULL,
    item_content_hash TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    policy_profile_key TEXT NOT NULL,
    policy_contract_version TEXT NOT NULL,
    policy_config_digest TEXT NOT NULL,
    selected_basis TEXT CHECK (selected_basis IN ('legacy_confidence','retention_evidence')),
    outcome TEXT NOT NULL CHECK (outcome IN ('admitted','would_admit','cooling',
        'review_required','blocked','insufficient_evidence','unknown','stale','not_applicable')),
    blocker_codes JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(blocker_codes) = 'array'
            AND jsonb_array_length(blocker_codes) <= 32),
    reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(reason_codes) = 'array'
            AND jsonb_array_length(reason_codes) <= 32),
    decision_inputs JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(decision_inputs) = 'object'
            AND octet_length(decision_inputs::text) <= 8192),
    classification_run_id UUID REFERENCES classification_runs(id) ON DELETE SET NULL,
    available_memory_assessment_refs JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(available_memory_assessment_refs) = 'array'
            AND jsonb_array_length(available_memory_assessment_refs) <= 16
            AND octet_length(available_memory_assessment_refs::text) <= 8192),
    conflict_recheck_status TEXT NOT NULL CHECK (conflict_recheck_status IN
        ('clear','blocked','not_run','not_run_preview','unavailable_legacy')),
    cooling_period_start TIMESTAMPTZ,
    eligible_at TIMESTAMPTZ,
    next_evaluation_at TIMESTAMPTZ,
    next_actions JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(next_actions) = 'array'
            AND jsonb_array_length(next_actions) <= 8),
    decision_hash TEXT NOT NULL,
    prior_assessment_id UUID REFERENCES admission_assessments(id) ON DELETE SET NULL,
    linked_item_event_id UUID REFERENCES item_events(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One canonical evaluation execution yields at most one assessment per
    -- tenant, so a promotion.evaluate retry reuses the bound row instead of
    -- appending a second mutation decision.
    UNIQUE (tenant_id, evaluation_id),
    UNIQUE (tenant_id, memory_item_id, id)
);

-- Bounded history reads: newest-first per (item, profile) without a scan.
CREATE INDEX IF NOT EXISTS idx_admission_assessment_history
    ON admission_assessments(tenant_id, memory_item_id, policy_profile_key,
        evaluated_at DESC, id DESC);
-- Operator queries by outcome and due time stay bounded per tenant.
CREATE INDEX IF NOT EXISTS idx_admission_assessment_due
    ON admission_assessments(tenant_id, policy_profile_key, next_evaluation_at)
    WHERE next_evaluation_at IS NOT NULL;

-- The one-row current projection. Only authoritative and legacy_import rows
-- may be projected; the pointed assessment remains the source of truth, so
-- this table carries identity plus the operational metadata needed to resolve
-- precedence, never a copy of the decision.
CREATE TABLE IF NOT EXISTS admission_assessment_current (
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    memory_item_id UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    policy_profile_key TEXT NOT NULL,
    assessment_id UUID NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('authoritative','legacy_import')),
    -- Precedence components. authoritative (1) always supersedes
    -- legacy_import (0); within a mode the later evaluation wins; a tie is
    -- broken toward the row that actually mutated item state, then by id, so
    -- a lost promotion race still resolves deterministically to the winner.
    mode_rank SMALLINT NOT NULL CHECK (mode_rank IN (0, 1)),
    mutation_rank SMALLINT NOT NULL CHECK (mutation_rank IN (0, 1)),
    evaluated_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, memory_item_id, policy_profile_key),
    FOREIGN KEY (tenant_id, memory_item_id, assessment_id)
        REFERENCES admission_assessments(tenant_id, memory_item_id, id) ON DELETE CASCADE
);

-- item_events audit rows may name the assessment that authorized them.
-- Nullable: every historical event predates this migration and stays unlinked.
ALTER TABLE item_events
    ADD COLUMN IF NOT EXISTS admission_assessment_id UUID
    REFERENCES admission_assessments(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_item_events_admission_assessment
    ON item_events(admission_assessment_id)
    WHERE admission_assessment_id IS NOT NULL;

-- Integrity validation only: it reads internal rows to prove referential and
-- vocabulary consistency and either raises or returns NEW. It returns no data
-- to the caller, so a definer context with a fixed search_path is safe here
-- and is required: the capturing worker holds the tenant's admin principal,
-- which is not the author of every private item it must capture a decision
-- for, so an invoker-context check would fail on rows the writer is
-- nonetheless authorized (and obliged) to record.
CREATE OR REPLACE FUNCTION admission_assessment_integrity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM memory_items WHERE id = NEW.memory_item_id
        AND tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'admission assessment item tenant mismatch' USING ERRCODE = '23514';
    END IF;
    IF TG_TABLE_NAME = 'admission_assessments' THEN
        IF NEW.mode = 'shadow' AND NEW.outcome = 'admitted' THEN
            RAISE EXCEPTION 'shadow assessment cannot claim admission'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.mode = 'shadow' AND NEW.linked_item_event_id IS NOT NULL THEN
            RAISE EXCEPTION 'shadow assessment cannot link a mutation event'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.outcome <> 'admitted' AND NEW.linked_item_event_id IS NOT NULL THEN
            RAISE EXCEPTION 'only an admitted assessment links a mutation event'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.prior_assessment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM admission_assessments a WHERE a.id = NEW.prior_assessment_id
            AND a.tenant_id = NEW.tenant_id AND a.memory_item_id = NEW.memory_item_id
            AND a.policy_profile_key = NEW.policy_profile_key) THEN
            RAISE EXCEPTION 'admission assessment prior mismatch' USING ERRCODE = '23514';
        END IF;
        IF NEW.job_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM jobs j WHERE j.id = NEW.job_id AND j.tenant_id = NEW.tenant_id) THEN
            RAISE EXCEPTION 'admission assessment job mismatch' USING ERRCODE = '23514';
        END IF;
    ELSE
        IF NOT EXISTS (SELECT 1 FROM admission_assessments a WHERE a.id = NEW.assessment_id
            AND a.tenant_id = NEW.tenant_id AND a.memory_item_id = NEW.memory_item_id
            AND a.policy_profile_key = NEW.policy_profile_key
            AND a.mode = NEW.mode AND a.evaluated_at = NEW.evaluated_at) THEN
            RAISE EXCEPTION 'admission projection assessment mismatch' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION admission_assessment_integrity() FROM PUBLIC;

DROP TRIGGER IF EXISTS admission_assessment_integrity ON admission_assessments;
CREATE TRIGGER admission_assessment_integrity BEFORE INSERT ON admission_assessments
FOR EACH ROW EXECUTE FUNCTION admission_assessment_integrity();
DROP TRIGGER IF EXISTS admission_assessment_integrity ON admission_assessment_current;
CREATE TRIGGER admission_assessment_integrity
BEFORE INSERT OR UPDATE ON admission_assessment_current
FOR EACH ROW EXECUTE FUNCTION admission_assessment_integrity();

-- No-rewrite is enforced by a trigger in addition to the revoked UPDATE grant,
-- so a future privilege drift cannot silently make a recorded decision
-- rewritable — a rewritten decision is worse than no decision at all.
--
-- The trigger covers UPDATE only. Removal is governed by the revoked DELETE
-- grant instead, deliberately: memory_item_id is ON DELETE CASCADE, so
-- deleting an item must still be able to take its decisions with it. A
-- decision about an item that no longer exists binds to nothing, and blocking
-- that cascade would make item deletion fail outright.
CREATE OR REPLACE FUNCTION admission_assessment_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'admission assessment history is immutable' USING ERRCODE = '23514';
END $$;
DROP TRIGGER IF EXISTS admission_assessment_immutable ON admission_assessments;
CREATE TRIGGER admission_assessment_immutable
BEFORE UPDATE ON admission_assessments
FOR EACH ROW EXECUTE FUNCTION admission_assessment_immutable();

ALTER TABLE admission_assessments ENABLE ROW LEVEL SECURITY;
ALTER TABLE admission_assessments FORCE ROW LEVEL SECURITY;
ALTER TABLE admission_assessment_current ENABLE ROW LEVEL SECURITY;
ALTER TABLE admission_assessment_current FORCE ROW LEVEL SECURITY;
-- Reads follow item read eligibility (the #157 predicate from migration 037),
-- so an assessment is visible exactly when its item is. Writes are checked
-- against tenant scope alone: capture is performed by the worker under the
-- tenant's admin principal, which is not the author of every private item it
-- must record a decision for. Cross-tenant reads and writes fail either way.
DROP POLICY IF EXISTS admission_assessment_eligibility ON admission_assessments;
CREATE POLICY admission_assessment_eligibility ON admission_assessments
USING (assessment_item_eligible(memory_item_id, tenant_id))
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
DROP POLICY IF EXISTS admission_assessment_eligibility ON admission_assessment_current;
CREATE POLICY admission_assessment_eligibility ON admission_assessment_current
USING (assessment_item_eligible(memory_item_id, tenant_id))
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT ON admission_assessments TO engram_app;
REVOKE UPDATE, DELETE ON admission_assessments FROM engram_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON admission_assessment_current TO engram_app;
