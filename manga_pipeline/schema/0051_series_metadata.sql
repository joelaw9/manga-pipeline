-- Schema step 0051 (SPEC "Source ids", "Metadata checks"): series metadata. Knowledge: state reset keeps it.
-- series_source_id is replaced: nothing wrote it before this step, and an id is now kept per origin.
-- fetched: 1 row per source record a check stored; payload NULL = the source has no id for the series after its search.
-- series_check: data_version is 0 until the first completed check; urgent = made due by a new chapter.
-- series_import: what the old metadata tool's state file says it wrote (its field names and status, and its cover's
-- sha256), read when the pipeline first writes the series to Kavita.

DROP TABLE series_source_id;

CREATE TABLE series_source_id (
    series_id BIGINT NOT NULL REFERENCES series (id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('anilist', 'mangaupdates', 'mangadex', 'mangabaka', 'myanimelist')),
    origin TEXT NOT NULL
        CHECK (origin IN ('suwayomi', 'kavita', 'anilist', 'mangabaka', 'mangadex', 'search', 'import', 'human')),
    source_id TEXT NOT NULL,
    PRIMARY KEY (series_id, source, origin, source_id)
);
CREATE INDEX series_source_id_by_id ON series_source_id (source, source_id);

CREATE TABLE fetched (
    series_id BIGINT NOT NULL REFERENCES series (id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB,
    payload_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (series_id, source, kind)
);

CREATE TABLE series_check (
    series_id BIGINT PRIMARY KEY REFERENCES series (id) ON DELETE CASCADE,
    data_version BIGINT NOT NULL DEFAULT 0 CHECK (data_version >= 0),
    checked_at TEXT,
    next_check_at TEXT,
    urgent BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE series_cover (
    series_id BIGINT PRIMARY KEY REFERENCES series (id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL
);

CREATE TABLE series_import (
    series_id BIGINT PRIMARY KEY REFERENCES series (id) ON DELETE CASCADE,
    fields JSONB NOT NULL,
    status INTEGER,
    cover_sha256 TEXT
);
