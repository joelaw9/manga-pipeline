-- Schema step 0001 (SPEC "State"): the step record, meta, and the tables the skeleton and its parts work on.
-- Times are ISO 8601 UTC text. JSON columns are text checked by json_valid.

CREATE TABLE schema_step (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

INSERT INTO meta (key, value) VALUES ('database_id', lower(hex(randomblob(16))));

CREATE TABLE series (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kavita_series_id INTEGER UNIQUE,
    kavita_library_id INTEGER,
    folder TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('kavita', 'new')),
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'needs_human')),
    status_reason TEXT
) STRICT;

CREATE TABLE published_file (
    id INTEGER PRIMARY KEY,
    library_path TEXT NOT NULL UNIQUE,
    series_id INTEGER NOT NULL REFERENCES series (id),
    identity TEXT NOT NULL,
    source TEXT,
    parse_fields TEXT CHECK (parse_fields IS NULL OR json_valid(parse_fields)),
    state TEXT NOT NULL CHECK (state IN ('unconfirmed', 'confirmed')),
    size INTEGER,
    median_page_width INTEGER,
    strip_length INTEGER,
    page_phashes TEXT CHECK (page_phashes IS NULL OR json_valid(page_phashes)),
    pages_in INTEGER,
    pages_out INTEGER,
    comicinfo_series TEXT,
    comicinfo_volume TEXT,
    comicinfo_number TEXT,
    comicinfo_title TEXT,
    comicinfo_date TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('pipeline', 'bulk'))
) STRICT;

-- AUTOINCREMENT: an id names files on the share and is kept in other tables, so it is never reused, not even after
-- state reset empties the table.
CREATE TABLE chapter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series (id),
    kind TEXT NOT NULL CHECK (kind IN ('download', 'rewrite')),
    identity TEXT NOT NULL,
    download_path TEXT,
    suwayomi_chapter_id INTEGER,
    source TEXT,
    stage TEXT NOT NULL
        CHECK (stage IN ('discover', 'validate', 'junk', 'convert', 'comicinfo', 'publish', 'confirm')),
    status TEXT NOT NULL CHECK (status IN ('ready', 'claimed', 'waiting', 'needs_human', 'done')),
    waiting_on TEXT
        CHECK (waiting_on IN ('suwayomi_download', 'bulk_conversion', 'junk_decision', 'kavita_scan')),
    wait_data TEXT CHECK (wait_data IS NULL OR json_valid(wait_data)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    claimed_by TEXT,
    staged_path TEXT,
    staged_size INTEGER,
    library_path TEXT,
    published_file_id INTEGER REFERENCES published_file (id),
    comicinfo_series TEXT,
    comicinfo_volume TEXT,
    comicinfo_number TEXT,
    comicinfo_title TEXT,
    comicinfo_date TEXT,
    convert_result TEXT,
    output_pages TEXT CHECK (output_pages IS NULL OR json_valid(output_pages)),
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((status = 'waiting') = (waiting_on IS NOT NULL)),
    CHECK ((status = 'claimed') = (claimed_by IS NOT NULL))
) STRICT;

-- At most 1 open chapter per published file (a rewrite, or a better copy replacing it).
CREATE UNIQUE INDEX chapter_open_per_published_file ON chapter (published_file_id)
    WHERE published_file_id IS NOT NULL AND status != 'done';

CREATE TABLE page (
    chapter_id INTEGER NOT NULL REFERENCES chapter (id) ON DELETE CASCADE,
    page_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    bytes INTEGER,
    sha1 TEXT,
    width INTEGER,
    height INTEGER,
    format TEXT,
    blank INTEGER CHECK (blank IN (0, 1)),
    phash TEXT,
    junk_signature TEXT,
    colour_class TEXT,
    chroma REAL,
    PRIMARY KEY (chapter_id, page_index)
) STRICT;

CREATE TABLE handled_chapter (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series (id),
    identity TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('published', 'rejected', 'duplicate', 'dropped')),
    content_digest TEXT,
    handled_at TEXT NOT NULL
) STRICT;

CREATE INDEX handled_chapter_series_identity ON handled_chapter (series_id, identity);
