"""Handled chapters (SPEC "Bad downloads").

Every chapter the pipeline published, rejected, found a duplicate of or dropped, with its download's content digest. A
download whose pages a handled chapter of its series already had is put aside, not processed again; checked by the
discover stage and by the junk stage once the series' baseline exists.
"""

import os
import re
from pathlib import PurePosixPath
from typing import Literal

from manga_pipeline import config, db, errors, retention, validation, workflow

type Outcome = Literal["published", "rejected", "duplicate", "dropped"]


def renamed(cfg: config.Config, conn: db.Connection, chapter: db.Row) -> str | None:
    """Return why a download chapter's download was renamed away (SPEC "Bad downloads"), else None.

    Renamed away: its download is gone, and another chapter of its series that is not done has the same chapter key,
    the same content digest and its download in place (Suwayomi renamed the file; that chapter is the new one). A
    chapter being re-downloaded, naming a published file, or with no digest is never renamed away.
    """
    if (
        chapter["kind"] != "download"
        or chapter["published_file_id"] is not None
        or chapter["redownload"] is not None
        or not chapter["content_digest"]
        or errors.exists(config.to_local(cfg, chapter["download_path"]))
    ):
        return None
    key = validation.chapter_key(chapter["identity"])
    for twin in conn.execute(
        """SELECT id, identity, download_path FROM chapter
        WHERE series_id = %s AND id <> %s AND status <> 'done' AND kind = 'download' AND content_digest = %s
            AND download_path IS NOT NULL
        ORDER BY id""",
        (chapter["series_id"], chapter["id"], chapter["content_digest"]),
    ):
        if validation.chapter_key(twin["identity"]) != key:
            continue
        if os.name == "nt" and re.search(workflow.WINDOWS_UNUSABLE, twin["download_path"]):
            continue  # a path Windows cannot open: not checked here (stages/junk.py does the same)
        if errors.exists(config.to_local(cfg, twin["download_path"])):
            name = PurePosixPath(twin["download_path"]).name
            return f"download renamed: chapter {twin['id']} has its pages at {name}"
    return None


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
