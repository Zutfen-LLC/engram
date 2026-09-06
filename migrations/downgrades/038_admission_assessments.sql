-- Rollback for migration 038 (issue #159).
--
-- Rolling back #159 means disabling capture, not destroying decision history:
--
--   1. set ENGRAM_ADMISSION_ASSESSMENT_CAPTURE_ENABLED=false;
--   2. continue on the existing Path A mutation/audit behavior, which is
--      byte-identical with capture disabled;
--   3. keep admission_assessments and admission_assessment_current for
--      inspection.
--
-- Historical rows are never deleted or rewritten here, and item_events keeps
-- its nullable admission_assessment_id so already-linked audit rows stay
-- honest. Reapplying migration 038 is idempotent.
--
-- The one thing this downgrade does is refuse to run while a decision is
-- still in flight: a pending or running promotion.evaluate job may be about
-- to commit an assessment atomically with a promotion, and a schema change
-- underneath it would either lose that decision or fail the mutation closed.
DO $$
DECLARE
    inflight BIGINT;
BEGIN
    SELECT count(*) INTO inflight FROM jobs
    WHERE job_type = 'promotion.evaluate' AND status IN ('pending', 'running');
    IF inflight > 0 THEN
        RAISE EXCEPTION
            'refusing to downgrade 038: % pending/running promotion.evaluate job(s) '
            'may still commit an admission assessment; drain the queue first', inflight
            USING ERRCODE = '55006';
    END IF;
END $$;

SELECT 1;
