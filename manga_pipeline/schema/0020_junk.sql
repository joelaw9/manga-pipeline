-- Schema step 0020 (SPEC "Junk decisions"): the junk decision store, the chapters' junk scans and the open candidates.
-- A series key is the series folder's name ('' = a global decision). A key is a unit's signature text
-- (images.sig_to_str) or, for a frame, junk.frames.key_text.

-- Knowledge: every decision, imported or made by the pipeline.
CREATE TABLE junk_decision (
    id INTEGER PRIMARY KEY,
    series_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('block', 'probe', 'piece', 'page', 'band', 'frame')),
    key TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('junk', 'keep')),
    decider TEXT NOT NULL CHECK (decider IN ('import', 'claude', 'human')),
    decided_at TEXT NOT NULL,
    source TEXT,
    UNIQUE (series_key, kind, key)
) STRICT;

-- Knowledge: which chapters of a series showed a unit (recurrence, the first 3 chapters); first_seen orders them.
-- origin: 'pipeline' (a scan) or 'import' (the bulk converter's sightings, rebuilt by every store import).
CREATE TABLE junk_seen (
    series_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    identity TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('pipeline', 'import')),
    PRIMARY KEY (series_key, kind, key, identity, origin)
) STRICT;

-- Workflow state: a chapter's junk scan, current while its series and content digest are the chapter's. Emptied by
-- state reset through the cascade from chapter.
CREATE TABLE junk_scan (
    chapter_id INTEGER PRIMARY KEY REFERENCES chapter (id) ON DELETE CASCADE,
    series_id INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    scan TEXT NOT NULL CHECK (json_valid(scan) AND json_type(scan) = 'object'),
    scanned_at TEXT NOT NULL,
    -- the candidates the chapter waits on (junk_candidate ids), set with its junk_decision wait
    candidates TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(candidates) AND json_type(candidates) = 'array')
) STRICT;

-- Workflow state: 1 candidate per series key, kind and key. review_id is the review that last listed it; review_open
-- is 1 until that review has ended and counted it.
-- AUTOINCREMENT: needs_human reasons and the review name a candidate by id, so an id is never reused.
CREATE TABLE junk_candidate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('block', 'probe', 'piece', 'page', 'band', 'frame')),
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

CREATE INDEX junk_decision_scope ON junk_decision (series_key, kind);
