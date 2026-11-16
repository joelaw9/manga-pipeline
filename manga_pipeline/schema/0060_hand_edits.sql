-- Phase 7g-1: hand edits of a series' chapters (SPEC "Retroactive updates", hand edits), written only by
-- `series edit`, kept by `state reset`. A chapter as a person names it: its season (NULL: none), whether it is a side
-- story, and its number (names.fmt_num; a side story's own side number). title / volume NULL: that field not edited.
CREATE TABLE naming_edit (
    series_id INTEGER NOT NULL REFERENCES series (id) ON DELETE CASCADE,
    season INTEGER,
    side BOOLEAN NOT NULL,
    number TEXT NOT NULL,
    title TEXT,
    volume INTEGER,
    edited_at TEXT NOT NULL,
    UNIQUE NULLS NOT DISTINCT (series_id, season, side, number)
);
-- The values a published file's sources gave the fields an edit changed ({"title": …} and/or {"volume": …,
-- "volume_source": …}); NULL when no edit changed the file. A removed edit restores them.
ALTER TABLE published_file ADD COLUMN unedited JSONB;
-- A queued rewrite's title and unedited values (with the other pending columns).
ALTER TABLE published_file ADD COLUMN pending_title TEXT;
ALTER TABLE published_file ADD COLUMN pending_unedited JSONB;
-- A chapter's unedited values as comicinfo named it (an open chapter named already edited), or as a rewrite chapter
-- will leave its file; publish carries them to its file's row.
ALTER TABLE chapter ADD COLUMN naming_unedited JSONB;
-- Rewrites queued before this step keep their file's title and unedited values.
UPDATE published_file SET pending_title = comicinfo_title, pending_unedited = unedited WHERE pending_path IS NOT NULL;
