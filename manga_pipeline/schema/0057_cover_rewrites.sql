-- Phase 7e-2: retroactive volume cover rewrites (SPEC "Retroactive updates").
-- cover_state: the volume cover a published file holds: NULL not known yet, 'none', 'volume:<V>', or
-- 'unfit:<sha256>' (holds none: that cover image could not be inserted). pending_cover: the state a queued rewrite
-- gives it (finish's last act applies it).
ALTER TABLE published_file ADD COLUMN cover_state TEXT;
ALTER TABLE published_file ADD COLUMN pending_cover TEXT;
-- A rewrite chapter that drops its file's cover (one that inserts a cover has a volume_cover row).
ALTER TABLE chapter ADD COLUMN drop_cover BOOLEAN NOT NULL DEFAULT false;
-- volume_cover_wanted: a covered volume's cover image a published file needs and the series has not stored yet;
-- intake fetches it (a workflow table: `state reset` empties it, and the comparison wants it again).
CREATE TABLE volume_cover_wanted (
    series_id BIGINT NOT NULL REFERENCES series (id) ON DELETE CASCADE,
    volume BIGINT NOT NULL,
    attempts BIGINT NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (series_id, volume)
);
