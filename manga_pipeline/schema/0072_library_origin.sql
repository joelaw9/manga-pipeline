-- Schema step 0072 (SPEC "State"): origin library, a published file adopted from a library series with no
-- bulk-converter record (its own download, never re-encoded; not rewritten until it is converted).

ALTER TABLE published_file DROP CONSTRAINT published_file_origin_check;
ALTER TABLE published_file ADD CONSTRAINT published_file_origin_check
    CHECK (origin IN ('pipeline', 'bulk', 'library'));
