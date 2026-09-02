-- Deterministic rollback for migration 032.
-- The reconciliation cursor is derivable scheduling state (the next bounded
-- pass simply re-examines the head window after a downgrade), but refuse to
-- discard it silently while it still points at live progress.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM promotion_reconciliation_state) THEN
        RAISE EXCEPTION
            'migration 032 downgrade requires an empty promotion reconciliation cursor'
            USING ERRCODE = '55000';
    END IF;
END
$$;

DROP TABLE IF EXISTS promotion_reconciliation_state;
