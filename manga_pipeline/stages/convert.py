"""Stage convert (SPEC "Chapter stages"). Stub: writes the download's pages, bytes untouched and in reading order, as
a store-mode CBZ at <paths.staging>/<database id, first 8 characters>-<chapter id>.cbz (archive.write: .part, rename,
test). Phase 6 replaces it.

The database id in the name keeps a file left in staging by another database from passing as this chapter's output.
"""

from pathlib import Path

from manga_pipeline import archive, config, db, workflow


def staged_name(conn, chapter_id):
    return f"{db.database_id(conn)[:8]}-{chapter_id}.cbz"


def run(ctx, chapter):
    staged = config.abs_path(ctx.cfg["paths"]["staging"]) / staged_name(ctx.conn, chapter["id"])
    size = archive.write(staged, archive.pages(Path(chapter["download_path"])))

    def record(conn):
        conn.execute(
            "UPDATE chapter SET staged_path = ?, staged_size = ? WHERE id = ?", (str(staged), size, chapter["id"])
        )

    return workflow.Done(record)
