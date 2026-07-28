-- Single-use, read-only delegated bearer authorization.
-- Extends migration 028 without changing any persisted service-client
-- permission or creating a broker, grant, token, or event automatically.

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

ALTER TABLE tenant_provisioning_bindings
    ADD CONSTRAINT uq_tenant_provisioning_binding_owner_identity
    UNIQUE (id, service_client_id, tenant_id);
ALTER TABLE principal_provisioning_bindings
    ADD CONSTRAINT uq_principal_provisioning_binding_owner_identity
    UNIQUE (id, service_client_id, tenant_binding_id, tenant_id);

CREATE TABLE service_delegation_grants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    issuer_service_client_id UUID NOT NULL
        REFERENCES service_clients(id) ON DELETE RESTRICT,
    binding_owner_service_client_id UUID NOT NULL
        REFERENCES service_clients(id) ON DELETE RESTRICT,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    max_ttl_seconds INTEGER NOT NULL CHECK (max_ttl_seconds BETWEEN 30 AND 300),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revocation_reason VARCHAR(64),
    CONSTRAINT chk_service_delegation_grant_distinct_clients
        CHECK (issuer_service_client_id <> binding_owner_service_client_id),
    CONSTRAINT chk_service_delegation_grant_revoked
        CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
    CONSTRAINT chk_service_delegation_grant_reason
        CHECK (
            (status = 'active' AND revocation_reason IS NULL)
            OR (
                status = 'revoked'
                AND revocation_reason IN (
                    'operator_action',
                    'security_incident',
                    'policy_changed',
                    'credential_rotation',
                    'client_disabled'
                )
            )
        ),
    UNIQUE (id, issuer_service_client_id, binding_owner_service_client_id)
);
CREATE UNIQUE INDEX uq_service_delegation_active_grant
    ON service_delegation_grants(
        issuer_service_client_id, binding_owner_service_client_id
    )
    WHERE status = 'active';
CREATE INDEX idx_service_delegation_grants_authority
    ON service_delegation_grants(
        issuer_service_client_id, binding_owner_service_client_id, status
    );

CREATE FUNCTION enforce_service_delegation_grant_history() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = 'revoked' AND NEW.status <> 'revoked' THEN
        RAISE EXCEPTION 'revoked delegation grants cannot be reactivated'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;
CREATE TRIGGER trg_service_delegation_grant_history
    BEFORE UPDATE ON service_delegation_grants
    FOR EACH ROW EXECUTE FUNCTION enforce_service_delegation_grant_history();
REVOKE ALL ON FUNCTION enforce_service_delegation_grant_history() FROM PUBLIC;

CREATE TABLE service_delegation_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    grant_id UUID NOT NULL,
    issuer_service_client_id UUID NOT NULL
        REFERENCES service_clients(id) ON DELETE RESTRICT,
    issuer_credential_id UUID NOT NULL
        REFERENCES service_client_credentials(id) ON DELETE RESTRICT,
    binding_owner_service_client_id UUID NOT NULL
        REFERENCES service_clients(id) ON DELETE RESTRICT,
    tenant_binding_id UUID NOT NULL,
    principal_binding_id UUID NOT NULL,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    principal_id UUID NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    external_ref VARCHAR(255) NOT NULL CHECK (external_ref ~ '^[!-~]{1,255}$'),
    key_id VARCHAR(64) NOT NULL UNIQUE CHECK (key_id ~ '^[0-9A-Za-z]{22}$'),
    secret_digest CHAR(64) NOT NULL CHECK (secret_digest ~ '^[0-9a-f]{64}$'),
    digest_algorithm VARCHAR(32) NOT NULL DEFAULT 'sha256'
        CHECK (digest_algorithm = 'sha256'),
    scopes TEXT[] NOT NULL DEFAULT ARRAY['read']::TEXT[]
        CHECK (scopes = ARRAY['read']::TEXT[]),
    audience VARCHAR(64) NOT NULL DEFAULT 'engram-core'
        CHECK (audience = 'engram-core'),
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'used', 'revoked')),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revocation_reason VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_service_delegation_token_grant
        FOREIGN KEY (
            grant_id, issuer_service_client_id, binding_owner_service_client_id
        )
        REFERENCES service_delegation_grants(
            id, issuer_service_client_id, binding_owner_service_client_id
        ) ON DELETE RESTRICT,
    CONSTRAINT fk_service_delegation_token_tenant_binding
        FOREIGN KEY (tenant_binding_id, binding_owner_service_client_id, tenant_id)
        REFERENCES tenant_provisioning_bindings(id, service_client_id, tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_service_delegation_token_principal_binding
        FOREIGN KEY (
            principal_binding_id,
            binding_owner_service_client_id,
            tenant_binding_id,
            tenant_id
        )
        REFERENCES principal_provisioning_bindings(
            id, service_client_id, tenant_binding_id, tenant_id
        ) ON DELETE RESTRICT,
    CONSTRAINT fk_service_delegation_token_principal_tenant
        FOREIGN KEY (tenant_id, principal_id)
        REFERENCES principals(tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT chk_service_delegation_token_expiry
        CHECK (
            expires_at > issued_at
            AND expires_at <= issued_at + INTERVAL '300 seconds'
        ),
    CONSTRAINT chk_service_delegation_token_state
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
                    'expired'
                )
            )
        ),
    UNIQUE (
        issuer_service_client_id,
        tenant_binding_id,
        principal_binding_id,
        external_ref
    )
);
CREATE INDEX idx_service_delegation_tokens_external_ref
    ON service_delegation_tokens(
        issuer_service_client_id,
        tenant_binding_id,
        principal_binding_id,
        external_ref
    );
CREATE INDEX idx_service_delegation_tokens_active_expiry
    ON service_delegation_tokens(expires_at) WHERE status = 'active';

CREATE TABLE service_delegation_idempotency (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    issuer_service_client_id UUID NOT NULL
        REFERENCES service_clients(id) ON DELETE RESTRICT,
    key_digest BYTEA NOT NULL CHECK (octet_length(key_digest) = 32),
    request_digest BYTEA NOT NULL CHECK (octet_length(request_digest) = 32),
    delegation_token_id UUID NOT NULL
        REFERENCES service_delegation_tokens(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (issuer_service_client_id, key_digest)
);

CREATE TABLE service_delegation_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type VARCHAR(64) NOT NULL CHECK (
        event_type IN (
            'delegation_grant.created',
            'delegation_grant.revoked',
            'delegation.issued',
            'delegation.resolved_existing',
            'delegation.idempotent_replay',
            'delegation.revoked',
            'delegation.used',
            'delegation.denied',
            'delegation.conflict'
        )
    ),
    outcome VARCHAR(16) NOT NULL CHECK (outcome IN ('success', 'failure')),
    issuer_service_client_id UUID
        REFERENCES service_clients(id) ON DELETE RESTRICT,
    issuer_credential_id UUID
        REFERENCES service_client_credentials(id) ON DELETE RESTRICT,
    binding_owner_service_client_id UUID
        REFERENCES service_clients(id) ON DELETE RESTRICT,
    grant_id UUID REFERENCES service_delegation_grants(id) ON DELETE RESTRICT,
    delegation_token_id UUID
        REFERENCES service_delegation_tokens(id) ON DELETE RESTRICT,
    tenant_id UUID REFERENCES tenants(id) ON DELETE RESTRICT,
    principal_id UUID REFERENCES principals(id) ON DELETE RESTRICT,
    request_id VARCHAR(128) NOT NULL CHECK (request_id ~ '^[!-~]{1,128}$'),
    reason_code VARCHAR(64) NOT NULL CHECK (reason_code ~ '^[a-z0-9_.-]{1,64}$'),
    external_tenant_ref_digest BYTEA
        CHECK (
            external_tenant_ref_digest IS NULL
            OR octet_length(external_tenant_ref_digest) = 32
        ),
    external_principal_ref_digest BYTEA
        CHECK (
            external_principal_ref_digest IS NULL
            OR octet_length(external_principal_ref_digest) = 32
        ),
    external_delegation_ref_digest BYTEA
        CHECK (
            external_delegation_ref_digest IS NULL
            OR octet_length(external_delegation_ref_digest) = 32
        ),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_service_delegation_event_details_object
        CHECK (jsonb_typeof(details) = 'object'),
    CONSTRAINT chk_service_delegation_event_detail_keys
        CHECK (
            details - ARRAY[
                'ttl_seconds', 'scope_read', 'single_use', 'disposition'
            ]::TEXT[] = '{}'::jsonb
        ),
    CONSTRAINT chk_service_delegation_event_disposition
        CHECK (
            NOT (details ? 'disposition')
            OR details ->> 'disposition' IN (
                'created',
                'replayed',
                'resolved_existing',
                'conflict',
                'revoked',
                'already_revoked',
                'already_used',
                'not_found',
                'denied'
            )
        )
);
CREATE INDEX idx_service_delegation_events_token_time
    ON service_delegation_events(delegation_token_id, created_at);
CREATE INDEX idx_service_delegation_events_issuer_time
    ON service_delegation_events(issuer_service_client_id, created_at);

CREATE FUNCTION reject_service_delegation_event_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'service delegation events are append-only'
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;
CREATE TRIGGER trg_service_delegation_events_append_only
    BEFORE UPDATE OR DELETE ON service_delegation_events
    FOR EACH ROW EXECUTE FUNCTION reject_service_delegation_event_mutation();
REVOKE ALL ON FUNCTION reject_service_delegation_event_mutation() FROM PUBLIC;

CREATE FUNCTION validate_service_delegation_token_subject() RETURNS TRIGGER AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM principals AS principal
        JOIN principal_provisioning_bindings AS binding
          ON binding.principal_id = principal.id
         AND binding.tenant_id = principal.tenant_id
        WHERE principal.id = NEW.principal_id
          AND principal.tenant_id = NEW.tenant_id
          AND principal.type = 'user'
          AND principal.internal_key IS NULL
          AND binding.id = NEW.principal_binding_id
          AND binding.service_client_id = NEW.binding_owner_service_client_id
          AND binding.tenant_binding_id = NEW.tenant_binding_id
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

CREATE FUNCTION issue_service_delegation(
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
BEGIN
    IF v_issuer_id IS NULL THEN
        RETURN QUERY SELECT false, false, false, NULL::UUID, NULL::TIMESTAMPTZ,
            NULL::TIMESTAMPTZ, 'SERVICE_UNAUTHORIZED'::TEXT;
        RETURN;
    END IF;

    SELECT
        client.id,
        client.status AS client_status,
        client.permissions,
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
    IF NOT ('delegation.issue' = ANY(v_issuer.permissions)) THEN
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
       OR p_ttl_seconds NOT BETWEEN 30 AND 300
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

    v_canonical_request_digest := sha256(
        convert_to(
            replace(
                replace(
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
                    )::TEXT,
                    ': ',
                    ':'
                ),
                ', ',
                ','
            ),
            'UTF8'
        )
    );

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            v_issuer_id::TEXT || ':delegation-idempotency:' ||
            encode(p_idempotency_key_digest, 'hex'),
            0
        )
    );
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            v_issuer_id::TEXT || ':delegation-external:' ||
            v_tenant_binding.id::TEXT || ':' || v_principal_binding.id::TEXT ||
            ':' || p_delegation_external_ref,
            0
        )
    );

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
                request_id, reason_code, external_tenant_ref_digest,
                external_principal_ref_digest, external_delegation_ref_digest,
                details
            ) VALUES (
                'delegation.conflict', 'failure', v_issuer_id,
                p_issuer_credential_id, v_owner.id, v_grant.id,
                v_existing_idem.id, v_existing_idem.tenant_id,
                v_existing_idem.principal_id, p_request_id,
                'idempotency_key_reused',
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
            request_id, reason_code, external_tenant_ref_digest,
            external_principal_ref_digest, external_delegation_ref_digest,
            details
        ) VALUES (
            'delegation.idempotent_replay', 'success', v_issuer_id,
            p_issuer_credential_id, v_existing_idem.binding_owner_service_client_id,
            v_existing_idem.grant_id, v_existing_idem.id,
            v_existing_idem.tenant_id, v_existing_idem.principal_id,
            p_request_id, 'replayed',
            sha256(convert_to(p_tenant_external_ref, 'UTF8')),
            sha256(convert_to(p_principal_external_ref, 'UTF8')),
            sha256(convert_to(p_delegation_external_ref, 'UTF8')),
            jsonb_build_object(
                'ttl_seconds',
                EXTRACT(EPOCH FROM (
                    v_existing_idem.expires_at - v_existing_idem.issued_at
                ))::INTEGER,
                'scope_read', true,
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
      AND external_ref = p_delegation_external_ref;
    IF FOUND THEN
        IF v_existing_token.grant_id <> v_grant.id
           OR v_existing_token.binding_owner_service_client_id <> v_owner.id
           OR EXTRACT(EPOCH FROM (
               v_existing_token.expires_at - v_existing_token.issued_at
           ))::INTEGER <> p_ttl_seconds
        THEN
            INSERT INTO service_delegation_events (
                event_type, outcome, issuer_service_client_id,
                issuer_credential_id, binding_owner_service_client_id,
                grant_id, delegation_token_id, tenant_id, principal_id,
                request_id, reason_code, external_tenant_ref_digest,
                external_principal_ref_digest, external_delegation_ref_digest,
                details
            ) VALUES (
                'delegation.conflict', 'failure', v_issuer_id,
                p_issuer_credential_id, v_owner.id, v_grant.id,
                v_existing_token.id, v_existing_token.tenant_id,
                v_existing_token.principal_id, p_request_id,
                'external_ref_conflict',
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
            request_id, reason_code, external_tenant_ref_digest,
            external_principal_ref_digest, external_delegation_ref_digest,
            details
        ) VALUES (
            'delegation.resolved_existing', 'success', v_issuer_id,
            p_issuer_credential_id, v_owner.id, v_existing_token.grant_id,
            v_existing_token.id, v_existing_token.tenant_id,
            v_existing_token.principal_id, p_request_id, 'resolved_existing',
            sha256(convert_to(p_tenant_external_ref, 'UTF8')),
            sha256(convert_to(p_principal_external_ref, 'UTF8')),
            sha256(convert_to(p_delegation_external_ref, 'UTF8')),
            jsonb_build_object(
                'ttl_seconds', p_ttl_seconds,
                'scope_read', true,
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
        principal_binding_id, tenant_id, principal_id, external_ref,
        key_id, secret_digest, digest_algorithm, scopes, audience, status,
        issued_at, expires_at
    ) VALUES (
        v_token_id, v_grant.id, v_issuer_id, p_issuer_credential_id,
        v_owner.id, v_tenant_binding.id, v_principal_binding.id,
        v_tenant_binding.tenant_id, v_principal_binding.principal_id,
        p_delegation_external_ref, p_key_id, p_secret_digest, 'sha256',
        ARRAY['read']::TEXT[], 'engram-core', 'active',
        v_now, v_now + make_interval(secs => p_ttl_seconds)
    );
    INSERT INTO service_delegation_idempotency (
        issuer_service_client_id, key_digest, request_digest,
        delegation_token_id
    ) VALUES (
        v_issuer_id, p_idempotency_key_digest, v_canonical_request_digest, v_token_id
    );
    INSERT INTO service_delegation_events (
        event_type, outcome, issuer_service_client_id,
        issuer_credential_id, binding_owner_service_client_id,
        grant_id, delegation_token_id, tenant_id, principal_id,
        request_id, reason_code, external_tenant_ref_digest,
        external_principal_ref_digest, external_delegation_ref_digest,
        details
    ) VALUES (
        'delegation.issued', 'success', v_issuer_id,
        p_issuer_credential_id, v_owner.id, v_grant.id, v_token_id,
        v_tenant_binding.tenant_id, v_principal_binding.principal_id,
        p_request_id, 'created',
        sha256(convert_to(p_tenant_external_ref, 'UTF8')),
        sha256(convert_to(p_principal_external_ref, 'UTF8')),
        sha256(convert_to(p_delegation_external_ref, 'UTF8')),
        jsonb_build_object(
            'ttl_seconds', p_ttl_seconds,
            'scope_read', true,
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

CREATE FUNCTION revoke_service_delegation(
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
    v_authority RECORD;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_disposition TEXT;
BEGIN
    IF v_issuer_id IS NULL THEN
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
    END IF;

    SELECT
        issuer.status AS issuer_status,
        issuer.permissions,
        credential.status AS credential_status,
        credential.expires_at AS credential_expires_at,
        credential.service_client_id AS credential_client_id,
        owner.id AS owner_id,
        owner.status AS owner_status,
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
    IF NOT ('delegation.issue' = ANY(v_authority.permissions)) THEN
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
        request_id, reason_code, external_tenant_ref_digest,
        external_principal_ref_digest, external_delegation_ref_digest,
        details
    ) VALUES (
        CASE
            WHEN v_disposition = 'revoked' THEN 'delegation.revoked'
            ELSE 'delegation.resolved_existing'
        END,
        'success', v_issuer_id, p_issuer_credential_id,
        v_authority.owner_id, v_authority.grant_id, v_token_id,
        v_token_tenant_id, v_token_principal_id, p_request_id, p_reason,
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

-- Re-converge exact direct grants. Delegation state is function-only for the
-- provisioner: it receives no table DML or SELECT privilege on these tables.
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

REVOKE ALL ON
    service_delegation_grants,
    service_delegation_tokens,
    service_delegation_idempotency,
    service_delegation_events
FROM engram_app, engram_provisioner;
