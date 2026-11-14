-- Schema step 0021 (SPEC "Junk decisions"): unit kind 'logo' (a logo's key is junk.marks.Template.key_text).
-- The kind CHECK of junk_decision and junk_candidate is replaced in place; their rows and ids stay as they are.

ALTER TABLE junk_decision DROP CONSTRAINT junk_decision_kind_check;
ALTER TABLE junk_decision ADD CONSTRAINT junk_decision_kind_check
    CHECK (kind IN ('block', 'probe', 'piece', 'page', 'band', 'frame', 'logo'));

ALTER TABLE junk_candidate DROP CONSTRAINT junk_candidate_kind_check;
ALTER TABLE junk_candidate ADD CONSTRAINT junk_candidate_kind_check
    CHECK (kind IN ('block', 'probe', 'piece', 'page', 'band', 'frame', 'logo'));
