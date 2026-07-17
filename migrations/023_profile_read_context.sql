-- ENG-SCOPE-002B: additive recall-audit provenance for enforced memory reads.

ALTER TABLE recall_logs
    ADD COLUMN IF NOT EXISTS memory_profile_id UUID,
    ADD COLUMN IF NOT EXISTS memory_profile_revision_id UUID,
    ADD COLUMN IF NOT EXISTS memory_context_version TEXT;

-- Rows written before 002B did not enforce a resolved context.
UPDATE recall_logs
SET memory_context_version = 'legacy-unprofiled-v0'
WHERE memory_context_version IS NULL;

ALTER TABLE recall_logs
    -- An omitted value means the writer did not attest that it enforced 002B.
    -- Execute this unconditionally so reapplication repairs unsafe development
    -- copies that installed the earlier memory-context-v1 default.
    ALTER COLUMN memory_context_version SET DEFAULT 'legacy-unprofiled-v0',
    ALTER COLUMN memory_context_version SET NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_recall_logs_memory_profile_pair'
          AND conrelid = 'recall_logs'::regclass
    ) THEN
        ALTER TABLE recall_logs
            ADD CONSTRAINT chk_recall_logs_memory_profile_pair CHECK (
                (memory_profile_id IS NULL) = (memory_profile_revision_id IS NULL)
            );
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_recall_logs_memory_profile_revision'
          AND conrelid = 'recall_logs'::regclass
    ) THEN
        ALTER TABLE recall_logs
            ADD CONSTRAINT fk_recall_logs_memory_profile_revision
            FOREIGN KEY (memory_profile_revision_id, memory_profile_id, tenant_id)
            REFERENCES memory_profile_revisions(id, profile_id, tenant_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_recall_logs_memory_profile
    ON recall_logs (tenant_id, memory_profile_id, memory_profile_revision_id, created_at DESC)
    WHERE memory_profile_id IS NOT NULL;

-- Recall-log RLS, FORCE RLS, ownership, and application-role grants already
-- cover additive columns. Reassert them idempotently for independently
-- operated installations.
ALTER TABLE recall_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE recall_logs FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON recall_logs TO engram_app;
