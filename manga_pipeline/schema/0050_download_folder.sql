-- Schema step 0050 (SPEC "Series identification"): a hand-placed download folder's link to its series, keyed by its 2
-- folder names under <downloads>/mangas ("<source folder>/<title folder>"). Knowledge: state reset keeps it.

CREATE TABLE download_folder (
    folder TEXT PRIMARY KEY,
    series_id BIGINT NOT NULL REFERENCES series (id)
);
