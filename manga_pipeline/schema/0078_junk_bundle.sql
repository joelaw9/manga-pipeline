-- Schema step 0078 (SPEC "Junk decisions", rest of card): a chapter's rest-of-card bundle record and the bundle mark.
-- junk_scan.bundles: [{"ids": candidate ids, "size": member count}], written with candidates, reset with them on a
-- rescan. junk_decision.bundle: the decision was written on a candidate of a bundle of more than 1 member (or imported
-- from a bulk-converter `rn` row): a keep so marked never protects a gap. Existing rows: no bundle, no mark.

ALTER TABLE junk_scan ADD COLUMN bundles TEXT NOT NULL DEFAULT '[]' CHECK (bundles IS JSON ARRAY);
ALTER TABLE junk_decision ADD COLUMN bundle BOOLEAN NOT NULL DEFAULT false;
