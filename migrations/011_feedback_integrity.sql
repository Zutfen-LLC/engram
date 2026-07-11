-- V2-BL-005: canonical, append-preserved feedback and tenant rate bounds.

ALTER TABLE feedback_events
    ADD COLUMN superseded_at TIMESTAMPTZ NULL,
    ADD COLUMN replaces_feedback_event_id UUID NULL
        REFERENCES feedback_events(id) ON DELETE SET NULL;

WITH ordered AS (
    SELECT id,
           LAG(id) OVER (
               PARTITION BY tenant_id, item_id, principal_id
               ORDER BY created_at ASC, id ASC
           ) AS prior_id,
           LEAD(created_at) OVER (
               PARTITION BY tenant_id, item_id, principal_id
               ORDER BY created_at ASC, id ASC
           ) AS replacement_at
    FROM feedback_events
)
UPDATE feedback_events AS feedback
SET replaces_feedback_event_id = ordered.prior_id,
    superseded_at = ordered.replacement_at
FROM ordered
WHERE feedback.id = ordered.id;

CREATE UNIQUE INDEX idx_feedback_current_principal_item
    ON feedback_events (tenant_id, item_id, principal_id)
    WHERE superseded_at IS NULL;

CREATE INDEX idx_feedback_principal_created
    ON feedback_events (tenant_id, principal_id, created_at);

ALTER TABLE tenant_config
    ADD COLUMN feedback_daily_limit INTEGER NOT NULL DEFAULT 500,
    ADD CONSTRAINT tenant_config_feedback_daily_limit_check
        CHECK (feedback_daily_limit >= 1 AND feedback_daily_limit <= 100000);
