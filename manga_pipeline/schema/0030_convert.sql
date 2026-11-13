-- Schema step 0030 (SPEC "Calibration"): the AVIF quality of each series and encode group.
--
-- series_calibration: knowledge, kept by state reset. A fixed group's q is final (origin record: the bulk-converter
-- record's probed group, taken at the baseline; computed: chosen from samples). An open group encodes at q 80 and
-- gathers samples: [{"sha1", "chapter_id", "rendered", "bytes_in", "scores": {"<q>": [score, bytes_out]}}].
CREATE TABLE series_calibration (
    series_id INTEGER NOT NULL REFERENCES series (id),
    grp TEXT NOT NULL CHECK (grp IN ('gray', 'colour')),
    q INTEGER NOT NULL CHECK (q BETWEEN 0 AND 100),
    fixed INTEGER NOT NULL CHECK (fixed IN (0, 1)),
    origin TEXT NOT NULL CHECK (origin IN ('record', 'computed')),
    why TEXT NOT NULL,
    samples TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(samples) AND json_type(samples) = 'array'),
    PRIMARY KEY (series_id, grp)
) STRICT;
