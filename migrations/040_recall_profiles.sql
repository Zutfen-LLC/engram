-- Recall admission profiles (issue #160 / ENG-RECALL-003).
--
-- Additive: recall_logs records the effective recall profile that produced
-- each packet, alongside the existing scoring_version. "legacy" is both the
-- default for new rows and the backfilled value for pre-profile rows (their
-- behavior was the legacy blend by definition). No served result changes
-- from this migration alone — governed/exploratory require an explicit
-- recall_profile request, and the default stays legacy until rollout.

ALTER TABLE recall_logs
    ADD COLUMN IF NOT EXISTS recall_profile TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE recall_logs
    DROP CONSTRAINT IF EXISTS recall_logs_recall_profile_check;
ALTER TABLE recall_logs
    ADD CONSTRAINT recall_logs_recall_profile_check CHECK (
        recall_profile IN ('legacy', 'governed', 'exploratory', 'startup')
    );
