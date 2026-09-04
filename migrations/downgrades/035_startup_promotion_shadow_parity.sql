-- Shadow parity is non-authoritative diagnostics only.  No lifecycle work,
-- queue contract, or rollback path depends on it, so removal is always safe.
-- Removal includes compatibility coverage counters and the diagnostic cursor.
DROP TABLE IF EXISTS promotion_startup_shadow_state;
