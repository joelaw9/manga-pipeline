-- Schema step 0003 (SPEC "Parts", hand-off between parts): what 2 or more of the stage phases share.
--
-- chapter_edit: the junk stage's edit of a chapter, which convert applies. Workflow state, emptied by state reset
-- through the cascade from chapter. edit is a JSON object: {"drop_pages": [index...], "strip_removed": [{"start",
-- "end", "why"}...], "bands": [{"page", "box"}...], "join": [[index...]...], "split": [{"page", "parts"}...]}; strip
-- rows are of the stacked strip at its modal width.
CREATE TABLE chapter_edit (
    chapter_id INTEGER PRIMARY KEY REFERENCES chapter (id) ON DELETE CASCADE,
    edit TEXT NOT NULL CHECK (json_valid(edit) AND json_type(edit) = 'object')
) STRICT;

-- page.chroma holds images.chroma's text ("444", "420", "400", ...), not a number. No row held a value before this
-- step (nothing wrote page rows).
ALTER TABLE page DROP COLUMN chroma;
ALTER TABLE page ADD COLUMN chroma TEXT;
