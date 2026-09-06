-- Risk-aware, multi-surface shadow decisions (issue #158).
--
-- This migration is additive.  It preserves every v1 Path A decision and
-- adds bounded fields for v2 shadow evidence.  V2 rows are immutable history
-- only.  They cannot enter admission_assessment_current or claim a lifecycle
-- mutation.

ALTER TABLE admission_assessments
    DROP CONSTRAINT IF EXISTS admission_assessments_schema_version_check;
ALTER TABLE admission_assessments
    ADD CONSTRAINT admission_assessments_schema_version_check
    CHECK (schema_version IN (
        'engram.admission-assessment.v1',
        'engram.admission-assessment.v2'
    ));

ALTER TABLE admission_assessments
    ADD COLUMN IF NOT EXISTS risk_state TEXT,
    ADD COLUMN IF NOT EXISTS epistemic_state TEXT,
    ADD COLUMN IF NOT EXISTS retention_state TEXT,
    ADD COLUMN IF NOT EXISTS effective_memory_assessment_refs JSONB,
    ADD COLUMN IF NOT EXISTS highest_admission_tier TEXT,
    ADD COLUMN IF NOT EXISTS surface_decisions JSONB,
    ADD COLUMN IF NOT EXISTS observation_window_hours INTEGER;

ALTER TABLE admission_assessments
    DROP CONSTRAINT IF EXISTS admission_assessment_v2_shadow_contract;
ALTER TABLE admission_assessments
    ADD CONSTRAINT admission_assessment_v2_shadow_contract CHECK (
        schema_version <> 'engram.admission-assessment.v2' OR (
            mode = 'shadow'
            AND policy_profile_key = 'risk_aware_shadow_v1'
            AND resulting_state_digest IS NULL
            AND linked_item_event_id IS NULL
            AND risk_state IN ('low', 'medium', 'high', 'unknown', 'not_applicable')
            AND epistemic_state IN (
                'supported', 'contested', 'insufficient_evidence', 'unknown', 'not_applicable'
            )
            AND retention_state IN ('retain', 'transient', 'noise', 'uncertain', 'unknown')
            AND effective_memory_assessment_refs IS NOT NULL
            AND jsonb_typeof(effective_memory_assessment_refs) = 'array'
            AND jsonb_array_length(effective_memory_assessment_refs) <= 16
            AND octet_length(effective_memory_assessment_refs::text) <= 8192
            AND highest_admission_tier IN (
                'none', 'semantic_exploratory', 'semantic_governed', 'startup'
            )
            AND surface_decisions IS NOT NULL
            AND jsonb_typeof(surface_decisions) = 'object'
            AND surface_decisions ?& ARRAY[
                'semantic_exploratory', 'semantic_governed', 'startup'
            ]
            AND surface_decisions - ARRAY[
                'semantic_exploratory', 'semantic_governed', 'startup'
            ] = '{}'::jsonb
            AND surface_decisions->>'semantic_exploratory' IN (
                'allow', 'withhold', 'review_required', 'blocked', 'unknown'
            )
            AND surface_decisions->>'semantic_governed' IN (
                'allow', 'withhold', 'review_required', 'blocked', 'unknown'
            )
            AND surface_decisions->>'startup' IN (
                'allow', 'withhold', 'review_required', 'blocked', 'unknown'
            )
            AND (observation_window_hours IS NULL
                OR observation_window_hours BETWEEN 0 AND 8760)
        )
    );

-- A shadow candidate is never a current admission authority.  This duplicate
-- check makes a direct SQL projection attempt fail even if application code
-- regresses before the existing trigger inspects the referenced row.
ALTER TABLE admission_assessment_current
    DROP CONSTRAINT IF EXISTS admission_assessment_current_no_shadow_v2;
ALTER TABLE admission_assessment_current
    ADD CONSTRAINT admission_assessment_current_no_shadow_v2
    CHECK (mode IN ('authoritative', 'legacy_import'));

CREATE INDEX IF NOT EXISTS idx_admission_assessment_shadow_v2_history
    ON admission_assessments(tenant_id, policy_profile_key, memory_item_id,
        evaluated_at DESC, id DESC)
    WHERE schema_version = 'engram.admission-assessment.v2' AND mode = 'shadow';
