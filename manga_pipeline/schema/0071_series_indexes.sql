-- Schema step 0071 (SPEC "State"): indexes on chapter (series_id) and published_file (series_id), so the per-series
-- lookups of the claim, the naming comparison and validation read the series' rows, not the whole table.
CREATE INDEX chapter_series ON chapter (series_id);
CREATE INDEX published_file_series ON published_file (series_id);
