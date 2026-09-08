-- Candidate explicit-priority baseline (issue #196).
--
-- Historical ``importance`` includes legacy feedback deltas and may have
-- clamped values, so it cannot be safely decomposed. This migration takes a
-- truthful one-time baseline snapshot only; new writes maintain the field.

ALTER TABLE memory_items
    ADD COLUMN IF NOT EXISTS explicit_priority REAL;

UPDATE memory_items
SET explicit_priority = importance
WHERE explicit_priority IS NULL;

-- A direct internal ORM/SQL creator that omits the new additive column still
-- receives the same caller-selected baseline as legacy importance. Feedback
-- uses UPDATE and therefore remains deliberately outside this insert rule.
CREATE OR REPLACE FUNCTION memory_items_default_explicit_priority() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.explicit_priority IS NULL THEN
        NEW.explicit_priority := NEW.importance;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_memory_items_default_explicit_priority ON memory_items;
CREATE TRIGGER trg_memory_items_default_explicit_priority
    BEFORE INSERT ON memory_items
    FOR EACH ROW EXECUTE FUNCTION memory_items_default_explicit_priority();
