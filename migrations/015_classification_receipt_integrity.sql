-- Make receipt consumption immutable even when the bound memory is later deleted.
ALTER TABLE classification_runs
    ADD COLUMN bound_at TIMESTAMPTZ;

-- PR #87 created and bound these rows in one workflow. created_at is a
-- conservative historical backfill marker, not a reconstruction of exact bind time.
UPDATE classification_runs
SET bound_at = created_at
WHERE memory_item_id IS NOT NULL
  AND bound_at IS NULL;

ALTER TABLE classification_runs
    ADD CONSTRAINT chk_classification_run_bound_state
    CHECK (memory_item_id IS NULL OR bound_at IS NOT NULL),
    ADD CONSTRAINT chk_classification_run_bound_time
    CHECK (bound_at IS NULL OR bound_at >= created_at);

DROP INDEX idx_classification_runs_expired_unbound;
CREATE INDEX idx_classification_runs_expired_unbound
    ON classification_runs (tenant_id, expires_at) WHERE bound_at IS NULL;
