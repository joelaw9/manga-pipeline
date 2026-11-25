-- Schema step 0077 (SPEC "Junk decisions", chains and cuts): a decision's chain (canonical JSON text, junk.chains) and
-- credit, and decision kind 'cut'. Existing rows carry no chain and no credit; candidates have no cut kind.

ALTER TABLE junk_decision ADD COLUMN chain TEXT;
ALTER TABLE junk_decision ADD COLUMN credit BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE junk_decision DROP CONSTRAINT junk_decision_kind_check;
ALTER TABLE junk_decision ADD CONSTRAINT junk_decision_kind_check
    CHECK (kind IN ('block', 'probe', 'piece', 'page', 'band', 'frame', 'logo', 'cut'));
