-- Downgrade for 040_recall_profiles.sql (issue #160).
--
-- Dropping the audit column does not affect any served behavior (the default
-- profile remains legacy); it only removes the ability to attribute past
-- packets to a profile.

ALTER TABLE recall_logs
    DROP CONSTRAINT IF EXISTS recall_logs_recall_profile_check;
ALTER TABLE recall_logs
    DROP COLUMN IF EXISTS recall_profile;
