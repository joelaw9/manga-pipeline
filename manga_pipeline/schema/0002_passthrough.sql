-- Schema step 0002 (SPEC "Series identification", "State", "Publishing", "Suwayomi"): what Phase 3's passthrough
-- adds: the manga-to-series link, source ids, the series layout, the downloads not taken, and the series' Kavita
-- names, ComicInfo name, baseline and scan bookkeeping.

CREATE TABLE suwayomi_manga (
    manga_id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    series_id INTEGER NOT NULL REFERENCES series (id)
) STRICT;

CREATE TABLE series_source_id (
    series_id INTEGER NOT NULL REFERENCES series (id),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('import', 'found', 'human')),
    PRIMARY KEY (source, source_id)
) STRICT;

CREATE TABLE series_conversion (
    series_id INTEGER PRIMARY KEY REFERENCES series (id),
    layout TEXT NOT NULL CHECK (layout IN ('strip', 'pages')),
    page_aspect TEXT CHECK (page_aspect IS NULL OR json_valid(page_aspect)),
    origin TEXT NOT NULL CHECK (origin IN ('record', 'computed'))
) STRICT;

-- Workflow state: rewritten by every complete discovery listing; first_seen is carried over for a path still listed.
CREATE TABLE unmatched_download (
    path TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    first_seen TEXT NOT NULL
) STRICT;

ALTER TABLE series ADD COLUMN localized_name TEXT;
ALTER TABLE series ADD COLUMN original_name TEXT;
ALTER TABLE series ADD COLUMN comicinfo_series TEXT;
ALTER TABLE series ADD COLUMN baseline_at TEXT;
-- the record's series.naming.basis: where a new chapter's volume comes from (volumes.py)
ALTER TABLE series ADD COLUMN naming_basis TEXT;
ALTER TABLE series ADD COLUMN scan_due INTEGER NOT NULL DEFAULT 0 CHECK (scan_due IN (0, 1));
ALTER TABLE series ADD COLUMN scan_requested_at TEXT;

ALTER TABLE chapter ADD COLUMN content_digest TEXT;

CREATE INDEX handled_chapter_series_digest ON handled_chapter (series_id, content_digest);
CREATE INDEX chapter_download_path ON chapter (download_path);
