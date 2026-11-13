-- Schema step 0021 (SPEC "Junk decisions"): unit kind 'logo' (a logo's key is junk.marks.Template.key_text).
-- SQLite cannot change a CHECK in place, so junk_decision and junk_candidate are rebuilt with their rows; a
-- candidate's id is never reused, so its AUTOINCREMENT counter is carried over.

CREATE TABLE junk_decision_new (
    id INTEGER PRIMARY KEY,
    series_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('block', 'probe', 'piece', 'page', 'band', 'frame', 'logo')),
    key TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('junk', 'keep')),
    decider TEXT NOT NULL CHECK (decider IN ('import', 'claude', 'human')),
    decided_at TEXT NOT NULL,
    source TEXT,
    UNIQUE (series_key, kind, key)
) STRICT;
INSERT INTO junk_decision_new SELECT * FROM junk_decision;
DROP TABLE junk_decision;
ALTER TABLE junk_decision_new RENAME TO junk_decision;
CREATE INDEX junk_decision_scope ON junk_decision (series_key, kind);

CREATE TABLE junk_candidate_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('block', 'probe', 'piece', 'page', 'band', 'frame', 'logo')),
    key TEXT NOT NULL,
    reason TEXT NOT NULL,
    folder TEXT NOT NULL,
    reviews INTEGER NOT NULL DEFAULT 0 CHECK (reviews >= 0),
    review_id TEXT,
    review_open INTEGER NOT NULL DEFAULT 0 CHECK (review_open IN (0, 1)),
    verdict TEXT CHECK (verdict IS NULL OR verdict = 'unsure'),
    created_at TEXT NOT NULL,
    UNIQUE (series_key, kind, key)
) STRICT;
INSERT INTO junk_candidate_new SELECT * FROM junk_candidate;
DELETE FROM sqlite_sequence WHERE name = 'junk_candidate_new';
INSERT INTO sqlite_sequence (name, seq) SELECT 'junk_candidate_new', seq FROM sqlite_sequence WHERE name = 'junk_candidate';
DROP TABLE junk_candidate;
ALTER TABLE junk_candidate_new RENAME TO junk_candidate;
