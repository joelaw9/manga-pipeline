-- Schema step 0081 (SPEC "Junk decisions", Pages and the chapter edit): a re-sliced chapter's page count, which junk
-- records in its scan (pages) and edit (split_pages) and convert executes. Rows written before it get null, the value
-- of a stitched chapter's scan and of every other edit.
UPDATE junk_scan SET scan = (scan::jsonb || '{"pages": null}')::text WHERE scan::jsonb ->> 'split' = 'true';
UPDATE chapter_edit SET edit = (edit::jsonb || '{"split_pages": null}')::text;
