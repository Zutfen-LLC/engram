-- Downgrade for 043_explicit_priority.sql (issue #196).

DROP TRIGGER IF EXISTS trg_memory_items_default_explicit_priority ON memory_items;
DROP FUNCTION IF EXISTS memory_items_default_explicit_priority();

ALTER TABLE memory_items
    DROP COLUMN IF EXISTS explicit_priority;
