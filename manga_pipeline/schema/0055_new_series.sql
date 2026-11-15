-- Schema step 0055 (SPEC "Series identification"): new series.
-- series.alt_name: a hand-placed folder's ComicInfo Series when it differs from its title folder's name (searched,
-- and a name series.only may give); admit_name: the name a person gave with `series new --name`; admitted_at: when a
-- new series (origin 'new') got its name and folder. A new series with admitted_at NULL is not admitted: pending while
-- its status is ok, held while needs_human. Knowledge: state reset keeps them.

ALTER TABLE series ADD COLUMN alt_name TEXT;
ALTER TABLE series ADD COLUMN admit_name TEXT;
ALTER TABLE series ADD COLUMN admitted_at TEXT;

-- series_conversion.origin 'override': the layout a series' override file names (SPEC "State").
ALTER TABLE series_conversion DROP CONSTRAINT series_conversion_origin_check;
ALTER TABLE series_conversion ADD CONSTRAINT series_conversion_origin_check
    CHECK (origin IN ('record', 'computed', 'override'));
