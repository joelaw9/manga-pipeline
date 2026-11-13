-- Schema step 0031 (SPEC "Conversion"): the volume cover a chapter takes, handed from intake to convert.
--
-- volume_cover: a row = the chapter takes this cover as its page 000_cover; no row = none. Written by intake, read
-- by convert. Workflow state, emptied by state reset through the cascade from chapter. path: the cover file,
-- relative to paths.covers; sha256: its bytes'. volume: the volume it is the cover of.
CREATE TABLE volume_cover (
    chapter_id INTEGER PRIMARY KEY REFERENCES chapter (id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    volume TEXT NOT NULL
) STRICT;
