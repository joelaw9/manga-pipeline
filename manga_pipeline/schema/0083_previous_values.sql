-- Schema step 0083 (SPEC "Publishing", "Retroactive updates"): the row of a file's previous version, written with
-- previous by a download chapter's replace or a restore: {previous, before, after}, the previous it belongs with, the
-- row of the version previous names, and the compared columns as that replace wrote them. `rewrite restore` reads it.
-- Rows written before it get null: their previous version cannot be restored.
ALTER TABLE published_file ADD COLUMN previous_values JSONB;
