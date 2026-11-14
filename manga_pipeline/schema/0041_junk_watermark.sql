-- Schema step 0041 (SPEC "Junk decisions", strip tail): the decision keys a series' chapters have shown to be
-- watermarks (a tail band that repeats, or matches a middle block), not credits. Knowledge: `state reset` keeps it.

CREATE TABLE junk_watermark (
    series_key TEXT NOT NULL,
    key TEXT NOT NULL
);

CREATE UNIQUE INDEX junk_watermark_unit ON junk_watermark (series_key, md5(key));
