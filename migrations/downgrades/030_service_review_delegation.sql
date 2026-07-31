-- Deterministic rollback for migration 030.
-- Refuse to remove or reinterpret any review authority or evidence.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM service_clients
        WHERE 'delegation.review.issue' = ANY(permissions)
    ) OR EXISTS (
        SELECT 1 FROM service_delegation_grants
        WHERE authority_class = 'review'
    ) OR EXISTS (
        SELECT 1 FROM service_delegation_tokens
        WHERE authority_class = 'review'
    ) OR EXISTS (
        SELECT 1 FROM service_delegation_events
        WHERE authority_class = 'review'
    ) OR EXISTS (
        SELECT 1
        FROM service_delegation_idempotency AS idempotency
        JOIN service_delegation_tokens AS token
          ON token.id = idempotency.delegation_token_id
        WHERE token.authority_class = 'review'
    ) OR EXISTS (
        SELECT 1 FROM item_events
        WHERE delegated_review_token_id IS NOT NULL
           OR delegated_review_grant_id IS NOT NULL
           OR delegated_review_authority_class IS NOT NULL
           OR delegated_review_purpose IS NOT NULL
    )
    THEN
        RAISE EXCEPTION
            'migration 030 downgrade requires empty delegated review authority and evidence'
            USING ERRCODE = '55000';
    END IF;
END
$$;

DROP FUNCTION issue_service_review_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT,
    TEXT, BYTEA, UUID, TEXT
);
DROP FUNCTION revoke_service_review_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
);
DROP FUNCTION issue_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
);
DROP FUNCTION revoke_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
);
DROP FUNCTION issue_service_delegation_by_class(
    TEXT, TEXT, UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT,
    INTEGER, TEXT, TEXT, BYTEA, UUID, TEXT
);
DROP FUNCTION revoke_service_delegation_by_class(
    TEXT, TEXT, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
);

ALTER FUNCTION issue_service_delegation_029(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
) RENAME TO issue_service_delegation;
ALTER FUNCTION revoke_service_delegation_029(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) RENAME TO revoke_service_delegation;
REVOKE ALL ON FUNCTION issue_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION revoke_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION issue_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
) TO engram_provisioner;
GRANT EXECUTE ON FUNCTION revoke_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) TO engram_provisioner;

DROP TRIGGER trg_service_delegation_token_subject ON service_delegation_tokens;
DROP TRIGGER trg_service_delegation_invalidate_client ON service_clients;
DROP TRIGGER trg_service_delegation_invalidate_credential
    ON service_client_credentials;
DROP TRIGGER trg_service_delegation_invalidate_grant
    ON service_delegation_grants;
DROP TRIGGER trg_service_delegation_invalidate_principal ON principals;
DROP FUNCTION validate_service_delegation_token_subject();
DROP FUNCTION invalidate_service_delegations_for_client();
DROP FUNCTION invalidate_service_delegations_for_credential();
DROP FUNCTION invalidate_service_delegations_for_grant();
DROP FUNCTION invalidate_service_delegations_for_principal();

ALTER FUNCTION validate_service_delegation_token_subject_029()
    RENAME TO validate_service_delegation_token_subject;
ALTER FUNCTION invalidate_service_delegations_for_client_029()
    RENAME TO invalidate_service_delegations_for_client;
ALTER FUNCTION invalidate_service_delegations_for_credential_029()
    RENAME TO invalidate_service_delegations_for_credential;
ALTER FUNCTION invalidate_service_delegations_for_grant_029()
    RENAME TO invalidate_service_delegations_for_grant;
ALTER FUNCTION invalidate_service_delegations_for_principal_029()
    RENAME TO invalidate_service_delegations_for_principal;

ALTER TABLE item_events
    DROP CONSTRAINT fk_item_event_delegated_review_attribution,
    DROP CONSTRAINT chk_item_event_delegated_review_attribution,
    DROP COLUMN delegated_review_token_id,
    DROP COLUMN delegated_review_grant_id,
    DROP COLUMN delegated_review_authority_class,
    DROP COLUMN delegated_review_purpose;

ALTER TABLE service_delegation_events
    DROP CONSTRAINT chk_service_delegation_event_purpose,
    DROP COLUMN purpose_name,
    DROP COLUMN authority_class;

ALTER TABLE service_delegation_tokens
    DROP CONSTRAINT fk_service_delegation_token_grant,
    DROP CONSTRAINT chk_service_delegation_token_state,
    DROP CONSTRAINT chk_service_delegation_token_class_scope,
    DROP CONSTRAINT chk_service_delegation_token_purpose,
    DROP CONSTRAINT uq_service_delegation_token_review_attribution,
    DROP CONSTRAINT uq_service_delegation_token_external_ref_class;
DROP INDEX idx_service_delegation_tokens_external_ref;
ALTER TABLE service_delegation_tokens
    DROP COLUMN purpose_name,
    DROP COLUMN purpose_digest,
    DROP COLUMN target_item_id,
    DROP COLUMN target_review_status,
    DROP COLUMN authority_class;
ALTER TABLE service_delegation_tokens
    ADD CONSTRAINT chk_service_delegation_token_state
        CHECK (
            (
                status = 'active'
                AND used_at IS NULL
                AND revoked_at IS NULL
                AND revocation_reason IS NULL
            )
            OR (
                status = 'used'
                AND used_at IS NOT NULL
                AND revoked_at IS NULL
                AND revocation_reason IS NULL
            )
            OR (
                status = 'revoked'
                AND used_at IS NULL
                AND revoked_at IS NOT NULL
                AND revocation_reason IN (
                    'request_completed',
                    'response_uncertain',
                    'session_revoked',
                    'membership_changed',
                    'entitlement_changed',
                    'operator_action',
                    'credential_rotation',
                    'authority_invalidated',
                    'expired'
                )
            )
        ),
    ADD CONSTRAINT service_delegation_tokens_scopes_check
        CHECK (scopes = ARRAY['read']::TEXT[]),
    ADD CONSTRAINT fk_service_delegation_token_grant
        FOREIGN KEY (
            grant_id, issuer_service_client_id, binding_owner_service_client_id
        )
        REFERENCES service_delegation_grants(
            id, issuer_service_client_id, binding_owner_service_client_id
        ) ON DELETE RESTRICT,
    ADD UNIQUE (
        issuer_service_client_id,
        tenant_binding_id,
        principal_binding_id,
        external_ref
    );
CREATE INDEX idx_service_delegation_tokens_external_ref
    ON service_delegation_tokens(
        issuer_service_client_id,
        tenant_binding_id,
        principal_binding_id,
        external_ref
    );

ALTER TABLE service_delegation_grants
    DROP CONSTRAINT uq_service_delegation_grant_class_identity,
    DROP CONSTRAINT chk_service_delegation_review_grant_ttl;
DROP INDEX uq_service_delegation_active_grant;
DROP INDEX idx_service_delegation_grants_authority;
ALTER TABLE service_delegation_grants
    DROP COLUMN authority_class;
CREATE UNIQUE INDEX uq_service_delegation_active_grant
    ON service_delegation_grants(
        issuer_service_client_id, binding_owner_service_client_id
    )
    WHERE status = 'active';
CREATE INDEX idx_service_delegation_grants_authority
    ON service_delegation_grants(
        issuer_service_client_id, binding_owner_service_client_id, status
    );

CREATE CONSTRAINT TRIGGER trg_service_delegation_token_subject
    AFTER INSERT OR UPDATE ON service_delegation_tokens
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_service_delegation_token_subject();
CREATE TRIGGER trg_service_delegation_invalidate_client
    AFTER UPDATE OF status, permissions ON service_clients
    FOR EACH ROW EXECUTE FUNCTION invalidate_service_delegations_for_client();
CREATE TRIGGER trg_service_delegation_invalidate_credential
    AFTER UPDATE OF service_client_id, status, expires_at
    ON service_client_credentials
    FOR EACH ROW EXECUTE FUNCTION invalidate_service_delegations_for_credential();
CREATE TRIGGER trg_service_delegation_invalidate_grant
    AFTER UPDATE OF issuer_service_client_id, binding_owner_service_client_id, status
    ON service_delegation_grants
    FOR EACH ROW EXECUTE FUNCTION invalidate_service_delegations_for_grant();
CREATE TRIGGER trg_service_delegation_invalidate_principal
    AFTER UPDATE OF type, internal_key ON principals
    FOR EACH ROW EXECUTE FUNCTION invalidate_service_delegations_for_principal();
REVOKE ALL ON FUNCTION validate_service_delegation_token_subject() FROM PUBLIC;
REVOKE ALL ON FUNCTION invalidate_service_delegations_for_client() FROM PUBLIC;
REVOKE ALL ON FUNCTION invalidate_service_delegations_for_credential() FROM PUBLIC;
REVOKE ALL ON FUNCTION invalidate_service_delegations_for_grant() FROM PUBLIC;
REVOKE ALL ON FUNCTION invalidate_service_delegations_for_principal() FROM PUBLIC;

CREATE OR REPLACE FUNCTION service_permissions_are_canonical(perms TEXT[]) RETURNS BOOLEAN AS $$
    SELECT cardinality(perms) > 0 AND perms = ARRAY(
        SELECT permission
        FROM unnest(ARRAY[
            'tenant.provision',
            'principal.provision',
            'workspace.provision',
            'agent.provision',
            'api_key.provision',
            'delegation.issue'
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
               'api_key.provision',
               'delegation.issue'
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
