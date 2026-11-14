-- Schema step 0010 (SPEC "Bad downloads", "Validation"): a chapter's re-download state and its validation notes.
-- Both are workflow state on the chapter row, so state reset clears them with it.

-- {defect, original: {source, manga_id, chapter_id}, rejected: [{source, chapter_id, digest, why}], tried: [source],
--  same_source_done, expected: {source, manga_id, chapter_id, path, enqueued_at} | null, chosen: chapter id | null,
--  remove: [{path, chapter_id, dequeue}]} (redownload.py)
ALTER TABLE chapter ADD COLUMN redownload TEXT CHECK (redownload IS NULL OR redownload IS JSON);
-- JSON list of short notes (duplicate pages, short chapter), shown by status
ALTER TABLE chapter ADD COLUMN notes TEXT CHECK (notes IS NULL OR notes IS JSON);
