-- Schema step 0082 (SPEC "Publishing", "Retroactive updates"): where a published file's original download and the
-- version a download chapter's replace took its place of are kept in retention (stored paths). pending_previous holds
-- the replaced version from finish's retention act until the replace settles, cleared with the other pending columns.
-- Rows written before it get null; scripts/record_originals.py fills original for the confirmed pipeline files.
ALTER TABLE published_file ADD COLUMN original TEXT;
ALTER TABLE published_file ADD COLUMN previous TEXT;
ALTER TABLE published_file ADD COLUMN pending_previous TEXT;
