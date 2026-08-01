-- Fixed, replay-safe Portal installation enrollment.
-- This is not a general service-client administration interface.

ALTER TABLE service_client_credentials
    ADD COLUMN portal_enrollment_revocation_reason VARCHAR(32)
        CHECK (portal_enrollment_revocation_reason IN (
            'credential_rotation', 'enrollment_terminated'
        ));

CREATE TABLE portal_installation_enrollments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    enrollment_secret_digest BYTEA NOT NULL UNIQUE
        CHECK (octet_length(enrollment_secret_digest) = 32),
    installation_external_ref UUID NOT NULL UNIQUE,
    idempotency_digest BYTEA NOT NULL CHECK (octet_length(idempotency_digest) = 32),
    request_digest BYTEA NOT NULL CHECK (octet_length(request_digest) = 32),
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'terminated')),
    credential_generation INTEGER NOT NULL DEFAULT 1
        CHECK (credential_generation >= 1),
    event_sequence INTEGER NOT NULL DEFAULT 1 CHECK (event_sequence >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    terminated_at TIMESTAMPTZ,
    termination_reason VARCHAR(32),
    CHECK (completed_at >= created_at AND completed_at <= created_at + INTERVAL '5 minutes'),
    CHECK (
        (status = 'active' AND terminated_at IS NULL AND termination_reason IS NULL)
        OR (
            status = 'terminated'
            AND terminated_at IS NOT NULL
            AND terminated_at >= completed_at
            AND termination_reason IN ('operator_action', 'security_incident', 'client_disabled')
        )
    )
);

CREATE TABLE portal_installation_enrollment_clients (
    enrollment_id UUID NOT NULL
        REFERENCES portal_installation_enrollments(id) ON DELETE RESTRICT,
    role VARCHAR(32) NOT NULL CHECK (role IN ('provisioner', 'read_broker', 'review_broker')),
    service_client_id UUID NOT NULL UNIQUE REFERENCES service_clients(id) ON DELETE RESTRICT,
    service_credential_id UUID NOT NULL UNIQUE
        REFERENCES service_client_credentials(id) ON DELETE RESTRICT,
    key_id VARCHAR(64) NOT NULL UNIQUE CHECK (key_id ~ '^[0-9A-Za-z]{22}$'),
    secret_digest CHAR(64) NOT NULL CHECK (secret_digest ~ '^[0-9a-f]{64}$'),
    credential_generation INTEGER NOT NULL DEFAULT 1 CHECK (credential_generation >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (enrollment_id, role)
);

CREATE TABLE portal_installation_enrollment_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    enrollment_id UUID NOT NULL
        REFERENCES portal_installation_enrollments(id) ON DELETE RESTRICT,
    event_sequence INTEGER NOT NULL CHECK (event_sequence >= 1),
    event_type VARCHAR(48) NOT NULL CHECK (event_type IN (
        'enrollment.completed',
        'enrollment.credentials_rotated',
        'enrollment.terminated'
    )),
    credential_generation INTEGER NOT NULL CHECK (credential_generation >= 1),
    rotation_external_ref UUID,
    idempotency_digest BYTEA,
    request_digest BYTEA,
    outcome VARCHAR(16) NOT NULL CHECK (outcome = 'success'),
    request_id VARCHAR(128) NOT NULL CHECK (request_id ~ '^[!-~]{1,128}$'),
    reason_code VARCHAR(32) NOT NULL CHECK (reason_code IN (
        'initial_enrollment', 'credential_rotation',
        'operator_action', 'security_incident', 'client_disabled'
    )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (enrollment_id, event_sequence),
    CHECK (idempotency_digest IS NULL OR octet_length(idempotency_digest) = 32),
    CHECK (request_digest IS NULL OR octet_length(request_digest) = 32),
    CHECK (
        (
            event_type = 'enrollment.completed'
            AND event_sequence = 1
            AND credential_generation = 1
            AND rotation_external_ref IS NULL
            AND idempotency_digest IS NULL
            AND request_digest IS NULL
            AND reason_code = 'initial_enrollment'
        )
        OR (
            event_type = 'enrollment.credentials_rotated'
            AND event_sequence = credential_generation
            AND rotation_external_ref IS NOT NULL
            AND idempotency_digest IS NOT NULL
            AND request_digest IS NOT NULL
            AND reason_code = 'credential_rotation'
        )
        OR (
            event_type = 'enrollment.terminated'
            AND rotation_external_ref IS NULL
            AND idempotency_digest IS NULL
            AND request_digest IS NULL
            AND reason_code IN ('operator_action', 'security_incident', 'client_disabled')
        )
    )
);
CREATE UNIQUE INDEX uq_portal_enrollment_rotation_external_ref
    ON portal_installation_enrollment_events(enrollment_id, rotation_external_ref)
    WHERE rotation_external_ref IS NOT NULL;
CREATE UNIQUE INDEX uq_portal_enrollment_rotation_idempotency
    ON portal_installation_enrollment_events(enrollment_id, idempotency_digest)
    WHERE idempotency_digest IS NOT NULL;

CREATE FUNCTION reject_portal_enrollment_event_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'portal enrollment events are append-only' USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;
CREATE TRIGGER trg_portal_enrollment_events_append_only
    BEFORE UPDATE OR DELETE ON portal_installation_enrollment_events
    FOR EACH ROW EXECUTE FUNCTION reject_portal_enrollment_event_mutation();
REVOKE ALL ON FUNCTION reject_portal_enrollment_event_mutation() FROM PUBLIC;

CREATE FUNCTION reject_portal_enrollment_identity_mutation() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'portal enrollment identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_TABLE_NAME = 'portal_installation_enrollments' THEN
        IF OLD.id <> NEW.id
           OR OLD.enrollment_secret_digest <> NEW.enrollment_secret_digest
           OR OLD.installation_external_ref <> NEW.installation_external_ref
           OR OLD.idempotency_digest <> NEW.idempotency_digest
           OR OLD.request_digest <> NEW.request_digest
           OR OLD.created_at <> NEW.created_at
           OR OLD.completed_at <> NEW.completed_at
           OR OLD.status = 'terminated' AND NEW.status <> 'terminated'
           OR NEW.credential_generation < OLD.credential_generation
           OR NEW.credential_generation > OLD.credential_generation + 1
           OR NEW.event_sequence < OLD.event_sequence
           OR NEW.event_sequence > OLD.event_sequence + 1
        THEN
            RAISE EXCEPTION 'portal enrollment identity is immutable' USING ERRCODE = '55000';
        END IF;
    ELSIF OLD.enrollment_id <> NEW.enrollment_id
       OR OLD.role <> NEW.role
       OR OLD.service_client_id <> NEW.service_client_id
       OR OLD.created_at <> NEW.created_at
       OR NEW.credential_generation < OLD.credential_generation
       OR NEW.credential_generation > OLD.credential_generation + 1
    THEN
        RAISE EXCEPTION 'portal enrollment client identity is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;
CREATE TRIGGER trg_portal_enrollment_identity_immutable
    BEFORE UPDATE OR DELETE ON portal_installation_enrollments
    FOR EACH ROW EXECUTE FUNCTION reject_portal_enrollment_identity_mutation();
CREATE TRIGGER trg_portal_enrollment_clients_immutable
    BEFORE UPDATE OR DELETE ON portal_installation_enrollment_clients
    FOR EACH ROW EXECUTE FUNCTION reject_portal_enrollment_identity_mutation();
REVOKE ALL ON FUNCTION reject_portal_enrollment_identity_mutation() FROM PUBLIC;

CREATE FUNCTION assert_portal_installation_enrollment(enrollment UUID) RETURNS VOID AS $$
DECLARE
    enrollment_row portal_installation_enrollments%ROWTYPE;
    provisioner_id UUID;
    read_broker_id UUID;
    review_broker_id UUID;
    invalid_count INTEGER;
BEGIN
    SELECT * INTO enrollment_row
    FROM portal_installation_enrollments
    WHERE id = enrollment;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT service_client_id INTO provisioner_id
    FROM portal_installation_enrollment_clients
    WHERE enrollment_id = enrollment AND role = 'provisioner';
    SELECT service_client_id INTO read_broker_id
    FROM portal_installation_enrollment_clients
    WHERE enrollment_id = enrollment AND role = 'read_broker';
    SELECT service_client_id INTO review_broker_id
    FROM portal_installation_enrollment_clients
    WHERE enrollment_id = enrollment AND role = 'review_broker';

    IF provisioner_id IS NULL OR read_broker_id IS NULL OR review_broker_id IS NULL OR (
        SELECT count(*) FROM portal_installation_enrollment_clients
        WHERE enrollment_id = enrollment
    ) <> 3 THEN
        RAISE EXCEPTION 'portal enrollment requires exactly three fixed clients'
            USING ERRCODE = '23514';
    END IF;

    IF (
        SELECT count(*) FROM portal_installation_enrollment_events
        WHERE enrollment_id = enrollment
    ) <> enrollment_row.event_sequence
       OR NOT EXISTS (
           SELECT 1 FROM portal_installation_enrollment_events
           WHERE enrollment_id = enrollment
             AND event_type = 'enrollment.completed'
             AND event_sequence = 1
             AND credential_generation = 1
       )
       OR (
           SELECT count(*) FROM portal_installation_enrollment_events
           WHERE enrollment_id = enrollment
             AND event_type = 'enrollment.credentials_rotated'
       ) <> enrollment_row.credential_generation - 1
       OR (
           enrollment_row.status = 'active'
           AND (
               enrollment_row.event_sequence <> enrollment_row.credential_generation
               OR EXISTS (
                   SELECT 1 FROM portal_installation_enrollment_events
                   WHERE enrollment_id = enrollment
                     AND event_type = 'enrollment.terminated'
               )
           )
       )
       OR (
           enrollment_row.status = 'terminated'
           AND (
               enrollment_row.event_sequence <> enrollment_row.credential_generation + 1
               OR (
                   SELECT count(*) FROM portal_installation_enrollment_events
                   WHERE enrollment_id = enrollment
                     AND event_type = 'enrollment.terminated'
                     AND event_sequence = enrollment_row.event_sequence
                     AND credential_generation = enrollment_row.credential_generation
                     AND reason_code = enrollment_row.termination_reason
               ) <> 1
           )
       )
    THEN
        RAISE EXCEPTION 'portal enrollment lifecycle evidence is not canonical'
            USING ERRCODE = '23514';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM portal_installation_enrollment_clients AS enrolled
    JOIN service_clients AS client ON client.id = enrolled.service_client_id
    JOIN service_client_credentials AS credential
      ON credential.id = enrolled.service_credential_id
    WHERE enrolled.enrollment_id = enrollment
      AND (
          credential.service_client_id <> client.id
          OR credential.key_id <> enrolled.key_id
          OR credential.secret_digest <> enrolled.secret_digest
          OR credential.digest_algorithm <> 'sha256'
          OR credential.expires_at IS NOT NULL
          OR enrolled.credential_generation <> enrollment_row.credential_generation
          OR client.slug <> 'portal-' || replace(enrollment_row.installation_external_ref::text, '-', '')
              || '-' || replace(enrolled.role, '_', '-')
          OR client.display_name <> CASE enrolled.role
              WHEN 'provisioner' THEN 'Portal provisioning owner'
              WHEN 'read_broker' THEN 'Portal read broker'
              ELSE 'Portal review broker'
          END
          OR client.permissions <> CASE enrolled.role
              WHEN 'provisioner' THEN ARRAY[
                  'tenant.provision', 'principal.provision', 'workspace.provision',
                  'agent.provision', 'api_key.provision'
              ]::TEXT[]
              WHEN 'read_broker' THEN ARRAY['delegation.issue']::TEXT[]
              ELSE ARRAY['delegation.review.issue']::TEXT[]
          END
          OR (
              enrollment_row.status = 'active'
              AND (
                  credential.status <> 'active'
                  OR credential.portal_enrollment_revocation_reason IS NOT NULL
                  OR client.status <> 'active'
                  OR (SELECT count(*) FROM service_client_credentials AS active_credential
                      WHERE active_credential.service_client_id = client.id
                        AND active_credential.status = 'active') <> 1
                  OR EXISTS (
                      SELECT 1 FROM service_client_credentials AS historical
                      WHERE historical.service_client_id = client.id
                        AND historical.id <> credential.id
                        AND (
                            historical.status <> 'revoked'
                            OR historical.portal_enrollment_revocation_reason
                               <> 'credential_rotation'
                        )
                  )
              )
          )
          OR (
              enrollment_row.status = 'terminated'
              AND (
                  credential.status <> 'revoked'
                  OR credential.portal_enrollment_revocation_reason
                     <> 'enrollment_terminated'
                  OR client.status <> 'disabled'
                  OR EXISTS (
                      SELECT 1 FROM service_client_credentials AS active_credential
                      WHERE active_credential.service_client_id = client.id
                        AND active_credential.status = 'active'
                  )
                  OR EXISTS (
                      SELECT 1 FROM service_client_credentials AS historical
                      WHERE historical.service_client_id = client.id
                        AND historical.id <> credential.id
                        AND (
                            historical.status <> 'revoked'
                            OR historical.portal_enrollment_revocation_reason
                               <> 'credential_rotation'
                        )
                  )
              )
          )
      );
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'portal enrollment client authority is not canonical'
            USING ERRCODE = '23514';
    END IF;

    IF (
        SELECT count(*)
        FROM service_delegation_grants
        WHERE issuer_service_client_id IN (provisioner_id, read_broker_id, review_broker_id)
           OR binding_owner_service_client_id IN (provisioner_id, read_broker_id, review_broker_id)
    ) <> 2 OR NOT EXISTS (
        SELECT 1 FROM service_delegation_grants
        WHERE issuer_service_client_id = read_broker_id
          AND binding_owner_service_client_id = provisioner_id
          AND authority_class = 'read' AND max_ttl_seconds = 60
          AND status = CASE WHEN enrollment_row.status = 'active' THEN 'active' ELSE 'revoked' END
          AND (
              enrollment_row.status = 'active' AND revocation_reason IS NULL
              OR enrollment_row.status = 'terminated'
                 AND revocation_reason = enrollment_row.termination_reason
          )
    ) OR NOT EXISTS (
        SELECT 1 FROM service_delegation_grants
        WHERE issuer_service_client_id = review_broker_id
          AND binding_owner_service_client_id = provisioner_id
          AND authority_class = 'review' AND max_ttl_seconds = 60
          AND status = CASE WHEN enrollment_row.status = 'active' THEN 'active' ELSE 'revoked' END
          AND (
              enrollment_row.status = 'active' AND revocation_reason IS NULL
              OR enrollment_row.status = 'terminated'
                 AND revocation_reason = enrollment_row.termination_reason
          )
    ) OR (
        enrollment_row.status = 'terminated' AND EXISTS (
            SELECT 1 FROM service_delegation_tokens
            WHERE status = 'active'
              AND (
                  issuer_service_client_id IN (provisioner_id, read_broker_id, review_broker_id)
                  OR binding_owner_service_client_id IN (
                      provisioner_id, read_broker_id, review_broker_id
                  )
              )
        )
    ) THEN
        RAISE EXCEPTION 'portal enrollment grants are not canonical' USING ERRCODE = '23514';
    END IF;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;
REVOKE ALL ON FUNCTION assert_portal_installation_enrollment(UUID) FROM PUBLIC;

CREATE FUNCTION enforce_portal_installation_enrollment() RETURNS TRIGGER AS $$
DECLARE
    enrollment UUID;
BEGIN
    IF TG_TABLE_NAME = 'portal_installation_enrollments' THEN
        enrollment := COALESCE(NEW.id, OLD.id);
    ELSIF TG_TABLE_NAME IN (
        'portal_installation_enrollment_clients',
        'portal_installation_enrollment_events'
    ) THEN
        enrollment := COALESCE(NEW.enrollment_id, OLD.enrollment_id);
    ELSIF TG_TABLE_NAME = 'service_clients' THEN
        SELECT enrollment_id INTO enrollment
        FROM portal_installation_enrollment_clients
        WHERE service_client_id = COALESCE(NEW.id, OLD.id);
    ELSIF TG_TABLE_NAME = 'service_client_credentials' THEN
        SELECT enrollment_id INTO enrollment
        FROM portal_installation_enrollment_clients
        WHERE service_client_id = COALESCE(NEW.service_client_id, OLD.service_client_id)
           OR service_credential_id = COALESCE(NEW.id, OLD.id)
        LIMIT 1;
    ELSE
        SELECT enrolled.enrollment_id INTO enrollment
        FROM portal_installation_enrollment_clients AS enrolled
        WHERE enrolled.service_client_id IN (
            COALESCE(NEW.issuer_service_client_id, OLD.issuer_service_client_id),
            COALESCE(NEW.binding_owner_service_client_id, OLD.binding_owner_service_client_id)
        )
        LIMIT 1;
    END IF;
    IF enrollment IS NOT NULL THEN
        PERFORM assert_portal_installation_enrollment(enrollment);
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp;
REVOKE ALL ON FUNCTION enforce_portal_installation_enrollment() FROM PUBLIC;

CREATE CONSTRAINT TRIGGER trg_portal_enrollment_self_check
    AFTER INSERT OR UPDATE ON portal_installation_enrollments
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION enforce_portal_installation_enrollment();
CREATE CONSTRAINT TRIGGER trg_portal_enrollment_client_check
    AFTER INSERT OR UPDATE OR DELETE ON portal_installation_enrollment_clients
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION enforce_portal_installation_enrollment();
CREATE CONSTRAINT TRIGGER trg_portal_enrollment_event_check
    AFTER INSERT ON portal_installation_enrollment_events
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION enforce_portal_installation_enrollment();
CREATE CONSTRAINT TRIGGER trg_portal_enrollment_service_client_check
    AFTER UPDATE OR DELETE ON service_clients
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION enforce_portal_installation_enrollment();
CREATE CONSTRAINT TRIGGER trg_portal_enrollment_credential_check
    AFTER INSERT OR UPDATE OR DELETE ON service_client_credentials
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION enforce_portal_installation_enrollment();
CREATE CONSTRAINT TRIGGER trg_portal_enrollment_grant_check
    AFTER INSERT OR UPDATE OR DELETE ON service_delegation_grants
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION enforce_portal_installation_enrollment();

CREATE FUNCTION enroll_portal_installation(
    pairing_digest BYTEA,
    installation_ref UUID,
    idempotency_key_digest BYTEA,
    canonical_request_digest BYTEA,
    provisioner_key_id TEXT,
    provisioner_secret_digest TEXT,
    read_broker_key_id TEXT,
    read_broker_secret_digest TEXT,
    review_broker_key_id TEXT,
    review_broker_secret_digest TEXT,
    enrollment_request_id TEXT
) RETURNS TABLE (
    enrollment_status TEXT,
    idempotency_replayed BOOLEAN,
    error_code TEXT
) AS $$
DECLARE
    existing portal_installation_enrollments%ROWTYPE;
    enrollment_id UUID := uuid_generate_v4();
    provisioner_id UUID := uuid_generate_v4();
    read_broker_id UUID := uuid_generate_v4();
    review_broker_id UUID := uuid_generate_v4();
    provisioner_credential_id UUID := uuid_generate_v4();
    read_broker_credential_id UUID := uuid_generate_v4();
    review_broker_credential_id UUID := uuid_generate_v4();
    installation_slug TEXT := replace(installation_ref::text, '-', '');
BEGIN
    IF to_regclass('service_clients') IS NULL
       OR to_regclass('service_client_credentials') IS NULL
       OR to_regclass('service_delegation_grants') IS NULL
       OR to_regprocedure(
           'issue_service_delegation(uuid,text,text,text,text,bytea,bytea,text,text,integer,text)'
       ) IS NULL
       OR to_regprocedure(
           'issue_service_review_delegation(uuid,text,text,text,text,bytea,bytea,text,text,integer,text,text,bytea,uuid,text)'
       ) IS NULL
       OR NOT EXISTS (
           SELECT 1 FROM pg_roles
           WHERE rolname = 'engram_provisioner'
             AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
             AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication
             AND NOT rolinherit
       )
    THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'FEATURES_NOT_READY'::TEXT;
        RETURN;
    END IF;
    IF octet_length(pairing_digest) <> 32
       OR octet_length(idempotency_key_digest) <> 32
       OR octet_length(canonical_request_digest) <> 32
       OR provisioner_key_id !~ '^[0-9A-Za-z]{22}$'
       OR read_broker_key_id !~ '^[0-9A-Za-z]{22}$'
       OR review_broker_key_id !~ '^[0-9A-Za-z]{22}$'
       OR provisioner_secret_digest !~ '^[0-9a-f]{64}$'
       OR read_broker_secret_digest !~ '^[0-9a-f]{64}$'
       OR review_broker_secret_digest !~ '^[0-9a-f]{64}$'
       OR enrollment_request_id !~ '^[!-~]{1,128}$'
       OR cardinality(ARRAY[provisioner_key_id, read_broker_key_id, review_broker_key_id])
          <> cardinality(ARRAY(
              SELECT DISTINCT value FROM unnest(ARRAY[
                  provisioner_key_id, read_broker_key_id, review_broker_key_id
              ]) AS value
          ))
       OR cardinality(ARRAY[
              provisioner_secret_digest, read_broker_secret_digest, review_broker_secret_digest
          ]) <> cardinality(ARRAY(
              SELECT DISTINCT value FROM unnest(ARRAY[
                  provisioner_secret_digest,
                  read_broker_secret_digest,
                  review_broker_secret_digest
              ]) AS value
          ))
    THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'INVALID_REQUEST'::TEXT;
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(encode(pairing_digest, 'hex'), 0));
    PERFORM pg_advisory_xact_lock(hashtextextended(installation_ref::text, 1));

    SELECT enrollment.* INTO existing
    FROM portal_installation_enrollments AS enrollment
    WHERE enrollment.enrollment_secret_digest = pairing_digest
       OR enrollment.installation_external_ref = installation_ref
    ORDER BY (enrollment.enrollment_secret_digest = pairing_digest) DESC
    LIMIT 1
    FOR UPDATE;
    IF FOUND THEN
        IF existing.status = 'terminated'
           AND existing.enrollment_secret_digest = pairing_digest
           AND existing.installation_external_ref = installation_ref
        THEN
            RETURN QUERY SELECT 'terminated'::TEXT, false, 'ENROLLMENT_TERMINATED'::TEXT;
        ELSIF existing.enrollment_secret_digest = pairing_digest
           AND existing.installation_external_ref = installation_ref
           AND existing.idempotency_digest = idempotency_key_digest
           AND existing.request_digest = canonical_request_digest
        THEN
            RETURN QUERY SELECT 'completed'::TEXT, true, NULL::TEXT;
        ELSE
            RETURN QUERY SELECT NULL::TEXT, false, 'ENROLLMENT_CONFLICT'::TEXT;
        END IF;
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1 FROM service_client_credentials
        WHERE key_id = ANY(ARRAY[
            provisioner_key_id, read_broker_key_id, review_broker_key_id
        ]) OR secret_digest = ANY(ARRAY[
            provisioner_secret_digest,
            read_broker_secret_digest,
            review_broker_secret_digest
        ])
    ) THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'ENROLLMENT_CONFLICT'::TEXT;
        RETURN;
    END IF;

    INSERT INTO service_clients (id, slug, display_name, permissions) VALUES
        (provisioner_id, 'portal-' || installation_slug || '-provisioner',
         'Portal provisioning owner', ARRAY[
             'tenant.provision', 'principal.provision', 'workspace.provision',
             'agent.provision', 'api_key.provision'
         ]::TEXT[]),
        (read_broker_id, 'portal-' || installation_slug || '-read-broker',
         'Portal read broker', ARRAY['delegation.issue']::TEXT[]),
        (review_broker_id, 'portal-' || installation_slug || '-review-broker',
         'Portal review broker', ARRAY['delegation.review.issue']::TEXT[]);

    INSERT INTO service_client_credentials (
        id, service_client_id, key_id, secret_digest, digest_algorithm, label
    ) VALUES
        (provisioner_credential_id, provisioner_id, provisioner_key_id,
         provisioner_secret_digest, 'sha256', 'Portal installation enrollment'),
        (read_broker_credential_id, read_broker_id, read_broker_key_id,
         read_broker_secret_digest, 'sha256', 'Portal installation enrollment'),
        (review_broker_credential_id, review_broker_id, review_broker_key_id,
         review_broker_secret_digest, 'sha256', 'Portal installation enrollment');

    INSERT INTO service_delegation_grants (
        issuer_service_client_id, binding_owner_service_client_id,
        authority_class, max_ttl_seconds
    ) VALUES
        (read_broker_id, provisioner_id, 'read', 60),
        (review_broker_id, provisioner_id, 'review', 60);

    INSERT INTO portal_installation_enrollments (
        id, enrollment_secret_digest, installation_external_ref,
        idempotency_digest, request_digest
    ) VALUES (
        enrollment_id, pairing_digest, installation_ref,
        idempotency_key_digest, canonical_request_digest
    );
    INSERT INTO portal_installation_enrollment_clients (
        enrollment_id, role, service_client_id, service_credential_id, key_id, secret_digest
    ) VALUES
        (enrollment_id, 'provisioner', provisioner_id, provisioner_credential_id,
         provisioner_key_id, provisioner_secret_digest),
        (enrollment_id, 'read_broker', read_broker_id, read_broker_credential_id,
         read_broker_key_id, read_broker_secret_digest),
        (enrollment_id, 'review_broker', review_broker_id, review_broker_credential_id,
         review_broker_key_id, review_broker_secret_digest);
    INSERT INTO portal_installation_enrollment_events (
        enrollment_id, event_sequence, event_type, credential_generation,
        outcome, request_id, reason_code
    ) VALUES (
        enrollment_id, 1, 'enrollment.completed', 1,
        'success', enrollment_request_id, 'initial_enrollment'
    );

    RETURN QUERY SELECT 'completed'::TEXT, false, NULL::TEXT;
EXCEPTION
    WHEN unique_violation THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'ENROLLMENT_CONFLICT'::TEXT;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;

CREATE FUNCTION rotate_portal_installation_credentials(
    pairing_digest BYTEA,
    installation_ref UUID,
    expected_generation INTEGER,
    rotation_ref UUID,
    idempotency_key_digest BYTEA,
    canonical_request_digest BYTEA,
    provisioner_key_id TEXT,
    provisioner_secret_digest TEXT,
    read_broker_key_id TEXT,
    read_broker_secret_digest TEXT,
    review_broker_key_id TEXT,
    review_broker_secret_digest TEXT,
    rotation_request_id TEXT
) RETURNS TABLE (
    enrollment_status TEXT,
    resulting_credential_generation INTEGER,
    idempotency_replayed BOOLEAN,
    error_code TEXT
) AS $$
DECLARE
    enrollment_row portal_installation_enrollments%ROWTYPE;
    replay_event portal_installation_enrollment_events%ROWTYPE;
    next_generation INTEGER;
    provisioner_credential_id UUID := uuid_generate_v4();
    read_broker_credential_id UUID := uuid_generate_v4();
    review_broker_credential_id UUID := uuid_generate_v4();
BEGIN
    IF expected_generation < 1
       OR octet_length(pairing_digest) <> 32
       OR octet_length(idempotency_key_digest) <> 32
       OR octet_length(canonical_request_digest) <> 32
       OR provisioner_key_id !~ '^[0-9A-Za-z]{22}$'
       OR read_broker_key_id !~ '^[0-9A-Za-z]{22}$'
       OR review_broker_key_id !~ '^[0-9A-Za-z]{22}$'
       OR provisioner_secret_digest !~ '^[0-9a-f]{64}$'
       OR read_broker_secret_digest !~ '^[0-9a-f]{64}$'
       OR review_broker_secret_digest !~ '^[0-9a-f]{64}$'
       OR rotation_request_id !~ '^[!-~]{1,128}$'
       OR cardinality(ARRAY[provisioner_key_id, read_broker_key_id, review_broker_key_id])
          <> cardinality(ARRAY(
              SELECT DISTINCT value FROM unnest(ARRAY[
                  provisioner_key_id, read_broker_key_id, review_broker_key_id
              ]) AS value
          ))
       OR cardinality(ARRAY[
              provisioner_secret_digest, read_broker_secret_digest, review_broker_secret_digest
          ]) <> cardinality(ARRAY(
              SELECT DISTINCT value FROM unnest(ARRAY[
                  provisioner_secret_digest,
                  read_broker_secret_digest,
                  review_broker_secret_digest
              ]) AS value
          ))
    THEN
        RETURN QUERY SELECT NULL::TEXT, NULL::INTEGER, false, 'INVALID_REQUEST'::TEXT;
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(encode(pairing_digest, 'hex'), 0));
    PERFORM pg_advisory_xact_lock(hashtextextended(installation_ref::text, 1));
    SELECT * INTO enrollment_row
    FROM portal_installation_enrollments
    WHERE enrollment_secret_digest = pairing_digest
      AND installation_external_ref = installation_ref
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::TEXT, NULL::INTEGER, false, 'ENROLLMENT_NOT_FOUND'::TEXT;
        RETURN;
    END IF;
    IF enrollment_row.status = 'terminated' THEN
        RETURN QUERY SELECT 'terminated'::TEXT, enrollment_row.credential_generation,
            false, 'ENROLLMENT_TERMINATED'::TEXT;
        RETURN;
    END IF;

    SELECT * INTO replay_event
    FROM portal_installation_enrollment_events
    WHERE enrollment_id = enrollment_row.id
      AND (rotation_external_ref = rotation_ref OR idempotency_digest = idempotency_key_digest)
    ORDER BY (rotation_external_ref = rotation_ref) DESC
    LIMIT 1;
    IF FOUND THEN
        IF replay_event.event_type = 'enrollment.credentials_rotated'
           AND replay_event.rotation_external_ref = rotation_ref
           AND replay_event.idempotency_digest = idempotency_key_digest
           AND replay_event.request_digest = canonical_request_digest
        THEN
            RETURN QUERY SELECT 'active'::TEXT, replay_event.credential_generation,
                true, NULL::TEXT;
        ELSE
            RETURN QUERY SELECT NULL::TEXT, enrollment_row.credential_generation,
                false, 'ROTATION_CONFLICT'::TEXT;
        END IF;
        RETURN;
    END IF;
    IF enrollment_row.credential_generation <> expected_generation THEN
        RETURN QUERY SELECT NULL::TEXT, enrollment_row.credential_generation,
            false, 'STALE_CREDENTIAL_GENERATION'::TEXT;
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM service_client_credentials
        WHERE key_id = ANY(ARRAY[
            provisioner_key_id, read_broker_key_id, review_broker_key_id
        ]) OR secret_digest = ANY(ARRAY[
            provisioner_secret_digest,
            read_broker_secret_digest,
            review_broker_secret_digest
        ])
    ) THEN
        RETURN QUERY SELECT NULL::TEXT, enrollment_row.credential_generation,
            false, 'ROTATION_CONFLICT'::TEXT;
        RETURN;
    END IF;

    next_generation := enrollment_row.credential_generation + 1;
    INSERT INTO service_client_credentials (
        id, service_client_id, key_id, secret_digest, digest_algorithm, label
    ) SELECT
        CASE enrolled.role
            WHEN 'provisioner' THEN provisioner_credential_id
            WHEN 'read_broker' THEN read_broker_credential_id
            ELSE review_broker_credential_id
        END,
        enrolled.service_client_id,
        CASE enrolled.role
            WHEN 'provisioner' THEN provisioner_key_id
            WHEN 'read_broker' THEN read_broker_key_id
            ELSE review_broker_key_id
        END,
        CASE enrolled.role
            WHEN 'provisioner' THEN provisioner_secret_digest
            WHEN 'read_broker' THEN read_broker_secret_digest
            ELSE review_broker_secret_digest
        END,
        'sha256',
        'Portal credential generation ' || next_generation::text
    FROM portal_installation_enrollment_clients AS enrolled
    WHERE enrolled.enrollment_id = enrollment_row.id
    ORDER BY enrolled.role;

    UPDATE service_client_credentials AS credential
    SET status = 'revoked', revoked_at = now(),
        portal_enrollment_revocation_reason = 'credential_rotation'
    FROM portal_installation_enrollment_clients AS enrolled
    WHERE enrolled.enrollment_id = enrollment_row.id
      AND credential.id = enrolled.service_credential_id
      AND credential.status = 'active';

    UPDATE portal_installation_enrollment_clients AS enrolled
    SET service_credential_id = CASE enrolled.role
            WHEN 'provisioner' THEN provisioner_credential_id
            WHEN 'read_broker' THEN read_broker_credential_id
            ELSE review_broker_credential_id
        END,
        key_id = CASE enrolled.role
            WHEN 'provisioner' THEN provisioner_key_id
            WHEN 'read_broker' THEN read_broker_key_id
            ELSE review_broker_key_id
        END,
        secret_digest = CASE enrolled.role
            WHEN 'provisioner' THEN provisioner_secret_digest
            WHEN 'read_broker' THEN read_broker_secret_digest
            ELSE review_broker_secret_digest
        END,
        credential_generation = next_generation
    WHERE enrolled.enrollment_id = enrollment_row.id;

    UPDATE portal_installation_enrollments
    SET credential_generation = next_generation,
        event_sequence = next_generation
    WHERE id = enrollment_row.id;

    INSERT INTO portal_installation_enrollment_events (
        enrollment_id, event_sequence, event_type, credential_generation,
        rotation_external_ref, idempotency_digest, request_digest,
        outcome, request_id, reason_code
    ) VALUES (
        enrollment_row.id, next_generation, 'enrollment.credentials_rotated',
        next_generation, rotation_ref, idempotency_key_digest, canonical_request_digest,
        'success', rotation_request_id, 'credential_rotation'
    );

    RETURN QUERY SELECT 'active'::TEXT, next_generation, false, NULL::TEXT;
EXCEPTION
    WHEN unique_violation THEN
        RETURN QUERY SELECT NULL::TEXT, NULL::INTEGER, false, 'ROTATION_CONFLICT'::TEXT;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;

CREATE FUNCTION terminate_portal_installation_enrollment(
    installation_ref UUID,
    termination_reason_code TEXT,
    termination_request_id TEXT
) RETURNS TABLE (
    enrollment_status TEXT,
    credential_generation INTEGER,
    terminated BOOLEAN,
    error_code TEXT
) AS $$
DECLARE
    enrollment_row portal_installation_enrollments%ROWTYPE;
BEGIN
    IF termination_reason_code NOT IN (
           'operator_action', 'security_incident', 'client_disabled'
       ) OR termination_request_id !~ '^[!-~]{1,128}$'
    THEN
        RETURN QUERY SELECT NULL::TEXT, NULL::INTEGER, false, 'INVALID_REQUEST'::TEXT;
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(installation_ref::text, 1));
    SELECT * INTO enrollment_row
    FROM portal_installation_enrollments
    WHERE installation_external_ref = installation_ref
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::TEXT, NULL::INTEGER, false, 'ENROLLMENT_NOT_FOUND'::TEXT;
        RETURN;
    END IF;
    PERFORM 1 FROM service_clients
    WHERE id IN (
        SELECT service_client_id FROM portal_installation_enrollment_clients
        WHERE enrollment_id = enrollment_row.id
    )
    ORDER BY id FOR UPDATE;
    IF enrollment_row.status = 'terminated' THEN
        RETURN QUERY SELECT 'terminated'::TEXT, enrollment_row.credential_generation,
            false, NULL::TEXT;
        RETURN;
    END IF;

    UPDATE service_delegation_tokens
    SET status = 'revoked', revoked_at = now(), revocation_reason = 'authority_invalidated'
    WHERE status = 'active'
      AND grant_id IN (
          SELECT id FROM service_delegation_grants
          WHERE issuer_service_client_id IN (
              SELECT service_client_id FROM portal_installation_enrollment_clients
              WHERE enrollment_id = enrollment_row.id
          ) OR binding_owner_service_client_id IN (
              SELECT service_client_id FROM portal_installation_enrollment_clients
              WHERE enrollment_id = enrollment_row.id
          )
      );

    UPDATE service_client_credentials
    SET status = 'revoked', revoked_at = COALESCE(revoked_at, now()),
        portal_enrollment_revocation_reason = 'enrollment_terminated'
    WHERE service_client_id IN (
        SELECT service_client_id FROM portal_installation_enrollment_clients
        WHERE enrollment_id = enrollment_row.id
    ) AND status = 'active';

    UPDATE service_delegation_grants
    SET status = 'revoked', revoked_at = now(), updated_at = now(),
        revocation_reason = termination_reason_code
    WHERE status = 'active'
      AND (
          issuer_service_client_id IN (
              SELECT service_client_id FROM portal_installation_enrollment_clients
              WHERE enrollment_id = enrollment_row.id
          ) OR binding_owner_service_client_id IN (
              SELECT service_client_id FROM portal_installation_enrollment_clients
              WHERE enrollment_id = enrollment_row.id
          )
      );

    UPDATE service_clients
    SET status = 'disabled', disabled_at = now(), updated_at = now()
    WHERE id IN (
        SELECT service_client_id FROM portal_installation_enrollment_clients
        WHERE enrollment_id = enrollment_row.id
    ) AND status = 'active';

    UPDATE portal_installation_enrollments
    SET status = 'terminated', terminated_at = now(),
        termination_reason = termination_reason_code,
        event_sequence = event_sequence + 1
    WHERE id = enrollment_row.id;

    INSERT INTO portal_installation_enrollment_events (
        enrollment_id, event_sequence, event_type, credential_generation,
        outcome, request_id, reason_code
    ) VALUES (
        enrollment_row.id, enrollment_row.event_sequence + 1,
        'enrollment.terminated', enrollment_row.credential_generation,
        'success', termination_request_id, termination_reason_code
    );

    RETURN QUERY SELECT 'terminated'::TEXT, enrollment_row.credential_generation,
        true, NULL::TEXT;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;

REVOKE ALL ON FUNCTION enroll_portal_installation(
    BYTEA, UUID, BYTEA, BYTEA, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION rotate_portal_installation_credentials(
    BYTEA, UUID, INTEGER, UUID, BYTEA, BYTEA,
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION terminate_portal_installation_enrollment(UUID, TEXT, TEXT)
    FROM PUBLIC;

REVOKE ALL ON portal_installation_enrollments,
    portal_installation_enrollment_clients,
    portal_installation_enrollment_events FROM PUBLIC;
