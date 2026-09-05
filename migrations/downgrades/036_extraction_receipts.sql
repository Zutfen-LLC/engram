-- Disable structured extraction before downgrade. Memory items remain intact.
DROP TABLE IF EXISTS extraction_item_links;
DROP TABLE IF EXISTS extraction_runs;
DROP FUNCTION IF EXISTS extraction_link_integrity();
DROP FUNCTION IF EXISTS extraction_immutable();
