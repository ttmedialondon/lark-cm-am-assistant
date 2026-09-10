-- Keep source names separate from the UI label. This first seed predates any
-- application-generated composite names; never parse a formatted label later.
ALTER TABLE lark_identities ADD COLUMN name TEXT, ADD COLUMN en_name TEXT;
UPDATE lark_identities SET name=display_name
WHERE display_name IS NOT NULL AND length(btrim(display_name))>0;

COMMENT ON COLUMN lark_identities.name IS 'Original Lark name, not a composite UI label';
COMMENT ON COLUMN lark_identities.en_name IS 'Original Lark en_name when actually returned';
