-- Schema step 0053 (SPEC "Metadata", ComicInfo): the chapter's page at its Suwayomi source (Suwayomi's realUrl, NULL for
-- a hand-placed chapter or a source that gives none), written to ComicInfo Web; and the metadata_check wait (a chapter
-- whose series has never completed a metadata check waits at comicinfo).

ALTER TABLE chapter ADD COLUMN source_url TEXT;

ALTER TABLE chapter DROP CONSTRAINT chapter_waiting_on_check;
ALTER TABLE chapter ADD CONSTRAINT chapter_waiting_on_check
    CHECK (waiting_on IN ('suwayomi_download', 'bulk_conversion', 'junk_decision', 'kavita_scan', 'metadata_check'));
