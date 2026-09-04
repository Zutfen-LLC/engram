-- Engram — startup-promotion shadow parity state (ENG-PROMOTION-003B5 / #170).
--
-- This table is diagnostic-only.  It records a bounded shadow cursor and
-- aggregate, content-free outcomes; it is never read by promotion authority,
-- never represented in a job payload, and cannot authorize lifecycle writes.

CREATE TABLE IF NOT EXISTS promotion_startup_shadow_state (
    tenant_id                      UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    cursor_created_at              TIMESTAMPTZ,
    cursor_item_id                 UUID,
    rotation                       BIGINT NOT NULL DEFAULT 0,
    last_observed_at               TIMESTAMPTZ,
    last_window_size               INTEGER NOT NULL DEFAULT 0,
    last_wrapped                   BOOLEAN NOT NULL DEFAULT FALSE,
    parity_no_action               BIGINT NOT NULL DEFAULT 0,
    parity_already_committed       BIGINT NOT NULL DEFAULT 0,
    parity_durably_scheduled       BIGINT NOT NULL DEFAULT 0,
    mismatch_missing_obligation    BIGINT NOT NULL DEFAULT 0,
    mismatch_state                 BIGINT NOT NULL DEFAULT 0,
    unknown                        BIGINT NOT NULL DEFAULT 0,
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE promotion_startup_shadow_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE promotion_startup_shadow_state FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'promotion_startup_shadow_state'
          AND policyname = 'tenant_isolation_promotion_startup_shadow_state'
    ) THEN
        CREATE POLICY tenant_isolation_promotion_startup_shadow_state
            ON promotion_startup_shadow_state
            USING (tenant_id::text = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE ON promotion_startup_shadow_state TO engram_app;
REVOKE DELETE ON promotion_startup_shadow_state FROM engram_app;
