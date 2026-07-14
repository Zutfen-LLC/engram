-- Promotion Path A v2: evidence lane rollout and governed automatic-kind policy.
-- Existing tenants deliberately remain opted out of the new evidence lane.

ALTER TABLE tenant_config
    ADD COLUMN IF NOT EXISTS auto_promote_evidence_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS auto_promote_evidence_threshold REAL NOT NULL DEFAULT 0.70;

-- A tenant lacking an active config must fail closed for the new transition.
INSERT INTO tenant_config (
    tenant_id, config_version, active, auto_promote_evidence_enabled,
    auto_promote_evidence_threshold
)
SELECT t.id, 'v1', TRUE, FALSE, 0.70
FROM tenants t
WHERE NOT EXISTS (
    SELECT 1 FROM tenant_config tc WHERE tc.tenant_id = t.id AND tc.active = TRUE
);

ALTER TABLE tenant_config
    DROP CONSTRAINT IF EXISTS chk_auto_promote_evidence_threshold;
ALTER TABLE tenant_config
    ADD CONSTRAINT chk_auto_promote_evidence_threshold
    CHECK (auto_promote_evidence_threshold >= 0.0 AND auto_promote_evidence_threshold <= 1.0);

-- Only rows created after this migration inherit enabled=true.
ALTER TABLE tenant_config
    ALTER COLUMN auto_promote_evidence_enabled SET DEFAULT TRUE;

-- New tenants receive an explicit enabled config from the application tenant
-- creation path. A database trigger would race with existing import and test
-- helpers that already create their own active configuration.

ALTER TABLE memory_kinds
    ADD COLUMN IF NOT EXISTS auto_promote_from_inferred BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE memory_kinds
SET auto_promote_from_inferred = TRUE
WHERE is_builtin = TRUE
  AND name IN ('fact', 'decision', 'procedure', 'summary', 'observation');

UPDATE memory_kinds
SET auto_promote_from_inferred = FALSE
WHERE NOT (is_builtin = TRUE AND name IN ('fact', 'decision', 'procedure', 'summary', 'observation'));

-- Installations upgrading from migration 015 still have migration 007's old
-- tenant trigger function. Replace it so future tenants receive the governed
-- builtin defaults instead of the column default (FALSE) for every kind.
CREATE OR REPLACE FUNCTION seed_builtin_memory_kinds() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO memory_kinds (
        tenant_id, name, display_name, description, is_builtin, enabled,
        singleton, stays_in_recall_when_disputed, requires_review,
        auto_promote_from_inferred, default_importance, sort_order
    )
    SELECT
        NEW.id, k.name, k.display_name, k.description, TRUE, TRUE,
        k.singleton, k.stays, k.requires_review, k.auto_promote,
        k.default_importance, k.sort_order
    FROM (VALUES
        ('fact', 'Fact', 'An observed or stated fact.',
            FALSE, FALSE, FALSE, TRUE, 0.5::double precision, 10),
        ('preference', 'Preference', 'A stated preference or convention.',
            TRUE, FALSE, FALSE, FALSE, 0.5, 20),
        ('doctrine', 'Doctrine', 'A standing policy or rule that governs behavior.',
            FALSE, TRUE, TRUE, FALSE, 0.7, 30),
        ('decision', 'Decision', 'A decision that was made and should be remembered.',
            FALSE, FALSE, TRUE, TRUE, 0.6, 40),
        ('invariant', 'Invariant',
            'A rule that must always hold; violations are high-stakes.',
            TRUE, TRUE, TRUE, FALSE, 0.8, 50),
        ('observation', 'Observation',
            'Something noticed but not yet trusted or reviewed.',
            FALSE, FALSE, FALSE, TRUE, 0.4, 60),
        ('diary_entry', 'Diary Entry', 'A private agent diary entry.',
            FALSE, FALSE, FALSE, FALSE, 0.4, 70),
        ('procedure', 'Procedure', 'A how-to, runbook, or operational procedure.',
            FALSE, FALSE, FALSE, TRUE, 0.5, 80),
        ('summary', 'Summary', 'A condensed summary derived from other memories.',
            FALSE, FALSE, FALSE, TRUE, 0.4, 90)
    ) AS k(name, display_name, description, singleton, stays, requires_review,
           auto_promote, default_importance, sort_order)
    ON CONFLICT (tenant_id, name) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
