-- Schema step 0070 (SPEC "Needs human"): the Discord queue. A report (a chapter or series made needs_human, a series
-- title changed at the sources) queues 1 row per kind, item and text (which holds the reason); export sends what is unsent. Not a workflow
-- table: `state reset` keeps it, so a series' old report is never sent again.
-- sent_at: claimed for or done by a post; failed: Discord refused the message itself (it is never sent).
CREATE TABLE notification (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('chapter', 'series', 'title')),
    item_id BIGINT NOT NULL,
    text TEXT NOT NULL,
    fix TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    sent_at TEXT,
    failed TEXT
);
-- md5: a text can be long; the key stays small
CREATE UNIQUE INDEX notification_once ON notification (kind, item_id, md5(text));
CREATE INDEX notification_unsent ON notification (id) WHERE sent_at IS NULL AND failed IS NULL;
