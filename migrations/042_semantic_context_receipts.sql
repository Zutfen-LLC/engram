-- 042_semantic_context_receipts.sql
-- Additive support for semantic-context-manifest-v1.  Startup rows and their
-- frozen context-manifest-v1 contract remain unchanged.

ALTER TABLE recall_logs
    ADD COLUMN IF NOT EXISTS item_budget INTEGER NULL;

ALTER TABLE context_receipts
    DROP CONSTRAINT IF EXISTS chk_context_receipts_schema;
ALTER TABLE context_receipts
    ADD CONSTRAINT chk_context_receipts_schema CHECK (
        manifest_schema IN ('engram.context-manifest', 'engram.semantic-context-manifest')
    );

ALTER TABLE context_receipts
    DROP CONSTRAINT IF EXISTS chk_context_receipts_mode;
ALTER TABLE context_receipts
    ADD CONSTRAINT chk_context_receipts_mode CHECK (mode IN ('startup', 'semantic'));
