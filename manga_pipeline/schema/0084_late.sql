-- Schema step 0084 (SPEC "Junk decisions", "Publishing"): the late listing's inputs (`junk late`).
--
-- chapter_edit keeps, from junk Done until export clears it past export.retention_days, the chapter's scan (as
-- junk_scan held it), the watermark keys its decide used and the time; null for edits made before this step.
-- published_file.junk_chapter names the chapter whose junk edit the file's pages carry; no foreign key (chapter ids
-- are never reused, and the id is copied into pending_copy and previous_values, which outlive a state reset).
ALTER TABLE chapter_edit
    ADD COLUMN scan TEXT CHECK (scan IS JSON OBJECT),
    ADD COLUMN scan_watermarks JSONB,
    ADD COLUMN scan_at TEXT;
CREATE INDEX chapter_edit_scan_at ON chapter_edit (scan_at) WHERE scan IS NOT NULL;
ALTER TABLE published_file ADD COLUMN junk_chapter BIGINT;
