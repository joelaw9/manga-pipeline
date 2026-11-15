-- Schema step 0052 (SPEC "Metadata", "Retroactive updates"): what export last wrote to Kavita for a series.
-- kavita_series_id: the Kavita series written (a recreated series is written afresh); inputs: what the written fields
-- came from ("<data version>:<tag rules fingerprint>:<field merge fingerprint>", empty while a write is unfinished);
-- written: {field: value} the pipeline wrote (or found already right), the base of the lock guard; kept: fields a
-- person locked at another value (reported once); cover_sha256: the cover Kavita holds from the pipeline.
-- Knowledge: state reset keeps it.

CREATE TABLE series_written (
    series_id BIGINT PRIMARY KEY REFERENCES series (id) ON DELETE CASCADE,
    kavita_series_id BIGINT NOT NULL,
    inputs TEXT NOT NULL,
    written JSONB NOT NULL,
    kept JSONB NOT NULL DEFAULT '[]',
    cover_sha256 TEXT,
    written_at TEXT NOT NULL
);
