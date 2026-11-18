"""Handled chapters (SPEC "Bad downloads").

Every chapter the pipeline published, rejected, found a duplicate of or dropped, with its download's content digest. A
download whose pages a handled chapter of its series already had is put aside, not processed again; checked by the
discover stage and by the junk stage once the series' baseline exists.
"""

from typing import Literal, get_args

from manga_pipeline import config, db, retention, workflow

type Outcome = Literal["published", "rejected", "duplicate", "dropped"]
OUTCOMES: tuple[Outcome, ...] = get_args(Outcome.__value__)


def put_aside(ctx: workflow.Context, chapter: db.Row, digest: str) -> str | None:
    """Put a download aside when a handled chapter of its series has this content digest.

    Then move the download the retention way and return why (the stage then drops the chapter); else None.
    """
    row = ctx.conn.execute(
        """SELECT identity, outcome FROM handled_chapter WHERE series_id = %s AND content_digest = %s
        ORDER BY id LIMIT 1""",
        (chapter["series_id"], digest),
    ).fetchone()
    if row is None:
        return None
    workflow.holds_claim(ctx, chapter)
    retention.put(ctx.cfg, config.to_local(ctx.cfg, chapter["download_path"]))
    return f"same pages as a handled chapter ({row['outcome']}, {row['identity']})"


def record(conn: db.Connection, series_id: int, identity: str, outcome: Outcome, digest: str | None) -> None:
    """Record a handled chapter, inside the caller's transaction.

    digest: None for an archive that could not be read.
    """
    conn.execute(
        "INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (series_id, identity, outcome, digest, workflow.now(conn)),
    )
