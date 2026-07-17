-- ENG-SCOPE-002A: revisioned memory-profile registry and immutable key binding.
-- This migration is deliberately additive. Existing API keys remain unbound.

-- Composite tenant-safe references reuse the candidate keys introduced by
-- migration 020.  Keep these guards here as well so a pre-022 database that
-- was provisioned from an older/custom baseline still has the exact keys the
-- foreign keys below require.
CREATE UNIQUE INDEX IF NOT EXISTS idx_principals_tenant_identity
    ON principals (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_tenant_identity
    ON workspaces (tenant_id, id);

CREATE TABLE IF NOT EXISTS memory_profiles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    description TEXT,
    active_revision_id UUID,
    disabled_at TIMESTAMPTZ,
    created_by_principal_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_memory_profiles_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_memory_profiles_tenant_slug UNIQUE (tenant_id, slug),
    CONSTRAINT fk_memory_profiles_tenant_creator
        FOREIGN KEY (tenant_id, created_by_principal_id)
        REFERENCES principals(tenant_id, id)
        ON DELETE SET NULL (created_by_principal_id),
    CONSTRAINT chk_memory_profiles_slug
        CHECK (slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$')
);

CREATE TABLE IF NOT EXISTS memory_profile_revisions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL,
    profile_id UUID NOT NULL,
    version INTEGER NOT NULL,
    include_private BOOLEAN NOT NULL DEFAULT true,
    include_tenant BOOLEAN NOT NULL DEFAULT false,
    include_public BOOLEAN NOT NULL DEFAULT false,
    allow_tenant_write BOOLEAN NOT NULL DEFAULT false,
    allow_public_write BOOLEAN NOT NULL DEFAULT false,
    default_write_visibility TEXT NOT NULL DEFAULT 'private',
    default_write_workspace_id UUID,
    created_by_principal_id UUID,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_memory_profile_revisions_profile_version UNIQUE (profile_id, version),
    CONSTRAINT uq_memory_profile_revisions_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_memory_profile_revisions_identity UNIQUE (id, profile_id, tenant_id),
    CONSTRAINT fk_memory_profile_revision_profile FOREIGN KEY (tenant_id, profile_id)
        REFERENCES memory_profiles(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_profile_revision_default_workspace
        FOREIGN KEY (tenant_id, default_write_workspace_id)
        REFERENCES workspaces(tenant_id, id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_memory_profile_revisions_tenant_creator
        FOREIGN KEY (tenant_id, created_by_principal_id)
        REFERENCES principals(tenant_id, id)
        ON DELETE SET NULL (created_by_principal_id),
    CONSTRAINT chk_memory_profile_revision_version CHECK (version > 0),
    CONSTRAINT chk_memory_profile_revision_visibility
        CHECK (default_write_visibility IN ('private', 'workspace', 'tenant', 'public')),
    CONSTRAINT chk_memory_profile_revision_private_shape
        CHECK (default_write_visibility <> 'private' OR default_write_workspace_id IS NULL),
    CONSTRAINT chk_memory_profile_revision_workspace_shape
        CHECK ((default_write_visibility = 'workspace') = (default_write_workspace_id IS NOT NULL)),
    CONSTRAINT chk_memory_profile_revision_tenant_shape
        CHECK (default_write_visibility <> 'tenant'
               OR (default_write_workspace_id IS NULL AND allow_tenant_write)),
    CONSTRAINT chk_memory_profile_revision_public_shape
        CHECK (default_write_visibility <> 'public'
               OR (default_write_workspace_id IS NULL AND allow_public_write))
);

CREATE TABLE IF NOT EXISTS memory_profile_workspace_grants (
    tenant_id UUID NOT NULL,
    revision_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    can_read BOOLEAN NOT NULL,
    can_write BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (revision_id, workspace_id),
    CONSTRAINT fk_memory_profile_grant_revision FOREIGN KEY (tenant_id, revision_id)
        REFERENCES memory_profile_revisions(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_profile_grant_workspace FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES workspaces(tenant_id, id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT chk_memory_profile_grant_write_read CHECK (NOT can_write OR can_read),
    CONSTRAINT chk_memory_profile_grant_nonempty CHECK (can_read OR can_write)
);

CREATE TABLE IF NOT EXISTS memory_profile_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    profile_id UUID NOT NULL,
    revision_id UUID,
    actor_principal_id UUID,
    event_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_memory_profile_event_profile FOREIGN KEY (tenant_id, profile_id)
        REFERENCES memory_profiles(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_profile_event_revision
        FOREIGN KEY (revision_id, profile_id, tenant_id)
        REFERENCES memory_profile_revisions(id, profile_id, tenant_id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_profile_events_tenant_actor
        FOREIGN KEY (tenant_id, actor_principal_id)
        REFERENCES principals(tenant_id, id)
        ON DELETE SET NULL (actor_principal_id),
    CONSTRAINT chk_memory_profile_event_type CHECK (event_type IN (
        'profile_created', 'revision_activated', 'profile_disabled',
        'profile_enabled', 'profile_bound_at_key_issuance'
    ))
);

-- Repair safety for a database on which an earlier development copy of this
-- still-unmerged migration was applied.  Each guard is scoped to the owning
-- relation; constraint names alone are not globally unique in PostgreSQL.
ALTER TABLE memory_profiles
    DROP CONSTRAINT IF EXISTS memory_profiles_created_by_principal_id_fkey;
ALTER TABLE memory_profile_revisions
    DROP CONSTRAINT IF EXISTS memory_profile_revisions_created_by_principal_id_fkey;
ALTER TABLE memory_profile_events
    DROP CONSTRAINT IF EXISTS memory_profile_events_actor_principal_id_fkey;

-- An earlier development copy used RESTRICT for workspace references.  That
-- blocks a tenant cascade because PostgreSQL can reach the workspace before
-- the profile-owned grant/revision rows.  Deferred NO ACTION still rejects a
-- standalone workspace deletion at commit, while allowing the complete
-- tenant graph to disappear in one transaction.
ALTER TABLE memory_profile_revisions
    DROP CONSTRAINT IF EXISTS fk_memory_profile_revision_default_workspace;
ALTER TABLE memory_profile_revisions
    ADD CONSTRAINT fk_memory_profile_revision_default_workspace
    FOREIGN KEY (tenant_id, default_write_workspace_id)
    REFERENCES workspaces(tenant_id, id)
    ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE memory_profile_workspace_grants
    DROP CONSTRAINT IF EXISTS fk_memory_profile_grant_workspace;
ALTER TABLE memory_profile_workspace_grants
    ADD CONSTRAINT fk_memory_profile_grant_workspace
    FOREIGN KEY (tenant_id, workspace_id)
    REFERENCES workspaces(tenant_id, id)
    ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memory_profiles_tenant_creator'
          AND conrelid = 'memory_profiles'::regclass
    ) THEN
        ALTER TABLE memory_profiles
            ADD CONSTRAINT fk_memory_profiles_tenant_creator
            FOREIGN KEY (tenant_id, created_by_principal_id)
            REFERENCES principals(tenant_id, id)
            ON DELETE SET NULL (created_by_principal_id);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memory_profile_revisions_tenant_creator'
          AND conrelid = 'memory_profile_revisions'::regclass
    ) THEN
        ALTER TABLE memory_profile_revisions
            ADD CONSTRAINT fk_memory_profile_revisions_tenant_creator
            FOREIGN KEY (tenant_id, created_by_principal_id)
            REFERENCES principals(tenant_id, id)
            ON DELETE SET NULL (created_by_principal_id);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memory_profile_events_tenant_actor'
          AND conrelid = 'memory_profile_events'::regclass
    ) THEN
        ALTER TABLE memory_profile_events
            ADD CONSTRAINT fk_memory_profile_events_tenant_actor
            FOREIGN KEY (tenant_id, actor_principal_id)
            REFERENCES principals(tenant_id, id)
            ON DELETE SET NULL (actor_principal_id);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memory_profile_event_revision'
          AND conrelid = 'memory_profile_events'::regclass
    ) THEN
        ALTER TABLE memory_profile_events
            ADD CONSTRAINT fk_memory_profile_event_revision
            FOREIGN KEY (revision_id, profile_id, tenant_id)
            REFERENCES memory_profile_revisions(id, profile_id, tenant_id)
            ON DELETE CASCADE;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_memory_profiles_active_revision'
          AND conrelid = 'memory_profiles'::regclass
    ) THEN
        ALTER TABLE memory_profiles ADD CONSTRAINT fk_memory_profiles_active_revision
            FOREIGN KEY (active_revision_id, id, tenant_id)
            REFERENCES memory_profile_revisions(id, profile_id, tenant_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS memory_profile_id UUID;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_api_keys_memory_profile'
          AND conrelid = 'api_keys'::regclass
    ) THEN
        ALTER TABLE api_keys ADD CONSTRAINT fk_api_keys_memory_profile
            FOREIGN KEY (tenant_id, memory_profile_id)
            REFERENCES memory_profiles(tenant_id, id) ON DELETE RESTRICT;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_api_keys_memory_profile_id
    ON api_keys (tenant_id, memory_profile_id) WHERE memory_profile_id IS NOT NULL;

CREATE OR REPLACE FUNCTION enforce_api_key_memory_profile_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.memory_profile_id IS DISTINCT FROM OLD.memory_profile_id THEN
        RAISE EXCEPTION 'api key memory profile binding is immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_api_keys_memory_profile_immutable ON api_keys;
CREATE TRIGGER trg_api_keys_memory_profile_immutable
    BEFORE UPDATE ON api_keys FOR EACH ROW EXECUTE FUNCTION enforce_api_key_memory_profile_immutable();

-- Stable identity metadata is immutable; only lifecycle state/pointer changes.
-- The creator may move from a recorded principal to NULL solely so the
-- composite FK's ON DELETE SET NULL action can preserve historical profiles
-- when that principal is deleted.  The application role has no UPDATE grant
-- on this column, so callers cannot invoke this exception directly.
CREATE OR REPLACE FUNCTION enforce_memory_profile_mutability()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.name IS DISTINCT FROM OLD.name OR NEW.slug IS DISTINCT FROM OLD.slug
       OR NEW.description IS DISTINCT FROM OLD.description
       OR (NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
           AND NOT (OLD.created_by_principal_id IS NOT NULL
                    AND NEW.created_by_principal_id IS NULL))
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'memory profile identity is immutable' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_memory_profiles_mutable ON memory_profiles;
CREATE TRIGGER trg_memory_profiles_mutable BEFORE UPDATE ON memory_profiles
    FOR EACH ROW EXECUTE FUNCTION enforce_memory_profile_mutability();

ALTER TABLE memory_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_profile_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_profile_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_profile_workspace_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_profile_workspace_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_profile_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_profile_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_memory_profiles ON memory_profiles;
CREATE POLICY tenant_isolation_memory_profiles ON memory_profiles
    USING (tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
DROP POLICY IF EXISTS tenant_isolation_memory_profile_revisions ON memory_profile_revisions;
CREATE POLICY tenant_isolation_memory_profile_revisions ON memory_profile_revisions
    USING (tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
DROP POLICY IF EXISTS tenant_isolation_memory_profile_grants ON memory_profile_workspace_grants;
CREATE POLICY tenant_isolation_memory_profile_grants ON memory_profile_workspace_grants
    USING (tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
DROP POLICY IF EXISTS tenant_isolation_memory_profile_events ON memory_profile_events;
CREATE POLICY tenant_isolation_memory_profile_events ON memory_profile_events
    USING (tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT ON memory_profiles TO engram_app;
-- Remove the broad grant from any earlier development application of this
-- still-unmerged migration before installing the narrow lifecycle authority.
REVOKE UPDATE ON memory_profiles FROM engram_app;
GRANT UPDATE (active_revision_id, disabled_at, updated_at)
    ON memory_profiles TO engram_app;
GRANT SELECT, INSERT ON memory_profile_revisions, memory_profile_workspace_grants, memory_profile_events TO engram_app;
REVOKE DELETE ON memory_profiles, memory_profile_revisions, memory_profile_workspace_grants, memory_profile_events FROM engram_app;
REVOKE UPDATE, DELETE ON memory_profile_revisions, memory_profile_workspace_grants, memory_profile_events FROM engram_app;
