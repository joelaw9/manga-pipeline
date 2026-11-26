-- Schema step 0080 (SPEC "State", Layout): where a series' page aspect came from, apart from its layout's origin
-- (series_conversion.origin, which an override file sets to override). aspect_origin record: the bulk converter's
-- record, taken at the baseline and never replaced; computed: the junk scan's, computed again from every live chapter;
-- NULL with no page aspect.
-- Existing rows: an aspect stored on a row whose layout came from the record is the record's (every one checked equal
-- to its record, 2026-11-26); one on a computed row is the junk scan's. A row whose layout an override set can hold
-- either, which only the record on the share tells: none held an aspect on 2026-11-26, and the step refuses the
-- database, naming the series, if one does now, rather than guess.
DO $$
DECLARE
    held TEXT;
BEGIN
    SELECT string_agg(series_id::text, ', ' ORDER BY series_id) INTO held
    FROM series_conversion WHERE origin = 'override' AND page_aspect IS NOT NULL;
    IF held IS NOT NULL THEN
        RAISE EXCEPTION 'series % hold a page aspect under an override layout: mark each aspect_origin by its record',
            held;
    END IF;
END
$$;

ALTER TABLE series_conversion ADD COLUMN aspect_origin TEXT CHECK (aspect_origin IN ('record', 'computed'));
UPDATE series_conversion SET aspect_origin = CASE WHEN origin = 'record' THEN 'record' ELSE 'computed' END
WHERE page_aspect IS NOT NULL;
