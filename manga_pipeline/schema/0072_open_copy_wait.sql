-- Schema step 0072 (SPEC "Library copies"): the open_copy wait (a download at junk waits while another open download of
-- its chapter is ahead of it).

ALTER TABLE chapter DROP CONSTRAINT chapter_waiting_on_check;
ALTER TABLE chapter ADD CONSTRAINT chapter_waiting_on_check
    CHECK (waiting_on IN (
        'suwayomi_download', 'bulk_conversion', 'junk_decision', 'kavita_scan', 'metadata_check', 'open_copy'
    ));
