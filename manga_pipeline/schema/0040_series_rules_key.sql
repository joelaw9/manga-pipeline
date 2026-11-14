-- Schema step 0040 (SPEC "Junk decisions", "Publishing"): series.rules_key, the name a series' junk decisions,
-- sightings, candidates and volume override file are kept under: the last part of the first folder the series had,
-- for '/' and '\' paths alike. A later folder (series link, a renamed folder) leaves it. The database sets it, so
-- every writer of series.folder follows the rule.

ALTER TABLE series ADD COLUMN rules_key TEXT;

CREATE FUNCTION series_rules_key() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rules_key IS NULL AND NEW.folder IS NOT NULL THEN
        NEW.rules_key := regexp_replace(regexp_replace(NEW.folder, '[/\\]+$', ''), '^.*[/\\]', '');
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER series_rules_key BEFORE INSERT OR UPDATE ON series
    FOR EACH ROW EXECUTE FUNCTION series_rules_key();

UPDATE series SET folder = folder WHERE folder IS NOT NULL;  -- the trigger fills the rows there are
