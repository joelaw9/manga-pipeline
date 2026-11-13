"""Handled chapters (SPEC "Bad downloads"): a download whose pages a handled chapter of its series already had is put
aside, not processed again. Used by the discover stage and by the junk stage once the series' baseline exists.
"""

from pathlib import Path

from manga_pipeline import retention


def put_aside(ctx, chapter, digest):
    """When a handled chapter of the chapter's series has this content digest: move the download the retention way and
    return why (the stage then drops the chapter); else None."""
    row = ctx.conn.execute(
        """SELECT identity, outcome FROM handled_chapter WHERE series_id = ? AND content_digest = ?
        ORDER BY id LIMIT 1""",
        (chapter["series_id"], digest),
    ).fetchone()
    if row is None:
        return None
    retention.put(ctx.cfg, Path(chapter["download_path"]))
    return f"same pages as a handled chapter ({row['outcome']}, {row['identity']})"
