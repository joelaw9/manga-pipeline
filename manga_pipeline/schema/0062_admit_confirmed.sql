-- Schema step 0062 (SPEC "Series identification"): series.admit_confirmed, a person's word (`series new`) that a
-- held new series whose check had completed is a series of its own, so its admission no longer compares its ids with
-- other series' (its name still is). Written only by `series new`; state reset keeps it.
ALTER TABLE series ADD COLUMN admit_confirmed BOOLEAN NOT NULL DEFAULT false;
