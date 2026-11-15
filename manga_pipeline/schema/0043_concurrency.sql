-- Schema step 0043 (SPEC "State", "Chapter stages", "Junk decisions"): process on several machines.
-- chapter.claim_count: rises with every claim; a claim is its owner (claimed_by) and this count.
-- chapter.retry_after: a failed chapter is claimed by no machine before this time (the server's clock, ISO text).
-- junk_candidate.closed_at: when junk review's clean-up first found the candidate not open; NULL while open.

ALTER TABLE chapter ADD COLUMN claim_count BIGINT NOT NULL DEFAULT 0 CHECK (claim_count >= 0);
ALTER TABLE chapter ADD COLUMN retry_after TEXT;
ALTER TABLE junk_candidate ADD COLUMN closed_at TEXT;

-- A database holding paths an older version stored (absolute, not <share root>/<path>) is refused (SPEC "State"):
-- the new code would take its downloads again and delete its staged files. Build a new database.
DO $$
DECLARE
    form CONSTANT text := '^(downloads|library|staging|retention|candidates|covers)(/|$)';
    old bigint;
BEGIN
    SELECT count(*) INTO old FROM (
        SELECT download_path AS p FROM chapter
        UNION ALL SELECT staged_path FROM chapter
        UNION ALL SELECT library_path FROM chapter
        UNION ALL SELECT redownload::jsonb #>> '{expected,path}' FROM chapter
        UNION ALL SELECT folder FROM series
        UNION ALL SELECT library_path FROM published_file
        UNION ALL SELECT path FROM unmatched_download
    ) paths WHERE p IS NOT NULL AND p !~ form;
    IF old > 0 THEN
        RAISE EXCEPTION 'this database holds % path(s) stored by an older version (absolute, not <share root>/<path>): build a new database', old;
    END IF;
END $$;
