-- Downgrade only removes the additive recall-log field. Existing semantic
-- receipts prevent a safe constraint narrowing and must be retained.
ALTER TABLE recall_logs DROP COLUMN IF EXISTS item_budget;
