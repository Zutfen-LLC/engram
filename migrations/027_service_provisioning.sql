-- Service-authenticated tenant and human-principal provisioning.
-- This migration intentionally creates an authority domain separate from
-- tenant principals, ordinary API keys, and the owner/migration role.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engram_provisioner') THEN
        CREATE ROLE engram_provisioner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS NOINHERIT;
    END IF;
END
$$;

-- Converge pre-existing roles as well as freshly-created ones.  This is
-- intentionally explicit: an old manual role must not retain broader flags
-- merely because CREATE ROLE was skipped above.
ALTER ROLE engram_provisioner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
    NOCREATEROLE NOREPLICATION NOINHERIT;
-- Membership permits SET ROLE even when NOINHERIT is set, so converge an
-- existing role to no memberships at all.  This also removes indirect paths
-- to owner or application authority.
DO $$
DECLARE granted_role NAME;
BEGIN
    FOR granted_role IN
        SELECT parent.rolname
        FROM pg_auth_members membership
        JOIN pg_roles member_role ON member_role.oid = membership.member
        JOIN pg_roles parent ON parent.oid = membership.roleid
        WHERE member_role.rolname = 'engram_provisioner'
    LOOP
        EXECUTE format('REVOKE %I FROM engram_provisioner', granted_role);
    END LOOP;
END
$$;

GRANT USAGE ON SCHEMA public TO engram_provisioner;

CREATE OR REPLACE FUNCTION current_service_client_id() RETURNS UUID AS $$
DECLARE raw_value TEXT;
BEGIN
    raw_value := current_setting('app.service_client_id', true);
    IF raw_value IS NULL OR raw_value = '' THEN RETURN NULL; END IF;
    BEGIN
        RETURN raw_value::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN NULL;
    END;
END;
$$ LANGUAGE plpgsql STABLE;

REVOKE ALL ON FUNCTION current_service_client_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION current_service_client_id() TO engram_provisioner;

CREATE OR REPLACE FUNCTION service_permissions_are_canonical(perms TEXT[]) RETURNS BOOLEAN AS $$
    SELECT cardinality(perms) > 0 AND perms = ARRAY(
        SELECT permission
        FROM unnest(ARRAY['tenant.provision', 'principal.provision']::TEXT[]) AS permission
        WHERE permission = ANY(perms)
    );
$$ LANGUAGE sql IMMUTABLE;

CREATE TABLE service_clients (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(255) NOT NULL,
    permissions TEXT[] NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at TIMESTAMPTZ,
    CONSTRAINT chk_service_client_slug CHECK (slug ~ '^[a-z][a-z0-9-]{0,99}$'),
    CONSTRAINT chk_service_client_permissions CHECK (service_permissions_are_canonical(permissions)),
    CONSTRAINT chk_service_client_status CHECK (status IN ('active', 'disabled')),
    CONSTRAINT chk_service_client_disabled CHECK (
        (status = 'disabled') = (disabled_at IS NOT NULL)
    )
);

CREATE TABLE service_client_credentials (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    key_id VARCHAR(64) NOT NULL UNIQUE,
    secret_digest CHAR(64) NOT NULL,
    digest_algorithm VARCHAR(32) NOT NULL DEFAULT 'sha256',
    label VARCHAR(255),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT chk_service_credential_digest CHECK (digest_algorithm = 'sha256'),
    CONSTRAINT chk_service_credential_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT chk_service_credential_revoked CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
);
CREATE INDEX idx_service_client_credentials_client_status
    ON service_client_credentials(service_client_id, status);
CREATE INDEX idx_service_client_credentials_expiry
    ON service_client_credentials(expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE tenant_provisioning_bindings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    external_ref VARCHAR(255) NOT NULL,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(service_client_id, external_ref),
    UNIQUE(tenant_id),
    UNIQUE(id, tenant_id),
    CONSTRAINT chk_tenant_provisioning_ref CHECK (external_ref ~ '^[!-~]{1,255}$')
);
CREATE INDEX idx_tenant_provisioning_bindings_client_ref
    ON tenant_provisioning_bindings(service_client_id, external_ref);

CREATE TABLE principal_provisioning_bindings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    tenant_binding_id UUID NOT NULL REFERENCES tenant_provisioning_bindings(id) ON DELETE RESTRICT,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    external_ref VARCHAR(255) NOT NULL,
    principal_id UUID NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(service_client_id, tenant_binding_id, external_ref),
    UNIQUE(principal_id),
    CONSTRAINT chk_principal_provisioning_ref CHECK (external_ref ~ '^[!-~]{1,255}$'),
    CONSTRAINT fk_principal_binding_tenant_binding FOREIGN KEY (tenant_binding_id, tenant_id)
        REFERENCES tenant_provisioning_bindings(id, tenant_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_principal_binding_tenant_principal FOREIGN KEY (tenant_id, principal_id)
        REFERENCES principals(tenant_id, id) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX idx_principal_provisioning_bindings_client_ref
    ON principal_provisioning_bindings(service_client_id, tenant_binding_id, external_ref);

CREATE TABLE service_provisioning_idempotency (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    key_digest BYTEA NOT NULL,
    request_digest BYTEA NOT NULL,
    tenant_binding_id UUID NOT NULL REFERENCES tenant_provisioning_bindings(id) ON DELETE RESTRICT,
    principal_binding_id UUID NOT NULL REFERENCES principal_provisioning_bindings(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(service_client_id, key_digest)
);

CREATE TABLE service_provisioning_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    credential_id UUID REFERENCES service_client_credentials(id) ON DELETE RESTRICT,
    tenant_id UUID REFERENCES tenants(id) ON DELETE RESTRICT,
    principal_id UUID REFERENCES principals(id) ON DELETE RESTRICT,
    event_type VARCHAR(64) NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    reason_code VARCHAR(64),
    external_tenant_ref_digest BYTEA,
    external_principal_ref_digest BYTEA,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_service_provisioning_event_type CHECK (event_type IN (
        'service_client.created', 'service_client.enabled', 'service_client.disabled',
        'service_credential.created', 'service_credential.revoked', 'tenant.provisioned',
        'principal.provisioned', 'provisioning.resolved_existing',
        'provisioning.idempotent_replay', 'provisioning.conflict'
    )),
    CONSTRAINT chk_service_provisioning_event_outcome CHECK (outcome IN ('success', 'failure')),
    CONSTRAINT chk_service_provisioning_event_details CHECK (jsonb_typeof(details) = 'object')
);

-- Credential discovery happens before a service-client context exists. These
-- tables contain no tenant data and the provisioner has no write authority on
-- clients themselves.
GRANT SELECT ON service_clients, service_client_credentials TO engram_provisioner;
GRANT SELECT, INSERT ON tenant_provisioning_bindings, principal_provisioning_bindings,
    service_provisioning_idempotency, service_provisioning_events TO engram_provisioner;
GRANT SELECT, INSERT ON tenants, principals, tenant_config, memory_kinds TO engram_provisioner;

ALTER TABLE tenant_provisioning_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_provisioning_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE principal_provisioning_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE principal_provisioning_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE service_provisioning_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_provisioning_idempotency FORCE ROW LEVEL SECURITY;
ALTER TABLE service_provisioning_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_provisioning_events FORCE ROW LEVEL SECURITY;
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;

-- The existing builtin-kind trigger runs while the newly inserted tenant is
-- still unbound.  Execute that fixed, migration-owned trigger as its owner;
-- the explicit owner policy below still requires the authenticated service
-- context and direct callers cannot execute the function.
ALTER FUNCTION seed_builtin_memory_kinds() SECURITY DEFINER;
ALTER FUNCTION seed_builtin_memory_kinds() SET search_path = public, pg_temp;
REVOKE ALL ON FUNCTION seed_builtin_memory_kinds() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION seed_builtin_memory_kinds() TO engram;

CREATE POLICY provisioner_bindings ON tenant_provisioning_bindings FOR ALL TO engram_provisioner
    USING (service_client_id = current_service_client_id())
    WITH CHECK (service_client_id = current_service_client_id());
CREATE POLICY provisioner_principal_bindings ON principal_provisioning_bindings FOR ALL TO engram_provisioner
    USING (service_client_id = current_service_client_id())
    WITH CHECK (service_client_id = current_service_client_id());
CREATE POLICY provisioner_idempotency ON service_provisioning_idempotency FOR ALL TO engram_provisioner
    USING (service_client_id = current_service_client_id())
    WITH CHECK (service_client_id = current_service_client_id());
CREATE POLICY provisioner_events_select_insert ON service_provisioning_events FOR SELECT TO engram_provisioner
    USING (service_client_id = current_service_client_id());
CREATE POLICY provisioner_events_insert ON service_provisioning_events FOR INSERT TO engram_provisioner
    WITH CHECK (service_client_id = current_service_client_id());

-- Existing tenant policies remain unchanged. These role-specific policies are
-- additive and only apply to the dedicated provisioner role.
CREATE POLICY provisioner_tenants_insert ON tenants FOR INSERT TO engram_provisioner
    WITH CHECK (current_service_client_id() IS NOT NULL);
CREATE POLICY provisioner_tenants_select ON tenants FOR SELECT TO engram_provisioner
    USING (EXISTS (
        SELECT 1 FROM tenant_provisioning_bindings b
        WHERE b.tenant_id = tenants.id AND b.service_client_id = current_service_client_id()
    ));
CREATE POLICY provisioner_principals_insert ON principals FOR INSERT TO engram_provisioner
    WITH CHECK (
        type = 'user' AND internal_key IS NULL AND EXISTS (
            SELECT 1 FROM tenant_provisioning_bindings b
            WHERE b.tenant_id = principals.tenant_id
              AND b.service_client_id = current_service_client_id()
        )
    );
CREATE POLICY provisioner_principals_select ON principals FOR SELECT TO engram_provisioner
    USING (EXISTS (
        SELECT 1 FROM principal_provisioning_bindings b
        WHERE b.principal_id = principals.id AND b.service_client_id = current_service_client_id()
    ));
CREATE POLICY provisioner_tenant_config_insert ON tenant_config FOR INSERT TO engram_provisioner
    WITH CHECK (EXISTS (
        SELECT 1 FROM tenant_provisioning_bindings b
        WHERE b.tenant_id = tenant_config.tenant_id AND b.service_client_id = current_service_client_id()
    ));
CREATE POLICY provisioner_tenant_config_select ON tenant_config FOR SELECT TO engram_provisioner
    USING (EXISTS (
        SELECT 1 FROM tenant_provisioning_bindings b
        WHERE b.tenant_id = tenant_config.tenant_id AND b.service_client_id = current_service_client_id()
    ));
CREATE POLICY provisioner_memory_kinds_insert ON memory_kinds FOR INSERT TO engram_provisioner
    WITH CHECK (current_service_client_id() IS NOT NULL);
CREATE POLICY provisioner_memory_kinds_select ON memory_kinds FOR SELECT TO engram_provisioner
    USING (EXISTS (
        SELECT 1 FROM tenant_provisioning_bindings b
        WHERE b.tenant_id = memory_kinds.tenant_id AND b.service_client_id = current_service_client_id()
    ));
CREATE POLICY provisioner_seed_memory_kinds_insert ON memory_kinds FOR INSERT TO engram
    WITH CHECK (current_service_client_id() IS NOT NULL);

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM engram_provisioner;
GRANT SELECT ON service_clients, service_client_credentials TO engram_provisioner;
-- These inert per-row UPDATE grants are required by PostgreSQL for
-- SELECT ... FOR UPDATE. They do not permit a provisioner to alter client
-- status, permissions, credentials, or any provisioning resource.
GRANT UPDATE(updated_at) ON service_clients TO engram_provisioner;
GRANT UPDATE(last_used_at) ON service_client_credentials TO engram_provisioner;
GRANT SELECT, INSERT ON tenant_provisioning_bindings, principal_provisioning_bindings,
    service_provisioning_idempotency, service_provisioning_events TO engram_provisioner;
GRANT SELECT, INSERT ON tenants, principals, tenant_config, memory_kinds TO engram_provisioner;
