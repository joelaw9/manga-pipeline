-- Schema step 0075 (SPEC "Junk decisions", review): the junk refill record of a chapter's first decide after its
-- scan: when, how many candidates it waited on, and whether its series had no decision of its own then. NULL until
-- that decide; a new scan clears it. Chapters waiting on candidates now are recorded from their stored scans.
ALTER TABLE chapter ADD COLUMN junk_decided_at TEXT;
ALTER TABLE chapter ADD COLUMN junk_candidates BIGINT CHECK (junk_candidates >= 0);
ALTER TABLE chapter ADD COLUMN junk_new_series BOOLEAN;

UPDATE chapter c
SET junk_decided_at = s.scanned_at,
    junk_candidates = jsonb_array_length(s.candidates::jsonb),
    junk_new_series = NOT EXISTS (
        SELECT 1 FROM junk_decision d JOIN series r ON r.rules_key = d.series_key
        WHERE r.id = c.series_id AND d.decided_at < s.scanned_at
    )
FROM junk_scan s
WHERE s.chapter_id = c.id AND jsonb_array_length(s.candidates::jsonb) > 0;
