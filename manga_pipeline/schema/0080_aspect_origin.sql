-- Schema step 0080 (SPEC "State", Layout): where a series' page aspect came from, apart from its layout's origin
-- (series_conversion.origin, which an override file sets to override). aspect_origin record: the bulk converter's
-- record, taken at the baseline and never replaced; computed: the junk scan's, computed again from every live chapter;
-- NULL with no page aspect. Existing rows: record where the layout came from the record and an aspect is stored (every
-- such aspect is the record's: checked against the records on 2026-11-26), else computed where an aspect is stored.

ALTER TABLE series_conversion ADD COLUMN aspect_origin TEXT CHECK (aspect_origin IN ('record', 'computed'));
UPDATE series_conversion SET aspect_origin = CASE WHEN origin = 'record' THEN 'record' ELSE 'computed' END
WHERE page_aspect IS NOT NULL;
