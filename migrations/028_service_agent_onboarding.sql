-- Service-authenticated workspace, agent, membership, and API-key provisioning.
-- Extends migration 027 without granting existing service clients new permissions.

-- Re-converge cluster-global posture on upgrade. The persisted password is
-- intentionally untouched.
ALTER ROLE engram_provisioner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
    NOCREATEROLE NOREPLICATION NOINHERIT CONNECTION LIMIT -1 VALID UNTIL 'infinity';
ALTER ROLE engram_provisioner RESET ALL;
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
REVOKE CREATE ON SCHEMA public FROM engram_provisioner;
GRANT USAGE ON SCHEMA public TO engram_provisioner;

CREATE OR REPLACE FUNCTION service_permissions_are_canonical(perms TEXT[]) RETURNS BOOLEAN AS $$
    SELECT cardinality(perms) > 0 AND perms = ARRAY(
        SELECT permission
        FROM unnest(ARRAY[
            'tenant.provision',
            'principal.provision',
            'workspace.provision',
            'agent.provision',
            'api_key.provision'
        ]::TEXT[]) AS permission
        WHERE permission = ANY(perms)
    );
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION current_service_client_has_permission(
    requested_permission TEXT
) RETURNS BOOLEAN AS $$
    SELECT requested_permission = ANY(ARRAY[
               'tenant.provision',
               'principal.provision',
               'workspace.provision',
               'agent.provision',
               'api_key.provision'
           ]::TEXT[])
       AND EXISTS (
           SELECT 1
           FROM service_clients AS client
           WHERE client.id = current_service_client_id()
             AND client.status = 'active'
             AND requested_permission = ANY(client.permissions)
       );
$$ LANGUAGE sql STABLE SET search_path = public, pg_temp;

REVOKE ALL ON FUNCTION current_service_client_has_permission(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION current_service_client_has_permission(TEXT) TO engram_provisioner;

-- Principal bindings are referenced by their complete tenant identity below.
ALTER TABLE principal_provisioning_bindings
    ADD CONSTRAINT uq_principal_provisioning_binding_tenant_identity
    UNIQUE (id, tenant_binding_id, tenant_id);

CREATE TABLE workspace_provisioning_bindings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    tenant_binding_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    external_ref VARCHAR(255) NOT NULL,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (service_client_id, tenant_binding_id, external_ref),
    UNIQUE (workspace_id),
    UNIQUE (id, tenant_binding_id, tenant_id),
    CONSTRAINT chk_workspace_provisioning_ref
        CHECK (external_ref ~ '^[!-~]{1,255}$'),
    CONSTRAINT fk_workspace_binding_tenant_binding
        FOREIGN KEY (tenant_binding_id, tenant_id)
        REFERENCES tenant_provisioning_bindings(id, tenant_id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_workspace_binding_tenant_workspace
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES workspaces(tenant_id, id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX idx_workspace_provisioning_bindings_client_ref
    ON workspace_provisioning_bindings(service_client_id, tenant_binding_id, external_ref);

CREATE TABLE agent_api_key_provisioning_bindings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    tenant_binding_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    workspace_binding_id UUID NOT NULL,
    principal_binding_id UUID NOT NULL,
    external_ref VARCHAR(255) NOT NULL,
    api_key_id UUID NOT NULL REFERENCES api_keys(id) ON DELETE RESTRICT,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    replaces_binding_id UUID
        REFERENCES agent_api_key_provisioning_bindings(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    replaced_at TIMESTAMPTZ,
    UNIQUE (service_client_id, tenant_binding_id, external_ref),
    UNIQUE (api_key_id),
    CONSTRAINT chk_agent_api_key_provisioning_ref
        CHECK (external_ref ~ '^[!-~]{1,255}$'),
    CONSTRAINT chk_agent_api_key_binding_status CHECK (status IN ('active', 'replaced')),
    CONSTRAINT chk_agent_api_key_binding_replaced
        CHECK ((status = 'replaced') = (replaced_at IS NOT NULL)),
    CONSTRAINT fk_agent_api_key_workspace_binding
        FOREIGN KEY (workspace_binding_id, tenant_binding_id, tenant_id)
        REFERENCES workspace_provisioning_bindings(id, tenant_binding_id, tenant_id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_agent_api_key_principal_binding
        FOREIGN KEY (principal_binding_id, tenant_binding_id, tenant_id)
        REFERENCES principal_provisioning_bindings(id, tenant_binding_id, tenant_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE UNIQUE INDEX uq_agent_api_key_replaces_binding
    ON agent_api_key_provisioning_bindings(replaces_binding_id)
    WHERE replaces_binding_id IS NOT NULL;
CREATE UNIQUE INDEX uq_agent_api_key_active_pair
    ON agent_api_key_provisioning_bindings(
        service_client_id, workspace_binding_id, principal_binding_id
    )
    WHERE status = 'active';
CREATE INDEX idx_agent_api_key_provisioning_bindings_client_ref
    ON agent_api_key_provisioning_bindings(
        service_client_id, tenant_binding_id, external_ref
    );

CREATE TABLE service_workspace_agent_idempotency (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    key_digest BYTEA NOT NULL CHECK (octet_length(key_digest) = 32),
    request_digest BYTEA NOT NULL CHECK (octet_length(request_digest) = 32),
    workspace_binding_id UUID NOT NULL
        REFERENCES workspace_provisioning_bindings(id) ON DELETE RESTRICT,
    principal_binding_id UUID NOT NULL
        REFERENCES principal_provisioning_bindings(id) ON DELETE RESTRICT,
    workspace_member_id UUID NOT NULL REFERENCES workspace_members(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (service_client_id, key_digest)
);

CREATE TABLE service_agent_key_idempotency (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_client_id UUID NOT NULL REFERENCES service_clients(id) ON DELETE RESTRICT,
    key_digest BYTEA NOT NULL CHECK (octet_length(key_digest) = 32),
    request_digest BYTEA NOT NULL CHECK (octet_length(request_digest) = 32),
    api_key_binding_id UUID NOT NULL
        REFERENCES agent_api_key_provisioning_bindings(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (service_client_id, key_digest)
);

ALTER TABLE service_provisioning_events
    ADD COLUMN workspace_id UUID REFERENCES workspaces(id) ON DELETE RESTRICT,
    ADD COLUMN api_key_id UUID REFERENCES api_keys(id) ON DELETE RESTRICT,
    ADD COLUMN external_workspace_ref_digest BYTEA,
    ADD COLUMN external_api_key_ref_digest BYTEA;

ALTER TABLE service_provisioning_events
    ADD CONSTRAINT chk_service_event_tenant_ref_digest
        CHECK (
            external_tenant_ref_digest IS NULL
            OR octet_length(external_tenant_ref_digest) = 32
        ),
    ADD CONSTRAINT chk_service_event_principal_ref_digest
        CHECK (
            external_principal_ref_digest IS NULL
            OR octet_length(external_principal_ref_digest) = 32
        ),
    ADD CONSTRAINT chk_service_event_workspace_ref_digest
        CHECK (
            external_workspace_ref_digest IS NULL
            OR octet_length(external_workspace_ref_digest) = 32
        ),
    ADD CONSTRAINT chk_service_event_api_key_ref_digest
        CHECK (
            external_api_key_ref_digest IS NULL
            OR octet_length(external_api_key_ref_digest) = 32
        );

ALTER TABLE service_provisioning_events
    DROP CONSTRAINT chk_service_provisioning_event_type;
ALTER TABLE service_provisioning_events
    ADD CONSTRAINT chk_service_provisioning_event_type CHECK (event_type IN (
        'service_client.created', 'service_client.enabled', 'service_client.disabled',
        'service_client.permissions_changed',
        'service_credential.created', 'service_credential.revoked',
        'tenant.provisioned', 'principal.provisioned',
        'provisioning.resolved_existing', 'provisioning.idempotent_replay',
        'provisioning.conflict',
        'workspace.provisioned', 'agent.provisioned',
        'workspace_member.provisioned', 'workspace_agent.resolved_existing',
        'workspace_agent.idempotent_replay',
        'api_key.provisioned', 'api_key.replaced',
        'api_key.resolved_existing', 'api_key.idempotent_replay',
        'onboarding.conflict'
    ));

ALTER TABLE workspace_provisioning_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_provisioning_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_api_key_provisioning_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_api_key_provisioning_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE service_workspace_agent_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_workspace_agent_idempotency FORCE ROW LEVEL SECURITY;
ALTER TABLE service_agent_key_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_agent_key_idempotency FORCE ROW LEVEL SECURITY;

CREATE POLICY provisioner_workspace_bindings_select
    ON workspace_provisioning_bindings FOR SELECT TO engram_provisioner
    USING (service_client_id = current_service_client_id());
CREATE POLICY provisioner_workspace_bindings_insert
    ON workspace_provisioning_bindings FOR INSERT TO engram_provisioner
    WITH CHECK (
        service_client_id = current_service_client_id()
        AND EXISTS (
            SELECT 1 FROM tenant_provisioning_bindings AS tenant_binding
            WHERE tenant_binding.id = workspace_provisioning_bindings.tenant_binding_id
              AND tenant_binding.tenant_id = workspace_provisioning_bindings.tenant_id
              AND tenant_binding.service_client_id = current_service_client_id()
        )
    );

CREATE POLICY provisioner_agent_key_bindings_select
    ON agent_api_key_provisioning_bindings FOR SELECT TO engram_provisioner
    USING (service_client_id = current_service_client_id());
CREATE POLICY provisioner_agent_key_bindings_insert
    ON agent_api_key_provisioning_bindings FOR INSERT TO engram_provisioner
    WITH CHECK (
        service_client_id = current_service_client_id()
        AND EXISTS (
            SELECT 1 FROM workspace_provisioning_bindings AS workspace_binding
            WHERE workspace_binding.id =
                    agent_api_key_provisioning_bindings.workspace_binding_id
              AND workspace_binding.tenant_binding_id =
                    agent_api_key_provisioning_bindings.tenant_binding_id
              AND workspace_binding.tenant_id =
                    agent_api_key_provisioning_bindings.tenant_id
              AND workspace_binding.service_client_id = current_service_client_id()
        )
        AND EXISTS (
            SELECT 1 FROM principal_provisioning_bindings AS principal_binding
            WHERE principal_binding.id =
                    agent_api_key_provisioning_bindings.principal_binding_id
              AND principal_binding.tenant_binding_id =
                    agent_api_key_provisioning_bindings.tenant_binding_id
              AND principal_binding.tenant_id =
                    agent_api_key_provisioning_bindings.tenant_id
              AND principal_binding.service_client_id = current_service_client_id()
        )
    );
CREATE POLICY provisioner_agent_key_bindings_update
    ON agent_api_key_provisioning_bindings FOR UPDATE TO engram_provisioner
    USING (
        service_client_id = current_service_client_id()
        AND status = 'active'
        AND replaced_at IS NULL
    )
    WITH CHECK (
        service_client_id = current_service_client_id()
        AND status = 'replaced'
        AND replaced_at IS NOT NULL
    );

CREATE POLICY provisioner_workspace_agent_idempotency
    ON service_workspace_agent_idempotency FOR SELECT TO engram_provisioner
    USING (service_client_id = current_service_client_id());
CREATE POLICY provisioner_workspace_agent_idempotency_insert
    ON service_workspace_agent_idempotency FOR INSERT TO engram_provisioner
    WITH CHECK (service_client_id = current_service_client_id());
CREATE POLICY provisioner_agent_key_idempotency
    ON service_agent_key_idempotency FOR SELECT TO engram_provisioner
    USING (service_client_id = current_service_client_id());
CREATE POLICY provisioner_agent_key_idempotency_insert
    ON service_agent_key_idempotency FOR INSERT TO engram_provisioner
    WITH CHECK (service_client_id = current_service_client_id());

CREATE POLICY provisioner_workspaces_insert ON workspaces FOR INSERT TO engram_provisioner
    WITH CHECK (
        current_service_client_has_permission('workspace.provision')
        AND EXISTS (
            SELECT 1 FROM tenant_provisioning_bindings AS binding
            WHERE binding.tenant_id = workspaces.tenant_id
              AND binding.service_client_id = current_service_client_id()
        )
    );
CREATE POLICY provisioner_workspaces_select ON workspaces FOR SELECT TO engram_provisioner
    USING (
        EXISTS (
            SELECT 1 FROM workspace_provisioning_bindings AS binding
            WHERE binding.workspace_id = workspaces.id
              AND binding.tenant_id = workspaces.tenant_id
              AND binding.service_client_id = current_service_client_id()
        )
    );

DROP POLICY provisioner_principals_insert ON principals;
CREATE POLICY provisioner_principals_insert ON principals FOR INSERT TO engram_provisioner
    WITH CHECK (
        internal_key IS NULL
        AND EXISTS (
            SELECT 1 FROM tenant_provisioning_bindings AS binding
            WHERE binding.tenant_id = principals.tenant_id
              AND binding.service_client_id = current_service_client_id()
        )
        AND (
            (type = 'user'
             AND current_service_client_has_permission('principal.provision'))
            OR
            (type = 'agent'
             AND current_service_client_has_permission('agent.provision'))
        )
    );

CREATE POLICY provisioner_workspace_members_select
    ON workspace_members FOR SELECT TO engram_provisioner
    USING (
        EXISTS (
            SELECT 1
            FROM workspace_provisioning_bindings AS workspace_binding
            JOIN principal_provisioning_bindings AS principal_binding
              ON principal_binding.tenant_binding_id =
                    workspace_binding.tenant_binding_id
             AND principal_binding.tenant_id = workspace_binding.tenant_id
             AND principal_binding.service_client_id =
                    workspace_binding.service_client_id
            WHERE workspace_binding.workspace_id = workspace_members.workspace_id
              AND principal_binding.principal_id = workspace_members.principal_id
              AND workspace_binding.service_client_id = current_service_client_id()
        )
    );
CREATE POLICY provisioner_workspace_members_insert
    ON workspace_members FOR INSERT TO engram_provisioner
    WITH CHECK (
        role = 'member'
        AND current_service_client_has_permission('workspace.provision')
        AND current_service_client_has_permission('agent.provision')
        AND EXISTS (
            SELECT 1
            FROM workspace_provisioning_bindings AS workspace_binding
            JOIN principal_provisioning_bindings AS principal_binding
              ON principal_binding.tenant_binding_id =
                    workspace_binding.tenant_binding_id
             AND principal_binding.tenant_id = workspace_binding.tenant_id
             AND principal_binding.service_client_id =
                    workspace_binding.service_client_id
            JOIN principals AS principal
              ON principal.id = principal_binding.principal_id
             AND principal.tenant_id = principal_binding.tenant_id
            WHERE workspace_binding.workspace_id = workspace_members.workspace_id
              AND principal_binding.principal_id = workspace_members.principal_id
              AND workspace_binding.service_client_id = current_service_client_id()
              AND principal.type = 'agent'
              AND principal.internal_key IS NULL
        )
    );

CREATE POLICY provisioner_api_keys_select ON api_keys FOR SELECT TO engram_provisioner
    USING (
        EXISTS (
            SELECT 1 FROM agent_api_key_provisioning_bindings AS binding
            WHERE binding.api_key_id = api_keys.id
              AND binding.tenant_id = api_keys.tenant_id
              AND binding.service_client_id = current_service_client_id()
        )
    );
CREATE POLICY provisioner_api_keys_insert ON api_keys FOR INSERT TO engram_provisioner
    WITH CHECK (
        current_service_client_has_permission('api_key.provision')
        AND principal_id IS NOT NULL
        AND key_hash IS NULL
        AND key_id IS NOT NULL
        AND secret_digest IS NOT NULL
        AND digest_algorithm = 'sha256'
        AND scopes = ARRAY['read', 'write']::TEXT[]
        AND memory_profile_id IS NULL
        AND revoked_at IS NULL
        AND EXISTS (
            SELECT 1
            FROM principal_provisioning_bindings AS principal_binding
            JOIN principals AS principal
              ON principal.id = principal_binding.principal_id
             AND principal.tenant_id = principal_binding.tenant_id
            JOIN workspace_provisioning_bindings AS workspace_binding
              ON workspace_binding.tenant_binding_id =
                    principal_binding.tenant_binding_id
             AND workspace_binding.tenant_id = principal_binding.tenant_id
             AND workspace_binding.service_client_id =
                    principal_binding.service_client_id
            JOIN workspace_members AS member
              ON member.workspace_id = workspace_binding.workspace_id
             AND member.principal_id = principal_binding.principal_id
             AND member.role = 'member'
            WHERE principal_binding.principal_id = api_keys.principal_id
              AND principal_binding.tenant_id = api_keys.tenant_id
              AND principal_binding.service_client_id = current_service_client_id()
              AND principal.type = 'agent'
              AND principal.internal_key IS NULL
        )
    );
CREATE POLICY provisioner_api_keys_revoke ON api_keys FOR UPDATE TO engram_provisioner
    USING (
        revoked_at IS NULL
        AND EXISTS (
            SELECT 1 FROM agent_api_key_provisioning_bindings AS binding
            WHERE binding.api_key_id = api_keys.id
              AND binding.tenant_id = api_keys.tenant_id
              AND binding.service_client_id = current_service_client_id()
              AND binding.status = 'active'
        )
    )
    WITH CHECK (revoked_at IS NOT NULL);

-- Converge a pre-existing provisioner role to the exact intended grants. The
-- role password is deliberately untouched because deployment owns it.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM engram_provisioner;
GRANT SELECT ON service_clients, service_client_credentials TO engram_provisioner;
GRANT UPDATE(updated_at) ON service_clients TO engram_provisioner;
GRANT UPDATE(last_used_at) ON service_client_credentials TO engram_provisioner;
GRANT SELECT, INSERT ON
    tenant_provisioning_bindings,
    principal_provisioning_bindings,
    workspace_provisioning_bindings,
    service_provisioning_idempotency,
    service_workspace_agent_idempotency,
    service_agent_key_idempotency,
    service_provisioning_events
TO engram_provisioner;
GRANT SELECT, INSERT, UPDATE(status, replaced_at)
    ON agent_api_key_provisioning_bindings TO engram_provisioner;
GRANT SELECT, INSERT ON
    tenants, principals, workspaces, workspace_members, api_keys,
    tenant_config, memory_kinds
TO engram_provisioner;
GRANT UPDATE(revoked_at) ON api_keys TO engram_provisioner;

-- Migration 003's owner default privileges otherwise auto-grant new tables to
-- the ordinary application role. Service control-plane state is never part of
-- tenant-principal authority, including the migration-027 tables.
REVOKE ALL ON
    service_clients,
    service_client_credentials,
    tenant_provisioning_bindings,
    principal_provisioning_bindings,
    workspace_provisioning_bindings,
    agent_api_key_provisioning_bindings,
    service_provisioning_idempotency,
    service_workspace_agent_idempotency,
    service_agent_key_idempotency,
    service_provisioning_events
FROM engram_app;
