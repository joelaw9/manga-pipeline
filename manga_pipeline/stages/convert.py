"""Stage convert (SPEC "Chapter stages"). Stub: copies the download unchanged to
<paths.staging>/<database id, first 8 characters>-<chapter id>.cbz, written as .part and renamed. Phase 6 replaces it.

The database id in the name keeps a file left in staging by another database from passing as this chapter's output.
"""

import os
import shutil
from pathlib import Path

from manga_pipeline import config, db, workflow


def staged_name(conn, chapter_id):
    return f"{db.database_id(conn)[:8]}-{chapter_id}.cbz"


def run(ctx, chapter):
    source = Path(chapter["download_path"])
    staged = config.abs_path(ctx.cfg["paths"]["staging"]) / staged_name(ctx.conn, chapter["id"])
    size = source.stat().st_size
    if not (staged.exists() and staged.stat().st_size == size):  # in place after a crash: the write is done
        part = staged.with_name(staged.name + ".part")
        shutil.copyfile(source, part)
        os.replace(part, staged)

    def record(conn):
        conn.execute(
            "UPDATE chapter SET staged_path = ?, staged_size = ? WHERE id = ?", (str(staged), size, chapter["id"])
        )

    return workflow.Done(record)
