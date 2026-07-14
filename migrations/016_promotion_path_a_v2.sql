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
