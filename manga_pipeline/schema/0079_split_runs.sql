-- Schema step 0079 (SPEC "Retroactive updates", Split chapters): a join chapter's other files, a queued join's files
-- and the parts a joined file holds. chapter.join_parts: the files a join chapter joins after the one it names;
-- published_file.pending_parts: a queued join's other files, cleared with the other pending columns;
-- published_file.joined_parts: how many parts a join made the file hold (its parts N.1 ... N.k). Existing rows: none.

ALTER TABLE chapter ADD COLUMN join_parts BIGINT[];
ALTER TABLE published_file ADD COLUMN pending_parts BIGINT[];
ALTER TABLE published_file ADD COLUMN joined_parts INTEGER CHECK (joined_parts >= 2);
