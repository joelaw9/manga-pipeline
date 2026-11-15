-- Phase 7e-1: retroactive volume and name rewrites (SPEC "Retroactive updates").
-- series_naming: the hash of the naming inputs a series was last compared with (a workflow table: `state reset`
-- empties it, so every series is compared again).
CREATE TABLE series_naming (
    series_id BIGINT PRIMARY KEY REFERENCES series (id) ON DELETE CASCADE,
    naming_hash TEXT NOT NULL,
    compared_at TEXT NOT NULL
);

-- A rewrite queued on its file's row (publish queues it, export finishes it step by step: pending_step is the last
-- step done), the reader's progress it carries, and the wait for Kavita to re-read the new file.
ALTER TABLE published_file ADD COLUMN pending_path TEXT;
ALTER TABLE published_file ADD COLUMN pending_staged TEXT;
ALTER TABLE published_file ADD COLUMN pending_size BIGINT;
ALTER TABLE published_file ADD COLUMN pending_volume TEXT;
ALTER TABLE published_file ADD COLUMN pending_volume_source TEXT;
ALTER TABLE published_file ADD COLUMN pending_step BIGINT;
ALTER TABLE published_file ADD COLUMN pending_error TEXT;
ALTER TABLE published_file ADD COLUMN pending_attempts BIGINT NOT NULL DEFAULT 0;
-- indexed_since: when the rewritten file was renamed in; set until Kavita has re-read it and the progress is restored.
ALTER TABLE published_file ADD COLUMN indexed_since TEXT;
ALTER TABLE published_file ADD COLUMN wait_charges BIGINT NOT NULL DEFAULT 0;
ALTER TABLE published_file ADD COLUMN carry_pages_read BIGINT;
ALTER TABLE published_file ADD COLUMN carry_old_pages BIGINT;
ALTER TABLE published_file ADD COLUMN carry_new_pages BIGINT;
ALTER TABLE published_file ADD COLUMN carry_page_shift BIGINT;
ALTER TABLE published_file ADD COLUMN carry_kavita_modified TEXT;
ALTER TABLE published_file ADD CONSTRAINT published_file_pending_whole
    CHECK ((pending_path IS NULL) = (pending_staged IS NULL) AND (pending_path IS NULL) = (pending_step IS NULL));
