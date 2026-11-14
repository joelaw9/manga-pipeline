-- Schema step 0042 (SPEC "Junk decisions", unsignable units): decisions and candidates on a signature whose every tile
-- is a uniform image's (images.uniform_tiles: these 2 hashes) are deleted: such a key matches any blank or flat rows.
-- No new one can be written.

DELETE FROM junk_decision
WHERE kind IN ('block', 'probe', 'piece', 'page', 'band')
    AND key ~ '^((0000000000000000|8000000000000000):)*(0000000000000000|8000000000000000)\|';

DELETE FROM junk_candidate
WHERE kind IN ('block', 'probe', 'piece', 'page', 'band')
    AND key ~ '^((0000000000000000|8000000000000000):)*(0000000000000000|8000000000000000)\|';
