-- Purpose-bound, single-use delegated review authorization.
-- Existing migration 029 state remains read-only and is backfilled as read.

CREATE OR REPLACE FUNCTION service_permissions_are_canonical(perms TEXT[]) RETURNS BOOLEAN AS $$
    SELECT cardinality(perms) > 0 AND perms = ARRAY(
        SELECT permission
        FROM unnest(ARRAY[
            'tenant.provision',
            'principal.provision',
            'workspace.provision',
            'agent.provision',
            'api_key.provision',
            'delegation.issue',
            'delegation.review.issue'
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
               'delegation.issue',
               'delegation.review.issue'
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

ALTER TABLE service_delegation_grants
    ADD COLUMN authority_class VARCHAR(16) NOT NULL DEFAULT 'read'
        CHECK (authority_class IN ('read', 'review'));
ALTER TABLE service_delegation_grants
    DROP CONSTRAINT chk_service_delegation_grant_distinct_clients;
ALTER TABLE service_delegation_grants
    ADD CONSTRAINT chk_service_delegation_grant_distinct_clients
        CHECK (issuer_service_client_id <> binding_owner_service_client_id);
ALTER TABLE service_delegation_grants
    ADD CONSTRAINT chk_service_delegation_review_grant_ttl
        CHECK (authority_class <> 'review' OR max_ttl_seconds <= 60);
DROP INDEX uq_service_delegation_active_grant;
CREATE UNIQUE INDEX uq_service_delegation_active_grant
    ON service_delegation_grants(
        issuer_service_client_id,
        binding_owner_service_client_id,
        authority_class
    )
    WHERE status = 'active';
DROP INDEX idx_service_delegation_grants_authority;
CREATE INDEX idx_service_delegation_grants_authority
    ON service_delegation_grants(
        issuer_service_client_id,
        binding_owner_service_client_id,
        authority_class,
        status
    );
ALTER TABLE service_delegation_grants
    ADD CONSTRAINT uq_service_delegation_grant_class_identity
    UNIQUE (
        id,
        issuer_service_client_id,
        binding_owner_service_client_id,
        authority_class
    );

ALTER TABLE service_delegation_tokens
    ADD COLUMN authority_class VARCHAR(16) NOT NULL DEFAULT 'read'
        CHECK (authority_class IN ('read', 'review')),
    ADD COLUMN purpose_name VARCHAR(32),
    ADD COLUMN purpose_digest BYTEA,
    ADD COLUMN target_item_id UUID,
    ADD COLUMN target_review_status TEXT;

ALTER TABLE service_delegation_tokens
    DROP CONSTRAINT chk_service_delegation_token_state;
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
                    'expired',
                    'purpose_mismatch'
                )
            )
        );

DO $$
DECLARE constraint_name NAME;
BEGIN
    SELECT con.conname
    INTO constraint_name
    FROM pg_constraint AS con
    WHERE con.conrelid = 'service_delegation_tokens'::regclass
      AND con.contype = 'c'
      AND pg_get_constraintdef(con.oid) LIKE '%scopes = ARRAY%read%';
    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE service_delegation_tokens DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END
$$;

ALTER TABLE service_delegation_tokens
    ADD CONSTRAINT chk_service_delegation_token_class_scope
        CHECK (
            (authority_class = 'read' AND scopes = ARRAY['read']::TEXT[])
            OR
            (authority_class = 'review' AND scopes = ARRAY['review']::TEXT[])
        ),
    ADD CONSTRAINT chk_service_delegation_token_purpose
        CHECK (
            (
                authority_class = 'read'
                AND purpose_name IS NULL
                AND purpose_digest IS NULL
                AND target_item_id IS NULL
                AND target_review_status IS NULL
            )
            OR
            (
                authority_class = 'review'
                AND purpose_name = 'review.queue'
                AND octet_length(purpose_digest) = 32
                AND target_item_id IS NULL
                AND target_review_status IS NULL
            )
            OR
            (
                authority_class = 'review'
                AND purpose_name = 'review.transition'
                AND octet_length(purpose_digest) = 32
                AND target_item_id IS NOT NULL
                AND target_review_status IN ('active', 'rejected')
            )
        ),
    ADD CONSTRAINT uq_service_delegation_token_review_attribution
        UNIQUE (
            id,
            grant_id,
            authority_class,
            purpose_name,
            principal_id
        ),
    ADD CONSTRAINT uq_service_delegation_token_event_attribution
        UNIQUE (
            id,
            issuer_service_client_id,
            binding_owner_service_client_id,
            grant_id,
            authority_class,
            tenant_id,
            principal_id
        ),
    ADD CONSTRAINT uq_service_delegation_token_event_purpose
        UNIQUE (
            id,
            authority_class,
            purpose_name
        ),
    ADD CONSTRAINT uq_service_delegation_token_review_target
        UNIQUE (
            id,
            grant_id,
            authority_class,
            purpose_name,
            principal_id,
            target_item_id,
            target_review_status
        );

ALTER TABLE service_delegation_tokens
    DROP CONSTRAINT fk_service_delegation_token_grant;
ALTER TABLE service_delegation_tokens
    ADD CONSTRAINT fk_service_delegation_token_grant
        FOREIGN KEY (
            grant_id,
            issuer_service_client_id,
            binding_owner_service_client_id,
            authority_class
        )
        REFERENCES service_delegation_grants(
            id,
            issuer_service_client_id,
            binding_owner_service_client_id,
            authority_class
        ) ON DELETE RESTRICT;

DO $$
DECLARE constraint_name NAME;
BEGIN
    SELECT con.conname
    INTO constraint_name
    FROM pg_constraint AS con
    WHERE con.conrelid = 'service_delegation_tokens'::regclass
      AND con.contype = 'u'
      AND (
          SELECT array_agg(att.attname ORDER BY key.ordinality)
          FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinality)
          JOIN pg_attribute AS att
            ON att.attrelid = con.conrelid AND att.attnum = key.attnum
      ) = ARRAY[
          'issuer_service_client_id',
          'tenant_binding_id',
          'principal_binding_id',
          'external_ref'
      ]::NAME[];
    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE service_delegation_tokens DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END
$$;
ALTER TABLE service_delegation_tokens
    ADD CONSTRAINT uq_service_delegation_token_external_ref_class
    UNIQUE (
        issuer_service_client_id,
        tenant_binding_id,
        principal_binding_id,
        authority_class,
        external_ref
    );
DROP INDEX idx_service_delegation_tokens_external_ref;
CREATE INDEX idx_service_delegation_tokens_external_ref
    ON service_delegation_tokens(
        issuer_service_client_id,
        tenant_binding_id,
        principal_binding_id,
        authority_class,
        external_ref
    );

ALTER TABLE service_delegation_events
    ADD COLUMN authority_class VARCHAR(16) NOT NULL DEFAULT 'read'
        CHECK (authority_class IN ('read', 'review')),
    ADD COLUMN purpose_name VARCHAR(32);
ALTER TABLE service_delegation_events
    ADD CONSTRAINT chk_service_delegation_event_purpose
        CHECK (
            (authority_class = 'read' AND purpose_name IS NULL)
            OR (
                authority_class = 'review'
                AND (
                    (
                        event_type IN (
                            'delegation_grant.created',
                            'delegation_grant.revoked'
                        )
                        AND purpose_name IS NULL
                    )
                    OR (
                        event_type = 'delegation.resolved_existing'
                        AND delegation_token_id IS NULL
                        AND tenant_id IS NULL
                        AND principal_id IS NULL
                        AND purpose_name IS NULL
                        AND details ->> 'disposition' = 'not_found'
                    )
                    OR purpose_name IN ('review.queue', 'review.transition')
                )
            )
        ),
    ADD CONSTRAINT chk_service_delegation_event_token_attribution
        CHECK (
            delegation_token_id IS NULL
            OR (
                issuer_service_client_id IS NOT NULL
                AND binding_owner_service_client_id IS NOT NULL
                AND grant_id IS NOT NULL
                AND tenant_id IS NOT NULL
                AND principal_id IS NOT NULL
                AND (authority_class = 'read' OR purpose_name IS NOT NULL)
            )
        ),
    ADD CONSTRAINT fk_service_delegation_event_token_attribution
        FOREIGN KEY (
            delegation_token_id,
            issuer_service_client_id,
            binding_owner_service_client_id,
            grant_id,
            authority_class,
            tenant_id,
            principal_id
        )
        REFERENCES service_delegation_tokens(
            id,
            issuer_service_client_id,
            binding_owner_service_client_id,
            grant_id,
            authority_class,
            tenant_id,
            principal_id
        ) ON DELETE RESTRICT NOT VALID,
    ADD CONSTRAINT fk_service_delegation_event_token_purpose
        FOREIGN KEY (
            delegation_token_id,
            authority_class,
            purpose_name
        )
        REFERENCES service_delegation_tokens(
            id,
            authority_class,
            purpose_name
        ) NOT VALID;

ALTER TABLE item_events
    ADD COLUMN delegated_review_token_id UUID,
    ADD COLUMN delegated_review_grant_id UUID,
    ADD COLUMN delegated_review_authority_class VARCHAR(16),
    ADD COLUMN delegated_review_purpose VARCHAR(32);
ALTER TABLE item_events
    ADD CONSTRAINT chk_item_event_delegated_review_attribution
        CHECK (
            (
                delegated_review_token_id IS NULL
                AND delegated_review_grant_id IS NULL
                AND delegated_review_authority_class IS NULL
                AND delegated_review_purpose IS NULL
            )
            OR
            (
                delegated_review_token_id IS NOT NULL
                AND delegated_review_grant_id IS NOT NULL
                AND delegated_review_authority_class = 'review'
                AND delegated_review_purpose = 'review.transition'
                AND actor_principal_id IS NOT NULL
                AND event_type = 'review_change'
                AND field_name = 'review_status'
                AND new_value IS NOT NULL
                AND new_value IN ('active', 'rejected')
            )
        ),
    ADD CONSTRAINT fk_item_event_delegated_review_attribution
        FOREIGN KEY (
            delegated_review_token_id,
            delegated_review_grant_id,
            delegated_review_authority_class,
            delegated_review_purpose,
            actor_principal_id,
            item_id,
            new_value
        )
        REFERENCES service_delegation_tokens(
            id,
            grant_id,
            authority_class,
            purpose_name,
            principal_id,
            target_item_id,
            target_review_status
        ) ON DELETE RESTRICT;

DROP TRIGGER trg_service_delegation_token_subject ON service_delegation_tokens;
ALTER FUNCTION validate_service_delegation_token_subject()
    RENAME TO validate_service_delegation_token_subject_029;
CREATE FUNCTION validate_service_delegation_token_subject() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF ROW(
               OLD.grant_id,
               OLD.issuer_service_client_id,
               OLD.issuer_credential_id,
               OLD.binding_owner_service_client_id,
               OLD.tenant_binding_id,
               OLD.principal_binding_id,
               OLD.tenant_id,
               OLD.principal_id,
               OLD.authority_class
           ) IS NOT DISTINCT FROM ROW(
               NEW.grant_id,
               NEW.issuer_service_client_id,
               NEW.issuer_credential_id,
               NEW.binding_owner_service_client_id,
               NEW.tenant_binding_id,
               NEW.principal_binding_id,
               NEW.tenant_id,
               NEW.principal_id,
               NEW.authority_class
           )
        THEN
            RETURN NEW;
        END IF;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM principals AS principal
        JOIN principal_provisioning_bindings AS binding
          ON binding.principal_id = principal.id
         AND binding.tenant_id = principal.tenant_id
        JOIN service_delegation_grants AS delegation_grant
          ON delegation_grant.id = NEW.grant_id
         AND delegation_grant.issuer_service_client_id =
             NEW.issuer_service_client_id
         AND delegation_grant.binding_owner_service_client_id =
             NEW.binding_owner_service_client_id
         AND delegation_grant.authority_class = NEW.authority_class
        WHERE principal.id = NEW.principal_id
          AND principal.tenant_id = NEW.tenant_id
          AND principal.type = 'user'
          AND principal.internal_key IS NULL
          AND binding.id = NEW.principal_binding_id
          AND binding.service_client_id = NEW.binding_owner_service_client_id
          AND binding.tenant_binding_id = NEW.tenant_binding_id
          AND EXISTS (
              SELECT 1
              FROM service_client_credentials AS credential
              WHERE credential.id = NEW.issuer_credential_id
                AND credential.service_client_id =
                    NEW.issuer_service_client_id
          )
    ) THEN
        RAISE EXCEPTION 'delegation subject integrity violation'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;
CREATE CONSTRAINT TRIGGER trg_service_delegation_token_subject
    AFTER INSERT OR UPDATE ON service_delegation_tokens
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_service_delegation_token_subject();
REVOKE ALL ON FUNCTION validate_service_delegation_token_subject() FROM PUBLIC;

DROP TRIGGER trg_service_delegation_invalidate_client ON service_clients;
DROP TRIGGER trg_service_delegation_invalidate_credential
    ON service_client_credentials;
DROP TRIGGER trg_service_delegation_invalidate_grant
    ON service_delegation_grants;
DROP TRIGGER trg_service_delegation_invalidate_principal ON principals;
ALTER FUNCTION invalidate_service_delegations_for_client()
    RENAME TO invalidate_service_delegations_for_client_029;
ALTER FUNCTION invalidate_service_delegations_for_credential()
    RENAME TO invalidate_service_delegations_for_credential_029;
ALTER FUNCTION invalidate_service_delegations_for_grant()
    RENAME TO invalidate_service_delegations_for_grant_029;
ALTER FUNCTION invalidate_service_delegations_for_principal()
    RENAME TO invalidate_service_delegations_for_principal_029;

CREATE FUNCTION invalidate_service_delegations_for_client() RETURNS TRIGGER AS $$
DECLARE
    v_token RECORD;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_read_changed BOOLEAN;
    v_review_changed BOOLEAN;
    v_owner_changed BOOLEAN;
BEGIN
    v_read_changed := (
        OLD.status = 'active' AND 'delegation.issue' = ANY(OLD.permissions)
    ) IS DISTINCT FROM (
        NEW.status = 'active' AND 'delegation.issue' = ANY(NEW.permissions)
    );
    v_review_changed := (
        OLD.status = 'active'
        AND 'delegation.review.issue' = ANY(OLD.permissions)
    ) IS DISTINCT FROM (
        NEW.status = 'active'
        AND 'delegation.review.issue' = ANY(NEW.permissions)
    );
    v_owner_changed :=
        (OLD.status = 'active') IS DISTINCT FROM (NEW.status = 'active');
    FOR v_token IN
        UPDATE service_delegation_tokens
        SET status = 'revoked',
            revoked_at = v_now,
            revocation_reason = 'authority_invalidated'
        WHERE status = 'active'
          AND (
              (
                  issuer_service_client_id = NEW.id
                  AND (
                      (authority_class = 'read' AND v_read_changed)
                      OR (authority_class = 'review' AND v_review_changed)
                  )
              )
              OR (
                  binding_owner_service_client_id = NEW.id
                  AND v_owner_changed
              )
          )
        RETURNING *
    LOOP
        INSERT INTO service_delegation_events (
            event_type, outcome, issuer_service_client_id,
            issuer_credential_id, binding_owner_service_client_id,
            grant_id, delegation_token_id, tenant_id, principal_id,
            authority_class, purpose_name, request_id, reason_code, details
        ) VALUES (
            'delegation.denied', 'failure',
            v_token.issuer_service_client_id, v_token.issuer_credential_id,
            v_token.binding_owner_service_client_id, v_token.grant_id,
            v_token.id, v_token.tenant_id, v_token.principal_id,
            v_token.authority_class, v_token.purpose_name,
            'authority-invalidation', 'authority_invalidated',
            '{"disposition":"denied"}'::jsonb
        );
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;
CREATE TRIGGER trg_service_delegation_invalidate_client
    AFTER UPDATE OF status, permissions ON service_clients
    FOR EACH ROW EXECUTE FUNCTION invalidate_service_delegations_for_client();
REVOKE ALL ON FUNCTION invalidate_service_delegations_for_client() FROM PUBLIC;

CREATE FUNCTION invalidate_service_delegations_for_credential() RETURNS TRIGGER AS $$
DECLARE
    v_token RECORD;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_authority_changed BOOLEAN;
BEGIN
    v_authority_changed := (
        OLD.status = 'active'
        AND (OLD.expires_at IS NULL OR OLD.expires_at > v_now)
    ) IS DISTINCT FROM (
        NEW.status = 'active'
        AND (NEW.expires_at IS NULL OR NEW.expires_at > v_now)
    ) OR OLD.service_client_id IS DISTINCT FROM NEW.service_client_id;
    IF v_authority_changed THEN
        FOR v_token IN
            UPDATE service_delegation_tokens
            SET status = 'revoked',
                revoked_at = v_now,
                revocation_reason = 'authority_invalidated'
            WHERE status = 'active' AND issuer_credential_id = NEW.id
            RETURNING *
        LOOP
            INSERT INTO service_delegation_events (
                event_type, outcome, issuer_service_client_id,
                issuer_credential_id, binding_owner_service_client_id,
                grant_id, delegation_token_id, tenant_id, principal_id,
                authority_class, purpose_name, request_id, reason_code, details
            ) VALUES (
                'delegation.denied', 'failure',
                v_token.issuer_service_client_id, v_token.issuer_credential_id,
                v_token.binding_owner_service_client_id, v_token.grant_id,
                v_token.id, v_token.tenant_id, v_token.principal_id,
                v_token.authority_class, v_token.purpose_name,
                'authority-invalidation', 'authority_invalidated',
                '{"disposition":"denied"}'::jsonb
            );
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;
CREATE TRIGGER trg_service_delegation_invalidate_credential
    AFTER UPDATE OF service_client_id, status, expires_at
    ON service_client_credentials
    FOR EACH ROW EXECUTE FUNCTION invalidate_service_delegations_for_credential();
REVOKE ALL ON FUNCTION invalidate_service_delegations_for_credential() FROM PUBLIC;

CREATE FUNCTION invalidate_service_delegations_for_grant() RETURNS TRIGGER AS $$
DECLARE
    v_token RECORD;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status
       OR OLD.issuer_service_client_id IS DISTINCT FROM NEW.issuer_service_client_id
       OR OLD.binding_owner_service_client_id
          IS DISTINCT FROM NEW.binding_owner_service_client_id
       OR OLD.authority_class IS DISTINCT FROM NEW.authority_class
    THEN
        FOR v_token IN
            UPDATE service_delegation_tokens
            SET status = 'revoked',
                revoked_at = v_now,
                revocation_reason = 'authority_invalidated'
            WHERE status = 'active' AND grant_id = NEW.id
            RETURNING *
        LOOP
            INSERT INTO service_delegation_events (
                event_type, outcome, issuer_service_client_id,
                issuer_credential_id, binding_owner_service_client_id,
                grant_id, delegation_token_id, tenant_id, principal_id,
                authority_class, purpose_name, request_id, reason_code, details
            ) VALUES (
                'delegation.denied', 'failure',
                v_token.issuer_service_client_id, v_token.issuer_credential_id,
                v_token.binding_owner_service_client_id, v_token.grant_id,
                v_token.id, v_token.tenant_id, v_token.principal_id,
                v_token.authority_class, v_token.purpose_name,
                'authority-invalidation', 'authority_invalidated',
                '{"disposition":"denied"}'::jsonb
            );
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;
CREATE TRIGGER trg_service_delegation_invalidate_grant
    AFTER UPDATE OF
        issuer_service_client_id,
        binding_owner_service_client_id,
        authority_class,
        status
    ON service_delegation_grants
    FOR EACH ROW EXECUTE FUNCTION invalidate_service_delegations_for_grant();
REVOKE ALL ON FUNCTION invalidate_service_delegations_for_grant() FROM PUBLIC;

CREATE FUNCTION invalidate_service_delegations_for_principal() RETURNS TRIGGER AS $$
DECLARE
    v_token RECORD;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF (OLD.type = 'user' AND OLD.internal_key IS NULL)
       IS DISTINCT FROM (NEW.type = 'user' AND NEW.internal_key IS NULL)
    THEN
        FOR v_token IN
            UPDATE service_delegation_tokens
            SET status = 'revoked',
                revoked_at = v_now,
                revocation_reason = 'authority_invalidated'
            WHERE status = 'active' AND principal_id = NEW.id
            RETURNING *
        LOOP
            INSERT INTO service_delegation_events (
                event_type, outcome, issuer_service_client_id,
                issuer_credential_id, binding_owner_service_client_id,
                grant_id, delegation_token_id, tenant_id, principal_id,
                authority_class, purpose_name, request_id, reason_code, details
            ) VALUES (
                'delegation.denied', 'failure',
                v_token.issuer_service_client_id, v_token.issuer_credential_id,
                v_token.binding_owner_service_client_id, v_token.grant_id,
                v_token.id, v_token.tenant_id, v_token.principal_id,
                v_token.authority_class, v_token.purpose_name,
                'authority-invalidation', 'authority_invalidated',
                '{"disposition":"denied"}'::jsonb
            );
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;
CREATE TRIGGER trg_service_delegation_invalidate_principal
    AFTER UPDATE OF type, internal_key ON principals
    FOR EACH ROW EXECUTE FUNCTION invalidate_service_delegations_for_principal();
REVOKE ALL ON FUNCTION invalidate_service_delegations_for_principal() FROM PUBLIC;

-- Internal generic issue function. Only its two narrow wrappers are granted.
ALTER FUNCTION issue_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
) RENAME TO issue_service_delegation_029;
ALTER FUNCTION revoke_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) RENAME TO revoke_service_delegation_029;
REVOKE ALL ON FUNCTION issue_service_delegation_029(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
) FROM PUBLIC, engram_provisioner;
REVOKE ALL ON FUNCTION revoke_service_delegation_029(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC, engram_provisioner;

CREATE FUNCTION issue_service_delegation_by_class(
    p_authority_class TEXT,
    p_required_permission TEXT,
    p_issuer_credential_id UUID,
    p_binding_owner_slug TEXT,
    p_tenant_external_ref TEXT,
    p_principal_external_ref TEXT,
    p_delegation_external_ref TEXT,
    p_idempotency_key_digest BYTEA,
    p_request_digest BYTEA,
    p_key_id TEXT,
    p_secret_digest TEXT,
    p_ttl_seconds INTEGER,
    p_request_id TEXT,
    p_purpose_name TEXT,
    p_purpose_digest BYTEA,
    p_target_item_id UUID,
    p_target_review_status TEXT
) RETURNS TABLE (
    created BOOLEAN,
    idempotency_replayed BOOLEAN,
    credential_secret_available BOOLEAN,
    delegation_token_id UUID,
    issued_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    error_code TEXT
) AS $$
DECLARE
    v_issuer_id UUID := current_service_client_id();
    v_issuer RECORD;
    v_owner RECORD;
    v_grant RECORD;
    v_tenant_binding RECORD;
    v_principal_binding RECORD;
    v_existing_idem RECORD;
    v_existing_token RECORD;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_token_id UUID;
    v_canonical_request_digest BYTEA;
    v_scopes TEXT[];
BEGIN
    IF p_authority_class = 'read'
       AND p_required_permission = 'delegation.issue'
       AND p_purpose_name IS NULL
       AND p_purpose_digest IS NULL
       AND p_target_item_id IS NULL
       AND p_target_review_status IS NULL
    THEN
        v_scopes := ARRAY['read']::TEXT[];
    ELSIF p_authority_class = 'review'
       AND p_required_permission = 'delegation.review.issue'
       AND p_purpose_name IN ('review.queue', 'review.transition')
       AND octet_length(p_purpose_digest) = 32
       AND (
           (
               p_purpose_name = 'review.queue'
               AND p_target_item_id IS NULL
               AND p_target_review_status IS NULL
           )
           OR (
               p_purpose_name = 'review.transition'
               AND p_target_item_id IS NOT NULL
               AND p_target_review_status IN ('active', 'rejected')
           )
       )
    THEN
        v_scopes := ARRAY['review']::TEXT[];
    ELSE
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'DELEGATION_CONFLICT'::TEXT;
        RETURN;
    END IF;
    IF v_issuer_id IS NULL THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'SERVICE_UNAUTHORIZED'::TEXT;
        RETURN;
    END IF;
    SELECT client.id, client.status AS client_status, client.permissions,
        credential.id AS credential_id,
        credential.service_client_id AS credential_client_id,
        credential.status AS credential_status,
        credential.expires_at AS credential_expires_at
    INTO v_issuer
    FROM service_clients AS client
    JOIN service_client_credentials AS credential
      ON credential.id = p_issuer_credential_id
    WHERE client.id = v_issuer_id
    FOR UPDATE OF client, credential;
    IF NOT FOUND
       OR v_issuer.client_status <> 'active'
       OR v_issuer.credential_client_id <> v_issuer_id
       OR v_issuer.credential_status <> 'active'
       OR (
           v_issuer.credential_expires_at IS NOT NULL
           AND v_issuer.credential_expires_at <= v_now
       )
    THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'SERVICE_UNAUTHORIZED'::TEXT;
        RETURN;
    END IF;
    IF NOT (p_required_permission = ANY(v_issuer.permissions)) THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'SERVICE_FORBIDDEN'::TEXT;
        RETURN;
    END IF;
    IF p_binding_owner_slug !~ '^[a-z][a-z0-9-]{0,99}$'
       OR p_tenant_external_ref !~ '^[!-~]{1,255}$'
       OR p_principal_external_ref !~ '^[!-~]{1,255}$'
       OR p_delegation_external_ref !~ '^[!-~]{1,255}$'
       OR p_request_id !~ '^[!-~]{1,128}$'
       OR octet_length(p_idempotency_key_digest) <> 32
       OR octet_length(p_request_digest) <> 32
       OR p_key_id !~ '^[0-9A-Za-z]{22}$'
       OR p_secret_digest !~ '^[0-9a-f]{64}$'
       OR p_ttl_seconds NOT BETWEEN 30 AND (
           CASE WHEN p_authority_class = 'review' THEN 60 ELSE 300 END
       )
    THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'DELEGATION_CONFLICT'::TEXT;
        RETURN;
    END IF;
    SELECT id, status INTO v_owner
    FROM service_clients
    WHERE slug = p_binding_owner_slug
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'BINDING_OWNER_NOT_FOUND'::TEXT;
        RETURN;
    END IF;
    IF v_owner.id = v_issuer_id OR v_owner.status <> 'active' THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'DELEGATION_GRANT_NOT_FOUND'::TEXT;
        RETURN;
    END IF;
    SELECT id, max_ttl_seconds, status INTO v_grant
    FROM service_delegation_grants
    WHERE issuer_service_client_id = v_issuer_id
      AND binding_owner_service_client_id = v_owner.id
      AND authority_class = p_authority_class
      AND status = 'active'
    FOR UPDATE;
    IF NOT FOUND OR p_ttl_seconds > v_grant.max_ttl_seconds THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'DELEGATION_GRANT_NOT_FOUND'::TEXT;
        RETURN;
    END IF;
    SELECT id, tenant_id INTO v_tenant_binding
    FROM tenant_provisioning_bindings
    WHERE service_client_id = v_owner.id
      AND external_ref = p_tenant_external_ref
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'TENANT_BINDING_NOT_FOUND'::TEXT;
        RETURN;
    END IF;
    SELECT binding.id, binding.principal_id, principal.type, principal.internal_key
    INTO v_principal_binding
    FROM principal_provisioning_bindings AS binding
    JOIN principals AS principal
      ON principal.id = binding.principal_id
     AND principal.tenant_id = binding.tenant_id
    WHERE binding.service_client_id = v_owner.id
      AND binding.tenant_binding_id = v_tenant_binding.id
      AND binding.tenant_id = v_tenant_binding.tenant_id
      AND binding.external_ref = p_principal_external_ref
    FOR UPDATE OF binding, principal;
    IF NOT FOUND THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'PRINCIPAL_BINDING_NOT_FOUND'::TEXT;
        RETURN;
    END IF;
    IF v_principal_binding.type <> 'user'
       OR v_principal_binding.internal_key IS NOT NULL
    THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'DELEGATION_SUBJECT_INVALID'::TEXT;
        RETURN;
    END IF;
    IF p_authority_class = 'read' THEN
        v_canonical_request_digest := sha256(convert_to(replace(replace(
            jsonb_build_object(
                'audience', 'engram-core',
                'binding_owner_service_client_id', v_owner.id::TEXT,
                'delegation_external_ref', p_delegation_external_ref,
                'grant_id', v_grant.id::TEXT,
                'issuer_service_client_id', v_issuer_id::TEXT,
                'principal_binding_id', v_principal_binding.id::TEXT,
                'schema_version', 1,
                'scopes', jsonb_build_array('read'),
                'single_use', true,
                'tenant_binding_id', v_tenant_binding.id::TEXT,
                'ttl_seconds', p_ttl_seconds
            )::TEXT, ': ', ':'), ', ', ','), 'UTF8'));
    ELSE
        v_canonical_request_digest := sha256(convert_to(replace(replace(
            jsonb_build_object(
                'audience', 'engram-core',
                'authority_class', 'review',
                'binding_owner_service_client_id', v_owner.id::TEXT,
                'delegation_external_ref', p_delegation_external_ref,
                'grant_id', v_grant.id::TEXT,
                'issuer_service_client_id', v_issuer_id::TEXT,
                'principal_binding_id', v_principal_binding.id::TEXT,
                'purpose_digest', encode(p_purpose_digest, 'hex'),
                'schema_version', 1,
                'scopes', jsonb_build_array('review'),
                'single_use', true,
                'tenant_binding_id', v_tenant_binding.id::TEXT,
                'ttl_seconds', p_ttl_seconds
            )::TEXT, ': ', ':'), ', ', ','), 'UTF8'));
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        v_issuer_id::TEXT || ':delegation-idempotency:' ||
        encode(p_idempotency_key_digest, 'hex'), 0
    ));
    PERFORM pg_advisory_xact_lock(hashtextextended(
        v_issuer_id::TEXT || ':delegation-external:' ||
        p_authority_class || ':' || v_tenant_binding.id::TEXT || ':' ||
        v_principal_binding.id::TEXT || ':' || p_delegation_external_ref, 0
    ));
    -- Idempotency keys remain issuer-global. If a key crosses authority
    -- classes, token attribution in the conflict event comes only from the
    -- existing token.
    SELECT idem.request_digest, token.*
    INTO v_existing_idem
    FROM service_delegation_idempotency AS idem
    JOIN service_delegation_tokens AS token
      ON token.id = idem.delegation_token_id
    WHERE idem.issuer_service_client_id = v_issuer_id
      AND idem.key_digest = p_idempotency_key_digest;
    IF FOUND THEN
        IF v_existing_idem.request_digest <> v_canonical_request_digest THEN
            INSERT INTO service_delegation_events (
                event_type, outcome, issuer_service_client_id,
                issuer_credential_id, binding_owner_service_client_id,
                grant_id, delegation_token_id, tenant_id, principal_id,
                authority_class, purpose_name, request_id, reason_code,
                external_tenant_ref_digest, external_principal_ref_digest,
                external_delegation_ref_digest, details
            ) VALUES (
                'delegation.conflict', 'failure',
                v_existing_idem.issuer_service_client_id,
                p_issuer_credential_id,
                v_existing_idem.binding_owner_service_client_id,
                v_existing_idem.grant_id,
                v_existing_idem.id, v_existing_idem.tenant_id,
                v_existing_idem.principal_id,
                v_existing_idem.authority_class,
                v_existing_idem.purpose_name,
                p_request_id, 'idempotency_key_reused',
                sha256(convert_to(p_tenant_external_ref, 'UTF8')),
                sha256(convert_to(p_principal_external_ref, 'UTF8')),
                sha256(convert_to(p_delegation_external_ref, 'UTF8')),
                '{"disposition":"conflict"}'::jsonb
            );
            UPDATE service_client_credentials SET last_used_at = v_now
            WHERE id = p_issuer_credential_id;
            RETURN QUERY SELECT false, false, false, NULL::UUID,
                NULL::TIMESTAMPTZ, NULL::TIMESTAMPTZ,
                'IDEMPOTENCY_KEY_REUSED'::TEXT;
            RETURN;
        END IF;
        INSERT INTO service_delegation_events (
            event_type, outcome, issuer_service_client_id,
            issuer_credential_id, binding_owner_service_client_id,
            grant_id, delegation_token_id, tenant_id, principal_id,
            authority_class, purpose_name, request_id, reason_code,
            external_tenant_ref_digest, external_principal_ref_digest,
            external_delegation_ref_digest, details
        ) VALUES (
            'delegation.idempotent_replay', 'success', v_issuer_id,
            p_issuer_credential_id,
            v_existing_idem.binding_owner_service_client_id,
            v_existing_idem.grant_id, v_existing_idem.id,
            v_existing_idem.tenant_id, v_existing_idem.principal_id,
            v_existing_idem.authority_class, v_existing_idem.purpose_name,
            p_request_id, 'replayed',
            sha256(convert_to(p_tenant_external_ref, 'UTF8')),
            sha256(convert_to(p_principal_external_ref, 'UTF8')),
            sha256(convert_to(p_delegation_external_ref, 'UTF8')),
            jsonb_build_object(
                'ttl_seconds', EXTRACT(EPOCH FROM (
                    v_existing_idem.expires_at - v_existing_idem.issued_at
                ))::INTEGER,
                'scope_read', v_existing_idem.authority_class = 'read',
                'single_use', true,
                'disposition', 'replayed'
            )
        );
        UPDATE service_client_credentials SET last_used_at = v_now
        WHERE id = p_issuer_credential_id;
        RETURN QUERY SELECT false, true, false, v_existing_idem.id,
            v_existing_idem.issued_at, v_existing_idem.expires_at, NULL::TEXT;
        RETURN;
    END IF;
    SELECT * INTO v_existing_token
    FROM service_delegation_tokens
    WHERE issuer_service_client_id = v_issuer_id
      AND tenant_binding_id = v_tenant_binding.id
      AND principal_binding_id = v_principal_binding.id
      AND authority_class = p_authority_class
      AND external_ref = p_delegation_external_ref;
    IF FOUND THEN
        IF v_existing_token.grant_id <> v_grant.id
           OR v_existing_token.binding_owner_service_client_id <> v_owner.id
           OR EXTRACT(EPOCH FROM (
               v_existing_token.expires_at - v_existing_token.issued_at
           ))::INTEGER <> p_ttl_seconds
           OR v_existing_token.purpose_name IS DISTINCT FROM p_purpose_name
           OR v_existing_token.purpose_digest IS DISTINCT FROM p_purpose_digest
           OR v_existing_token.target_item_id IS DISTINCT FROM p_target_item_id
           OR v_existing_token.target_review_status
              IS DISTINCT FROM p_target_review_status
        THEN
            INSERT INTO service_delegation_events (
                event_type, outcome, issuer_service_client_id,
                issuer_credential_id, binding_owner_service_client_id,
                grant_id, delegation_token_id, tenant_id, principal_id,
                authority_class, purpose_name, request_id, reason_code,
                external_tenant_ref_digest, external_principal_ref_digest,
                external_delegation_ref_digest, details
            ) VALUES (
                'delegation.conflict', 'failure',
                v_existing_token.issuer_service_client_id,
                p_issuer_credential_id,
                v_existing_token.binding_owner_service_client_id,
                v_existing_token.grant_id,
                v_existing_token.id, v_existing_token.tenant_id,
                v_existing_token.principal_id,
                v_existing_token.authority_class,
                v_existing_token.purpose_name,
                p_request_id, 'external_ref_conflict',
                sha256(convert_to(p_tenant_external_ref, 'UTF8')),
                sha256(convert_to(p_principal_external_ref, 'UTF8')),
                sha256(convert_to(p_delegation_external_ref, 'UTF8')),
                '{"disposition":"conflict"}'::jsonb
            );
            UPDATE service_client_credentials SET last_used_at = v_now
            WHERE id = p_issuer_credential_id;
            RETURN QUERY SELECT false, false, false, NULL::UUID,
                NULL::TIMESTAMPTZ, NULL::TIMESTAMPTZ,
                'DELEGATION_EXTERNAL_REF_CONFLICT'::TEXT;
            RETURN;
        END IF;
        INSERT INTO service_delegation_idempotency (
            issuer_service_client_id, key_digest, request_digest,
            delegation_token_id
        ) VALUES (
            v_issuer_id, p_idempotency_key_digest, v_canonical_request_digest,
            v_existing_token.id
        );
        INSERT INTO service_delegation_events (
            event_type, outcome, issuer_service_client_id,
            issuer_credential_id, binding_owner_service_client_id,
            grant_id, delegation_token_id, tenant_id, principal_id,
            authority_class, purpose_name, request_id, reason_code,
            external_tenant_ref_digest, external_principal_ref_digest,
            external_delegation_ref_digest, details
        ) VALUES (
            'delegation.resolved_existing', 'success', v_issuer_id,
            p_issuer_credential_id, v_owner.id, v_existing_token.grant_id,
            v_existing_token.id, v_existing_token.tenant_id,
            v_existing_token.principal_id, p_authority_class, p_purpose_name,
            p_request_id, 'resolved_existing',
            sha256(convert_to(p_tenant_external_ref, 'UTF8')),
            sha256(convert_to(p_principal_external_ref, 'UTF8')),
            sha256(convert_to(p_delegation_external_ref, 'UTF8')),
            jsonb_build_object(
                'ttl_seconds', p_ttl_seconds,
                'scope_read', p_authority_class = 'read',
                'single_use', true,
                'disposition', 'resolved_existing'
            )
        );
        UPDATE service_client_credentials SET last_used_at = v_now
        WHERE id = p_issuer_credential_id;
        RETURN QUERY SELECT false, false, false, v_existing_token.id,
            v_existing_token.issued_at, v_existing_token.expires_at, NULL::TEXT;
        RETURN;
    END IF;
    v_token_id := uuid_generate_v4();
    INSERT INTO service_delegation_tokens (
        id, grant_id, issuer_service_client_id, issuer_credential_id,
        binding_owner_service_client_id, tenant_binding_id,
        principal_binding_id, tenant_id, principal_id, authority_class,
        external_ref, key_id, secret_digest, digest_algorithm, scopes,
        audience, purpose_name, purpose_digest, target_item_id,
        target_review_status, status, issued_at, expires_at
    ) VALUES (
        v_token_id, v_grant.id, v_issuer_id, p_issuer_credential_id,
        v_owner.id, v_tenant_binding.id, v_principal_binding.id,
        v_tenant_binding.tenant_id, v_principal_binding.principal_id,
        p_authority_class, p_delegation_external_ref, p_key_id,
        p_secret_digest, 'sha256', v_scopes, 'engram-core',
        p_purpose_name, p_purpose_digest, p_target_item_id,
        p_target_review_status, 'active', v_now,
        v_now + make_interval(secs => p_ttl_seconds)
    );
    INSERT INTO service_delegation_idempotency (
        issuer_service_client_id, key_digest, request_digest,
        delegation_token_id
    ) VALUES (
        v_issuer_id, p_idempotency_key_digest, v_canonical_request_digest,
        v_token_id
    );
    INSERT INTO service_delegation_events (
        event_type, outcome, issuer_service_client_id,
        issuer_credential_id, binding_owner_service_client_id,
        grant_id, delegation_token_id, tenant_id, principal_id,
        authority_class, purpose_name, request_id, reason_code,
        external_tenant_ref_digest, external_principal_ref_digest,
        external_delegation_ref_digest, details
    ) VALUES (
        'delegation.issued', 'success', v_issuer_id,
        p_issuer_credential_id, v_owner.id, v_grant.id, v_token_id,
        v_tenant_binding.tenant_id, v_principal_binding.principal_id,
        p_authority_class, p_purpose_name, p_request_id, 'created',
        sha256(convert_to(p_tenant_external_ref, 'UTF8')),
        sha256(convert_to(p_principal_external_ref, 'UTF8')),
        sha256(convert_to(p_delegation_external_ref, 'UTF8')),
        jsonb_build_object(
            'ttl_seconds', p_ttl_seconds,
            'scope_read', p_authority_class = 'read',
            'single_use', true,
            'disposition', 'created'
        )
    );
    UPDATE service_client_credentials SET last_used_at = v_now
    WHERE id = p_issuer_credential_id;
    RETURN QUERY SELECT true, false, true, v_token_id, v_now,
        v_now + make_interval(secs => p_ttl_seconds), NULL::TEXT;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;
REVOKE ALL ON FUNCTION issue_service_delegation_by_class(
    TEXT, TEXT, UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT,
    INTEGER, TEXT, TEXT, BYTEA, UUID, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION issue_service_delegation(
    p_issuer_credential_id UUID,
    p_binding_owner_slug TEXT,
    p_tenant_external_ref TEXT,
    p_principal_external_ref TEXT,
    p_delegation_external_ref TEXT,
    p_idempotency_key_digest BYTEA,
    p_request_digest BYTEA,
    p_key_id TEXT,
    p_secret_digest TEXT,
    p_ttl_seconds INTEGER,
    p_request_id TEXT
) RETURNS TABLE (
    created BOOLEAN,
    idempotency_replayed BOOLEAN,
    credential_secret_available BOOLEAN,
    delegation_token_id UUID,
    issued_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    error_code TEXT
) AS $$
    SELECT * FROM issue_service_delegation_by_class(
        'read', 'delegation.issue', p_issuer_credential_id,
        p_binding_owner_slug, p_tenant_external_ref, p_principal_external_ref,
        p_delegation_external_ref, p_idempotency_key_digest, p_request_digest,
        p_key_id, p_secret_digest, p_ttl_seconds, p_request_id,
        NULL, NULL, NULL, NULL
    );
$$ LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp;

CREATE FUNCTION issue_service_review_delegation(
    p_issuer_credential_id UUID,
    p_binding_owner_slug TEXT,
    p_tenant_external_ref TEXT,
    p_principal_external_ref TEXT,
    p_delegation_external_ref TEXT,
    p_idempotency_key_digest BYTEA,
    p_request_digest BYTEA,
    p_key_id TEXT,
    p_secret_digest TEXT,
    p_ttl_seconds INTEGER,
    p_request_id TEXT,
    p_purpose_name TEXT,
    p_purpose_digest BYTEA,
    p_target_item_id UUID,
    p_target_review_status TEXT
) RETURNS TABLE (
    created BOOLEAN,
    idempotency_replayed BOOLEAN,
    credential_secret_available BOOLEAN,
    delegation_token_id UUID,
    issued_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    error_code TEXT
) AS $$
    SELECT * FROM issue_service_delegation_by_class(
        'review', 'delegation.review.issue', p_issuer_credential_id,
        p_binding_owner_slug, p_tenant_external_ref, p_principal_external_ref,
        p_delegation_external_ref, p_idempotency_key_digest, p_request_digest,
        p_key_id, p_secret_digest, p_ttl_seconds, p_request_id,
        p_purpose_name, p_purpose_digest, p_target_item_id,
        p_target_review_status
    );
$$ LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp;

CREATE FUNCTION revoke_service_delegation_by_class(
    p_authority_class TEXT,
    p_required_permission TEXT,
    p_issuer_credential_id UUID,
    p_binding_owner_slug TEXT,
    p_tenant_external_ref TEXT,
    p_principal_external_ref TEXT,
    p_delegation_external_ref TEXT,
    p_reason TEXT,
    p_request_id TEXT
) RETURNS TABLE (
    disposition TEXT,
    revoked BOOLEAN,
    error_code TEXT
) AS $$
DECLARE
    v_issuer_id UUID := current_service_client_id();
    v_token RECORD;
    v_token_id UUID;
    v_token_grant_id UUID;
    v_token_tenant_id UUID;
    v_token_principal_id UUID;
    v_token_purpose_name TEXT;
    v_authority RECORD;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_disposition TEXT;
BEGIN
    IF (p_authority_class, p_required_permission) NOT IN (
        ('read', 'delegation.issue'),
        ('review', 'delegation.review.issue')
    ) OR v_issuer_id IS NULL THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'SERVICE_UNAUTHORIZED'::TEXT;
        RETURN;
    END IF;
    IF p_reason NOT IN (
        'request_completed',
        'response_uncertain',
        'session_revoked',
        'membership_changed',
        'entitlement_changed',
        'operator_action',
        'credential_rotation'
    ) OR p_request_id !~ '^[!-~]{1,128}$'
    THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'DELEGATION_CONFLICT'::TEXT;
        RETURN;
    END IF;
    SELECT token.*
    INTO v_token
    FROM service_delegation_tokens AS token
    JOIN service_clients AS owner
      ON owner.id = token.binding_owner_service_client_id
    JOIN tenant_provisioning_bindings AS tenant_binding
      ON tenant_binding.id = token.tenant_binding_id
     AND tenant_binding.service_client_id = owner.id
     AND tenant_binding.tenant_id = token.tenant_id
    JOIN principal_provisioning_bindings AS principal_binding
      ON principal_binding.id = token.principal_binding_id
     AND principal_binding.service_client_id = owner.id
     AND principal_binding.tenant_binding_id = token.tenant_binding_id
     AND principal_binding.tenant_id = token.tenant_id
     AND principal_binding.principal_id = token.principal_id
    WHERE token.issuer_service_client_id = v_issuer_id
      AND token.authority_class = p_authority_class
      AND owner.slug = p_binding_owner_slug
      AND tenant_binding.external_ref = p_tenant_external_ref
      AND principal_binding.external_ref = p_principal_external_ref
      AND token.external_ref = p_delegation_external_ref;
    IF FOUND THEN
        v_token_id := v_token.id;
        SELECT * INTO v_token
        FROM service_delegation_tokens
        WHERE id = v_token_id
        FOR UPDATE;
        v_token_tenant_id := v_token.tenant_id;
        v_token_principal_id := v_token.principal_id;
        v_token_grant_id := v_token.grant_id;
        v_token_purpose_name := v_token.purpose_name;
    END IF;
    SELECT issuer.status AS issuer_status, issuer.permissions,
        credential.status AS credential_status,
        credential.expires_at AS credential_expires_at,
        credential.service_client_id AS credential_client_id,
        owner.id AS owner_id, owner.status AS owner_status,
        delegation_grant.id AS grant_id,
        delegation_grant.status AS grant_status
    INTO v_authority
    FROM service_clients AS issuer
    JOIN service_client_credentials AS credential
      ON credential.id = p_issuer_credential_id
    JOIN service_clients AS owner
      ON owner.slug = p_binding_owner_slug
    JOIN service_delegation_grants AS delegation_grant
      ON delegation_grant.issuer_service_client_id = issuer.id
     AND delegation_grant.binding_owner_service_client_id = owner.id
     AND delegation_grant.authority_class = p_authority_class
     AND (
         (v_token_id IS NOT NULL AND delegation_grant.id = v_token_grant_id)
         OR (v_token_id IS NULL AND delegation_grant.status = 'active')
     )
    WHERE issuer.id = v_issuer_id
    FOR UPDATE OF issuer, credential, owner, delegation_grant;
    IF NOT FOUND
       OR v_authority.issuer_status <> 'active'
       OR v_authority.credential_client_id <> v_issuer_id
       OR v_authority.credential_status <> 'active'
       OR (
           v_authority.credential_expires_at IS NOT NULL
           AND v_authority.credential_expires_at <= v_now
       )
    THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'SERVICE_UNAUTHORIZED'::TEXT;
        RETURN;
    END IF;
    IF NOT (p_required_permission = ANY(v_authority.permissions)) THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'SERVICE_FORBIDDEN'::TEXT;
        RETURN;
    END IF;
    IF v_authority.owner_status <> 'active'
       OR v_authority.grant_status <> 'active'
    THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'DELEGATION_GRANT_NOT_FOUND'::TEXT;
        RETURN;
    END IF;
    IF v_token_id IS NULL THEN
        v_disposition := 'not_found';
    ELSIF v_token.status = 'active' THEN
        UPDATE service_delegation_tokens
        SET status = 'revoked', revoked_at = v_now, revocation_reason = p_reason
        WHERE id = v_token_id;
        v_disposition := 'revoked';
    ELSIF v_token.status = 'used' THEN
        v_disposition := 'already_used';
    ELSE
        v_disposition := 'already_revoked';
    END IF;
    INSERT INTO service_delegation_events (
        event_type, outcome, issuer_service_client_id,
        issuer_credential_id, binding_owner_service_client_id,
        grant_id, delegation_token_id, tenant_id, principal_id,
        authority_class, purpose_name, request_id, reason_code,
        external_tenant_ref_digest, external_principal_ref_digest,
        external_delegation_ref_digest, details
    ) VALUES (
        CASE
            WHEN v_disposition = 'revoked' THEN 'delegation.revoked'
            ELSE 'delegation.resolved_existing'
        END,
        'success', v_issuer_id, p_issuer_credential_id,
        v_authority.owner_id, v_authority.grant_id, v_token_id,
        v_token_tenant_id, v_token_principal_id, p_authority_class,
        v_token_purpose_name, p_request_id, p_reason,
        sha256(convert_to(p_tenant_external_ref, 'UTF8')),
        sha256(convert_to(p_principal_external_ref, 'UTF8')),
        sha256(convert_to(p_delegation_external_ref, 'UTF8')),
        jsonb_build_object('disposition', v_disposition)
    );
    UPDATE service_client_credentials SET last_used_at = v_now
    WHERE id = p_issuer_credential_id;
    RETURN QUERY SELECT v_disposition, v_disposition = 'revoked', NULL::TEXT;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;
REVOKE ALL ON FUNCTION revoke_service_delegation_by_class(
    TEXT, TEXT, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION revoke_service_delegation(
    p_issuer_credential_id UUID,
    p_binding_owner_slug TEXT,
    p_tenant_external_ref TEXT,
    p_principal_external_ref TEXT,
    p_delegation_external_ref TEXT,
    p_reason TEXT,
    p_request_id TEXT
) RETURNS TABLE (
    disposition TEXT,
    revoked BOOLEAN,
    error_code TEXT
) AS $$
    SELECT * FROM revoke_service_delegation_by_class(
        'read', 'delegation.issue', p_issuer_credential_id,
        p_binding_owner_slug, p_tenant_external_ref, p_principal_external_ref,
        p_delegation_external_ref, p_reason, p_request_id
    );
$$ LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp;

CREATE FUNCTION revoke_service_review_delegation(
    p_issuer_credential_id UUID,
    p_binding_owner_slug TEXT,
    p_tenant_external_ref TEXT,
    p_principal_external_ref TEXT,
    p_delegation_external_ref TEXT,
    p_reason TEXT,
    p_request_id TEXT
) RETURNS TABLE (
    disposition TEXT,
    revoked BOOLEAN,
    error_code TEXT
) AS $$
    SELECT * FROM revoke_service_delegation_by_class(
        'review', 'delegation.review.issue', p_issuer_credential_id,
        p_binding_owner_slug, p_tenant_external_ref, p_principal_external_ref,
        p_delegation_external_ref, p_reason, p_request_id
    );
$$ LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp;

REVOKE ALL ON FUNCTION issue_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION issue_service_review_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT,
    TEXT, BYTEA, UUID, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION revoke_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION revoke_service_review_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION issue_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT
) TO engram_provisioner;
GRANT EXECUTE ON FUNCTION issue_service_review_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, BYTEA, BYTEA, TEXT, TEXT, INTEGER, TEXT,
    TEXT, BYTEA, UUID, TEXT
) TO engram_provisioner;
GRANT EXECUTE ON FUNCTION revoke_service_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) TO engram_provisioner;
GRANT EXECUTE ON FUNCTION revoke_service_review_delegation(
    UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) TO engram_provisioner;

REVOKE ALL ON
    service_delegation_grants,
    service_delegation_tokens,
    service_delegation_idempotency,
    service_delegation_events
FROM engram_app, engram_provisioner;
