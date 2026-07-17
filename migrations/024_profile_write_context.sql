-- ENG-SCOPE-002C: immutable profile write-context provenance.
-- Additive and safe to re-apply. Omitted values remain truthfully legacy.

CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_tenant_identity
    ON api_keys (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_items_tenant_identity
    ON memory_items (tenant_id, id);

ALTER TABLE candidate_ingests
    ADD COLUMN IF NOT EXISTS api_key_id UUID,
    ADD COLUMN IF NOT EXISTS memory_profile_id UUID,
    ADD COLUMN IF NOT EXISTS memory_profile_revision_id UUID,
    ADD COLUMN IF NOT EXISTS memory_context_version TEXT;

UPDATE candidate_ingests
SET memory_context_version = 'legacy-unprofiled-v0'
WHERE memory_context_version IS NULL;

ALTER TABLE candidate_ingests
    ALTER COLUMN memory_context_version SET DEFAULT 'legacy-unprofiled-v0',
    ALTER COLUMN memory_context_version SET NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_candidate_ingests_memory_profile_pair'
          AND conrelid = 'candidate_ingests'::regclass
    ) THEN
        ALTER TABLE candidate_ingests
            ADD CONSTRAINT chk_candidate_ingests_memory_profile_pair CHECK (
                (memory_profile_id IS NULL) = (memory_profile_revision_id IS NULL)
            );
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_candidate_ingests_api_key'
          AND conrelid = 'candidate_ingests'::regclass
    ) THEN
        ALTER TABLE candidate_ingests
            ADD CONSTRAINT fk_candidate_ingests_api_key
            FOREIGN KEY (tenant_id, api_key_id)
            REFERENCES api_keys (tenant_id, id)
            ON DELETE SET NULL (api_key_id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_candidate_ingests_memory_profile_revision'
          AND conrelid = 'candidate_ingests'::regclass
    ) THEN
        ALTER TABLE candidate_ingests
            ADD CONSTRAINT fk_candidate_ingests_memory_profile_revision
            FOREIGN KEY (memory_profile_revision_id, memory_profile_id, tenant_id)
            REFERENCES memory_profile_revisions (id, profile_id, tenant_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_candidate_ingests_memory_context
    ON candidate_ingests (
        tenant_id, memory_profile_id, memory_profile_revision_id, created_at DESC
    ) WHERE memory_profile_id IS NOT NULL;

ALTER TABLE item_events
    ADD COLUMN IF NOT EXISTS tenant_id UUID,
    ADD COLUMN IF NOT EXISTS api_key_id UUID,
    ADD COLUMN IF NOT EXISTS memory_profile_id UUID,
    ADD COLUMN IF NOT EXISTS memory_profile_revision_id UUID,
    ADD COLUMN IF NOT EXISTS memory_context_version TEXT;

-- Compatibility for pre-002C writers and historical test/maintenance SQL:
-- derive the tenant from the canonical parent when it is omitted.  A supplied
-- tenant is never rewritten, and the composite FK below still rejects a lie.
CREATE OR REPLACE FUNCTION populate_item_event_tenant()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tenant_id IS NULL THEN
        SELECT item.tenant_id INTO NEW.tenant_id
        FROM memory_items AS item
        WHERE item.id = NEW.item_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_populate_item_event_tenant ON item_events;
CREATE TRIGGER trg_populate_item_event_tenant
BEFORE INSERT ON item_events
FOR EACH ROW EXECUTE FUNCTION populate_item_event_tenant();

UPDATE item_events AS event
SET tenant_id = item.tenant_id
FROM memory_items AS item
WHERE event.item_id = item.id
  AND event.tenant_id IS NULL;

UPDATE item_events
SET memory_context_version = 'legacy-unprofiled-v0'
WHERE memory_context_version IS NULL;

ALTER TABLE item_events
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN memory_context_version SET DEFAULT 'legacy-unprofiled-v0',
    ALTER COLUMN memory_context_version SET NOT NULL;

ALTER TABLE item_events DROP CONSTRAINT IF EXISTS item_events_item_id_fkey;
ALTER TABLE item_events DROP CONSTRAINT IF EXISTS item_events_actor_principal_id_fkey;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_item_events_tenant_item'
          AND conrelid = 'item_events'::regclass
    ) THEN
        ALTER TABLE item_events
            ADD CONSTRAINT fk_item_events_tenant_item
            FOREIGN KEY (item_id, tenant_id)
            REFERENCES memory_items (id, tenant_id) ON DELETE CASCADE;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_item_events_tenant_actor'
          AND conrelid = 'item_events'::regclass
    ) THEN
        ALTER TABLE item_events
            ADD CONSTRAINT fk_item_events_tenant_actor
            FOREIGN KEY (tenant_id, actor_principal_id)
            REFERENCES principals (tenant_id, id)
            ON DELETE SET NULL (actor_principal_id);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_item_events_memory_profile_pair'
          AND conrelid = 'item_events'::regclass
    ) THEN
        ALTER TABLE item_events
            ADD CONSTRAINT chk_item_events_memory_profile_pair CHECK (
                (memory_profile_id IS NULL) = (memory_profile_revision_id IS NULL)
            );
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_item_events_api_key'
          AND conrelid = 'item_events'::regclass
    ) THEN
        ALTER TABLE item_events
            ADD CONSTRAINT fk_item_events_api_key
            FOREIGN KEY (tenant_id, api_key_id)
            REFERENCES api_keys (tenant_id, id)
            ON DELETE SET NULL (api_key_id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_item_events_memory_profile_revision'
          AND conrelid = 'item_events'::regclass
    ) THEN
        ALTER TABLE item_events
            ADD CONSTRAINT fk_item_events_memory_profile_revision
            FOREIGN KEY (memory_profile_revision_id, memory_profile_id, tenant_id)
            REFERENCES memory_profile_revisions (id, profile_id, tenant_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_item_events_memory_context
    ON item_events (
        tenant_id, memory_profile_id, memory_profile_revision_id, created_at DESC
    ) WHERE memory_profile_id IS NOT NULL;

ALTER TABLE candidate_ingests ENABLE ROW LEVEL SECURITY;
ALTER TABLE candidate_ingests FORCE ROW LEVEL SECURITY;
ALTER TABLE item_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE item_events FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON candidate_ingests TO engram_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON item_events TO engram_app;
REVOKE UPDATE, DELETE ON candidate_ingests FROM engram_app;
