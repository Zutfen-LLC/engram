-- Fixed, replay-safe Portal installation enrollment.
-- This is not a general service-client administration interface.

CREATE TABLE portal_installation_enrollments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    enrollment_secret_digest BYTEA NOT NULL UNIQUE
        CHECK (octet_length(enrollment_secret_digest) = 32),
    installation_external_ref UUID NOT NULL UNIQUE,
    idempotency_digest BYTEA NOT NULL CHECK (octet_length(idempotency_digest) = 32),
    request_digest BYTEA NOT NULL CHECK (octet_length(request_digest) = 32),
    status VARCHAR(16) NOT NULL DEFAULT 'completed' CHECK (status = 'completed'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (completed_at >= created_at AND completed_at <= created_at + INTERVAL '5 minutes')
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (enrollment_id, role)
);

CREATE TABLE portal_installation_enrollment_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    enrollment_id UUID NOT NULL
        REFERENCES portal_installation_enrollments(id) ON DELETE RESTRICT,
    event_type VARCHAR(32) NOT NULL CHECK (event_type = 'enrollment.completed'),
    outcome VARCHAR(16) NOT NULL CHECK (outcome = 'success'),
    request_id VARCHAR(128) NOT NULL CHECK (request_id ~ '^[!-~]{1,128}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
    RAISE EXCEPTION 'portal enrollment identity is immutable' USING ERRCODE = '55000';
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
    provisioner_id UUID;
    read_broker_id UUID;
    review_broker_id UUID;
    invalid_count INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM portal_installation_enrollments WHERE id = enrollment) THEN
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
    ) <> 1 THEN
        RAISE EXCEPTION 'portal enrollment requires exactly one completion event'
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
          OR credential.status <> 'active'
          OR credential.expires_at IS NOT NULL
          OR client.status <> 'active'
          OR client.slug <> 'portal-' || replace((
              SELECT installation_external_ref::text
              FROM portal_installation_enrollments WHERE id = enrollment
          ), '-', '') || '-' || replace(enrolled.role, '_', '-')
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
          OR (SELECT count(*) FROM service_client_credentials AS all_credentials
              WHERE all_credentials.service_client_id = client.id) <> 1
      );
    IF invalid_count <> 0 THEN
        RAISE EXCEPTION 'portal enrollment client authority is not canonical'
            USING ERRCODE = '23514';
    END IF;

    IF (
        SELECT count(*)
        FROM service_delegation_grants
        WHERE issuer_service_client_id IN (
                  provisioner_id, read_broker_id, review_broker_id
              )
           OR binding_owner_service_client_id IN (
               provisioner_id, read_broker_id, review_broker_id
           )
    ) <> 2 OR NOT EXISTS (
        SELECT 1 FROM service_delegation_grants
        WHERE issuer_service_client_id = read_broker_id
          AND binding_owner_service_client_id = provisioner_id
          AND authority_class = 'read' AND status = 'active' AND max_ttl_seconds = 60
    ) OR NOT EXISTS (
        SELECT 1 FROM service_delegation_grants
        WHERE issuer_service_client_id = review_broker_id
          AND binding_owner_service_client_id = provisioner_id
          AND authority_class = 'review' AND status = 'active' AND max_ttl_seconds = 60
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
    THEN
        RETURN QUERY SELECT NULL::TEXT, false, 'INVALID_REQUEST'::TEXT;
        RETURN;
    END IF;

    -- Serialize every use of the configured enrollment secret. This prevents
    -- concurrent conflicting calls from creating partial or duplicate authority.
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
        IF existing.enrollment_secret_digest = pairing_digest
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
        enrollment_id, event_type, outcome, request_id
    ) VALUES (enrollment_id, 'enrollment.completed', 'success', enrollment_request_id);

    RETURN QUERY SELECT 'completed'::TEXT, false, NULL::TEXT;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;

REVOKE ALL ON FUNCTION enroll_portal_installation(
    BYTEA, UUID, BYTEA, BYTEA, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;

REVOKE ALL ON portal_installation_enrollments,
    portal_installation_enrollment_clients,
    portal_installation_enrollment_events FROM PUBLIC;
