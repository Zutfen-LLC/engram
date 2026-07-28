-- Deterministic rollback for migration 029.
-- Refuse to discard active delegation state or a permission that migration 028
-- cannot represent. Operators must revoke/remove those objects first.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM service_delegation_grants)
       OR EXISTS (SELECT 1 FROM service_delegation_tokens)
       OR EXISTS (SELECT 1 FROM service_delegation_idempotency)
       OR EXISTS (SELECT 1 FROM service_delegation_events)
       OR EXISTS (
           SELECT 1 FROM service_clients
           WHERE 'delegation.issue' = ANY(permissions)
       )
    THEN
        RAISE EXCEPTION 'migration 029 downgrade requires empty delegation state'
            USING ERRCODE = '55000';
    END IF;
END
$$;

DROP FUNCTION revoke_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
);
DROP FUNCTION issue_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
);
DROP TABLE service_delegation_events;
DROP FUNCTION reject_service_delegation_event_mutation();
DROP TABLE service_delegation_idempotency;
DROP TABLE service_delegation_tokens;
DROP FUNCTION validate_service_delegation_token_subject();
DROP TABLE service_delegation_grants;
DROP FUNCTION enforce_service_delegation_grant_history();

ALTER TABLE principal_provisioning_bindings
    DROP CONSTRAINT uq_principal_provisioning_binding_owner_identity;
ALTER TABLE tenant_provisioning_bindings
    DROP CONSTRAINT uq_tenant_provisioning_binding_owner_identity;

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
GRANT EXECUTE ON FUNCTION current_service_client_has_permission(TEXT)
    TO engram_provisioner;

ALTER ROLE engram_provisioner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
    NOCREATEROLE NOREPLICATION NOINHERIT CONNECTION LIMIT -1 VALID UNTIL 'infinity';
ALTER ROLE engram_provisioner RESET ALL;
REVOKE CREATE ON SCHEMA public FROM engram_provisioner;
GRANT USAGE ON SCHEMA public TO engram_provisioner;
