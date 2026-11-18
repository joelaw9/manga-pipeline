-- A download's chapter number (names.parse's, as a float), written by the discover stage together with its identity:
-- every download past discover has one, since discover makes a chapter with no number needs_human (SPEC "Chapter
-- stages"). The re-download, which validate drives, reads it; NULL for a download still at discover and for a
-- rewrite chapter.
ALTER TABLE chapter ADD COLUMN number DOUBLE PRECISION;
-- Existing downloads past discover take it from their stored identity. This one-time backfill is the only SQL reader of
-- the identity's layout (names.identity's JSON: [scanlator key, season, side label, number, title key], the number
-- null for a chapter with none).
UPDATE chapter SET number = (identity::jsonb ->> 3)::double precision WHERE kind = 'download' AND stage <> 'discover';
-- A download past discover whose identity holds no number cannot be: the step refuses the database, naming how many.
DO $$
DECLARE
    missing BIGINT;
BEGIN
    SELECT count(*) INTO missing FROM chapter WHERE kind = 'download' AND stage <> 'discover' AND number IS NULL;
    IF missing > 0 THEN
        RAISE EXCEPTION '% download(s) past discover have no number in their identity', missing;
    END IF;
END
$$;
-- A download past discover has a number. Checked at commit, not per statement: workflow moves a chapter's stage before
-- its stage's record writes the number, in 1 transaction (a CHECK cannot be deferred). Only a write that leaves the row
-- without one queues the check; the check reads the row again, since a deferred trigger's NEW is the row as that
-- statement left it, not as the transaction does.
CREATE FUNCTION chapter_number_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM chapter WHERE id = NEW.id AND kind = 'download' AND stage <> 'discover' AND number IS NULL
    ) THEN
        RAISE EXCEPTION 'chapter %: a download past discover has no number', NEW.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER chapter_number AFTER INSERT OR UPDATE ON chapter DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.kind = 'download' AND NEW.stage <> 'discover' AND NEW.number IS NULL)
    EXECUTE FUNCTION chapter_number_check();
