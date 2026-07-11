-- Dedicated tenant-configurable scoring defaults for session-end captures.
ALTER TABLE tenant_config
    ADD COLUMN trust_session_end REAL NOT NULL DEFAULT 0.35,
    ADD COLUMN confidence_session_end REAL NOT NULL DEFAULT 0.35,
    ADD CONSTRAINT chk_tenant_config_trust_session_end_range
        CHECK (trust_session_end >= 0.0 AND trust_session_end <= 1.0),
    ADD CONSTRAINT chk_tenant_config_confidence_session_end_range
        CHECK (confidence_session_end >= 0.0 AND confidence_session_end <= 1.0);
