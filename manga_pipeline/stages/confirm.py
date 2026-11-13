"""Stage confirm (SPEC "Chapter stages"). Stub: takes the published file as indexed, records the chapter as handled
(published) and deletes the download, with no retention folder. Phase 7 replaces it."""

from pathlib import Path

from manga_pipeline import workflow


def run(ctx, chapter):
    Path(chapter["download_path"]).unlink(missing_ok=True)  # already gone after a crash: the delete is done

    def record(conn):
        conn.execute("UPDATE published_file SET state = 'confirmed' WHERE id = ?", (chapter["published_file_id"],))
        conn.execute(
            """INSERT INTO handled_chapter (series_id, identity, outcome, handled_at)
            VALUES (?, ?, 'published', ?)""",
            (chapter["series_id"], chapter["identity"], workflow.now()),
        )

    return workflow.Done(record)
