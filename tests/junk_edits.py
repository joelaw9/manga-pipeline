"""The chapter_edit row the junk stage writes for convert (SPEC "Conversion"), for tests that run `process` end to end:
until the junk stage writes it (Phase 5), a chapter whose junk stage ends done gets an edit with no junk, written with
the outcome. Tests that test the stage on its own insert their rows themselves."""

import json

from manga_pipeline import process, workflow

NO_EDIT = {"drop_pages": [], "strip_removed": [], "bands": [], "join": [], "split": False, "blank": []}


def with_no_edit(junk_run):
    def run(ctx, chapter):
        outcome = junk_run(ctx, chapter)
        if not isinstance(outcome, workflow.Done):
            return outcome

        def record(conn):
            if outcome.record:
                outcome.record(conn)
            conn.execute(
                "INSERT OR REPLACE INTO chapter_edit (chapter_id, edit) VALUES (?, ?)",
                (chapter["id"], json.dumps(NO_EDIT)),
            )

        return workflow.Done(record)

    return run


def install(setitem):
    """setitem(process.STAGES, "junk", ...): monkeypatch.setitem in a test, operator.setitem in a process."""
    setitem(process.STAGES, "junk", with_no_edit(process.STAGES["junk"]))
