-- Schema step 0054 (SPEC "Publishing", "Metadata" volume covers): what the naming resolver needs of a published file,
-- and the covers of covered volumes.
-- published_file.source_name: the source download's file stem (season markers are read from it); kind: main or side;
-- volume_source: where its volume came from (the record's spelling). NULL on rows written before this step: the
-- baseline is taken again for a series whose bulk rows lack them (validate), and a pipeline row keeps NULL.
-- series_volume_cover: 1 cover per covered volume of a series, fetched once from source_url; path is relative to
-- paths.covers ("volume/<sha256>.<ext>"), as volume_cover.path is. Knowledge: state reset keeps it.

ALTER TABLE published_file ADD COLUMN source_name TEXT;
ALTER TABLE published_file ADD COLUMN kind TEXT CHECK (kind IN ('main', 'side'));
ALTER TABLE published_file ADD COLUMN volume_source TEXT;
-- What comicinfo resolved for an open chapter (publish copies them to its published file): main or side, and where
-- its volume came from.
ALTER TABLE chapter ADD COLUMN naming_kind TEXT CHECK (naming_kind IN ('main', 'side'));
ALTER TABLE chapter ADD COLUMN naming_volume_source TEXT;

CREATE TABLE series_volume_cover (
    series_id BIGINT NOT NULL REFERENCES series (id) ON DELETE CASCADE,
    volume BIGINT NOT NULL,
    source_url TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (series_id, volume)
);
