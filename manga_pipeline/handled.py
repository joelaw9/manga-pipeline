"""Handled chapters (SPEC "Bad downloads"): every chapter the pipeline published, rejected, found a duplicate of or
dropped, with its download's content digest. A download whose pages a handled chapter of its series already had is
put aside, not processed again; checked by the discover stage and by the junk stage once the series' baseline exists.
"""

from pathlib import Path

from manga_pipeline import retention, workflow

OUTCOMES = ("published", "rejected", "duplicate", "dropped")


def put_aside(ctx, chapter, digest):
    """When a handled chapter of the chapter's series has this content digest: move the download the retention way and
    return why (the stage then drops the chapter); else None."""
    row = ctx.conn.execute(
        """SELECT identity, outcome FROM handled_chapter WHERE series_id = %s AND content_digest = %s
        ORDER BY id LIMIT 1""",
        (chapter["series_id"], digest),
    ).fetchone()
    if row is None:
        return None
    retention.put(ctx.cfg, Path(chapter["download_path"]))
    return f"same pages as a handled chapter ({row['outcome']}, {row['identity']})"


def record(conn, series_id, identity, outcome, digest):
    """Record a handled chapter (digest None for an archive that could not be read); inside the caller's
    transaction."""
    if outcome not in OUTCOMES:
        raise ValueError(f"handled chapter outcome must be one of {OUTCOMES}, got {outcome!r}")
    conn.execute(
        "INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (series_id, identity, outcome, digest, workflow.now()),
    )
