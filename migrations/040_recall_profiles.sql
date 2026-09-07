-- Recall admission profiles (issue #160 / ENG-RECALL-003).
--
-- Additive: recall_logs records the effective recall profile that produced
-- each packet, alongside the existing scoring_version. For historical rows
-- the profile is truthfully reconstructable from the mode they already
-- record: startup recall IS its own profile and always was, so pre-040
-- startup rows backfill 'startup' (exactly what new startup logs write);
-- pre-040 semantic rows were the legacy blend by definition and backfill
-- 'legacy'. No served result changes from this migration alone —
-- governed/exploratory are shadow-only until #162 certification, and the
-- default stays legacy until rollout.

ALTER TABLE recall_logs
    ADD COLUMN IF NOT EXISTS recall_profile TEXT NOT NULL DEFAULT 'legacy';

-- Reconstruct historical profiles. Also self-heals databases that applied
-- an earlier revision of this migration before the backfill existed (the
-- UPDATE is unconditional and idempotent).
UPDATE recall_logs SET recall_profile = 'startup' WHERE mode = 'startup';

ALTER TABLE recall_logs
    DROP CONSTRAINT IF EXISTS recall_logs_recall_profile_check;
ALTER TABLE recall_logs
    ADD CONSTRAINT recall_logs_recall_profile_check CHECK (
        recall_profile IN ('legacy', 'governed', 'exploratory', 'startup')
    );
