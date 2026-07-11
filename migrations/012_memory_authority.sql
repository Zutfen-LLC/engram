-- V2-BL-006: immutable ordinal memory authority, separate from source trust.

ALTER TABLE memory_items ADD COLUMN authority SMALLINT;

UPDATE memory_items AS mi
SET authority = CASE
    WHEN mi.source_type IN ('extraction', 'sync_turn', 'pre_compress', 'session_end') THEN 10
    WHEN mi.source_type = 'manual' AND p.type IN ('user', 'admin') THEN 50
    WHEN mi.source_type = 'manual' AND p.type IN ('agent', 'system') THEN 30
    WHEN mi.source_type IN ('import', 'migration') AND p.type = 'agent' THEN 20
    WHEN mi.source_type IN ('import', 'migration') AND p.type IN ('user', 'admin', 'system') THEN 40
    ELSE 10
END
FROM principals AS p
WHERE p.id = mi.principal_id;

-- Rows with malformed/missing historical principal provenance fail closed.
UPDATE memory_items SET authority = 10 WHERE authority IS NULL;

ALTER TABLE memory_items
    ALTER COLUMN authority SET DEFAULT 10,
    ALTER COLUMN authority SET NOT NULL,
    ADD CONSTRAINT chk_memory_authority CHECK (authority IN (10, 20, 30, 40, 50));

CREATE OR REPLACE VIEW cca_ledger WITH (security_invoker = true) AS
SELECT
    id, tenant_id, workspace_id, principal_id,
    content, kind, wing, room, visibility,
    review_status, memory_confidence, source_trust, human_verified,
    source_type, source_session, source_uri,
    valid_from, valid_to, superseded_by, created_at,
    authority
FROM memory_items
WHERE kind IN ('doctrine', 'decision', 'invariant', 'preference')
  AND valid_to IS NULL;
