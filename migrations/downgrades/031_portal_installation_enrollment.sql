-- Deterministic rollback for migration 031.
-- Refuse to orphan any Portal installation authority or evidence.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM portal_installation_enrollments)
       OR EXISTS (SELECT 1 FROM portal_installation_enrollment_clients)
       OR EXISTS (SELECT 1 FROM portal_installation_enrollment_events)
    THEN
        RAISE EXCEPTION
            'migration 031 downgrade requires empty portal enrollment authority and evidence'
            USING ERRCODE = '55000';
    END IF;
END
$$;

DROP FUNCTION enroll_portal_installation(
    BYTEA, UUID, BYTEA, BYTEA, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
);
DROP FUNCTION rotate_portal_installation_credentials(
    BYTEA, UUID, INTEGER, UUID, BYTEA, BYTEA,
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
);
DROP FUNCTION terminate_portal_installation_enrollment(UUID, TEXT, TEXT);
DROP TRIGGER trg_portal_enrollment_event_check
    ON portal_installation_enrollment_events;
DROP TRIGGER trg_portal_enrollment_grant_check ON service_delegation_grants;
DROP TRIGGER trg_portal_enrollment_credential_check ON service_client_credentials;
DROP TRIGGER trg_portal_enrollment_service_client_check ON service_clients;
DROP TRIGGER trg_portal_enrollment_client_check ON portal_installation_enrollment_clients;
DROP TRIGGER trg_portal_enrollment_self_check ON portal_installation_enrollments;
DROP FUNCTION enforce_portal_installation_enrollment();
DROP FUNCTION assert_portal_installation_enrollment(UUID);
DROP TRIGGER trg_portal_enrollment_clients_immutable
    ON portal_installation_enrollment_clients;
DROP TRIGGER trg_portal_enrollment_identity_immutable
    ON portal_installation_enrollments;
DROP FUNCTION reject_portal_enrollment_identity_mutation();
DROP TRIGGER trg_portal_enrollment_events_append_only
    ON portal_installation_enrollment_events;
DROP FUNCTION reject_portal_enrollment_event_mutation();
DROP TABLE portal_installation_enrollment_events;
DROP TABLE portal_installation_enrollment_clients;
DROP TABLE portal_installation_enrollments;
ALTER TABLE service_client_credentials
    DROP COLUMN portal_enrollment_revocation_reason;
