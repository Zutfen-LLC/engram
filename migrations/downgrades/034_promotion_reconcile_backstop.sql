-- Deterministic rollback for migration 034.
-- promotion_reconcile_state is scheduler-only bookkeeping: no job payload
-- references it, so dropping it never strips authority from live queue work.
-- It DOES lose the backstop's rotation position and last-pass diagnostics —
-- after a re-upgrade the first pass simply reads from the head (cursor NULL)
-- and rebuilds coverage. Refuse to discard the position while reconciliation
-- work is still live in the queue, so a downgrade cannot strand a
-- mid-rotation chain whose continuation depends on that position for
-- bounded, non-overlapping progress. Dead/succeeded history is inspectable
-- through the jobs table alone and does not block the downgrade.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM jobs
        WHERE job_type = 'promotion.reconcile'
          AND status IN ('pending', 'running')
    ) THEN
        RAISE EXCEPTION
            'migration 034 downgrade requires no pending/running promotion.reconcile jobs'
            USING ERRCODE = '55006';
    END IF;
END
$$;

DROP TRIGGER IF EXISTS trg_promotion_reconcile_item_state ON memory_items;
DROP TRIGGER IF EXISTS trg_promotion_reconcile_classification ON classification_runs;
DROP TRIGGER IF EXISTS trg_promotion_reconcile_feedback ON feedback_events;
DROP TRIGGER IF EXISTS trg_promotion_reconcile_item_event ON item_events;
DROP FUNCTION IF EXISTS invalidate_promotion_reconcile_terminal_item();
DROP FUNCTION IF EXISTS invalidate_promotion_reconcile_terminal();
DROP TABLE IF EXISTS promotion_reconcile_terminal;
DROP TABLE IF EXISTS promotion_reconcile_chains;
DROP TABLE IF EXISTS promotion_reconcile_scheduler_state;
DROP TABLE IF EXISTS promotion_reconcile_state;
DROP INDEX IF EXISTS idx_jobs_reconcile_item_state;
DROP INDEX IF EXISTS idx_memitems_proposed_rotation;
