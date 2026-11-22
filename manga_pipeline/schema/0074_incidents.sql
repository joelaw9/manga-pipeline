-- Schema step 0074 (SPEC "Needs human"): incidents (a stalled part, downloads not taken, Kavita refusing the key)
-- are queued and sent as reports are, under their own kind.
ALTER TABLE notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE notification ADD CONSTRAINT notification_kind_check CHECK (kind IN ('chapter', 'series', 'title', 'incident'));
