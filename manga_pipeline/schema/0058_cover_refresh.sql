-- Phase 7e-2: a rewrite that changed its file's cover (SPEC "Retroactive updates"): once Kavita re-read the file,
-- export has Kavita regenerate the series' cover images (a library scan keeps a re-read chapter's old one).
ALTER TABLE published_file ADD COLUMN carry_cover BOOLEAN;
