-- Phase 7e-3: a better copy replacing its published file (SPEC "Library copies", "Retroactive updates"):
-- pending_copy holds the row values publish records for a new file (JSON), queued with the rewrite; finish's last
-- step takes them. Cleared with the other pending columns.
ALTER TABLE published_file ADD COLUMN pending_copy JSONB;
-- The original's bits per pixel and the junk removed from it (pages dropped, or strip rows per 1000 px of width),
-- stored by publish: a later copy of the chapter is judged against them. NULL: not known (a bulk file, or one
-- published before they were stored).
ALTER TABLE published_file ADD COLUMN bpp DOUBLE PRECISION;
ALTER TABLE published_file ADD COLUMN junk DOUBLE PRECISION;
