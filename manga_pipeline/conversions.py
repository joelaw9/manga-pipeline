"""Adopted files' conversions (SPEC "State"): the queue that gives each adopted file its chapter, and its link.

`queue` gives a download chapter to each adopted file that is confirmed, in an ok series in scope, not busy and named
by no chapter, in order of series and number, while fewer than export.rewrites_open chapters that replace an adopted
file are open (counted apart from the rewrites; needs_human ones count). It only inserts chapters.

A conversion's download is a hard link to its library file, <paths.staging>/adopted/<published file id>/<file name>:
`link` makes it for the discover stage, before the download is read; `clean_links` removes the link folders no open
chapter's download is in. Only `intake` runs them, on the LXC, where staging and the library are 1 filesystem.
"""

import logging
import os
import shutil
import stat
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from manga_pipeline import config, db, errors, rewrite, workflow
from manga_pipeline.db import sql

log = logging.getLogger(__name__)

FOLDER = "adopted"  # below paths.staging: the link folders
# The chapters that replace an adopted file and are not done (needs_human ones included): the count the cap compares.
OPEN = sql.SQL("status <> 'done' AND {}").format(workflow.REPLACES_ADOPTED.format(c=sql.Identifier("chapter")))
# An adopted file p of series s that gets a conversion: confirmed, not busy, named by no chapter, its series ok and in
# scope (workflow.SCOPE's parameters).
CANDIDATE = sql.SQL("""p.origin = 'library' AND p.state = 'confirmed' AND NOT {busy}
    AND NOT EXISTS (SELECT 1 FROM chapter n WHERE n.published_file_id = p.id)
    AND s.status = 'ok' AND {scope}""").format(busy=rewrite.BUSY.format(p=sql.Identifier("p")), scope=workflow.SCOPE)


@dataclass
class Queued:
    """What queue did, per series id: conversions queued, and files left because the cap was full."""

    queued: Counter[int] = field(default_factory=Counter)
    left: Counter[int] = field(default_factory=Counter)


def queue(cfg: config.Config, conn: db.Connection, series: Sequence[int] | None = None) -> Queued:
    """Give a conversion chapter to each candidate adopted file (CANDIDATE), while the cap has room.

    In order of series id, then chapter number; with `series` (series ids), only those series, in that order, then by
    number. Each file gets 1 short transaction (the database-wide write lock), touching no share: it re-checks the file
    and that fewer than export.rewrites_open chapters are OPEN, then inserts the chapter, ready at discover, its
    download its link (discover makes it). It stops at the first file the cap refuses.
    """
    cap = cfg["export"]["rewrites_open"]
    params: dict[str, object] = {**workflow.scope_params(cfg), "series": None if series is None else list(series)}
    files = conn.execute(
        sql.SQL("""SELECT p.id, p.series_id FROM published_file p JOIN series s ON s.id = p.series_id
        WHERE {candidate} AND (%(series)s::bigint[] IS NULL OR p.series_id = ANY(%(series)s::bigint[]))
        ORDER BY array_position(%(series)s::bigint[], p.series_id), p.series_id,
            p.comicinfo_number::double precision, p.id""").format(candidate=CANDIDATE),
        params,
    ).fetchall()
    done = Queued()
    for i, file in enumerate(files):
        with db.transaction(conn):
            full = db.one_row(conn, sql.SQL("SELECT count(*) FROM chapter WHERE {}").format(OPEN))[0] >= cap
            if not full:
                done.queued[file["series_id"]] += _insert(cfg, conn, file["id"], params)
        if full:
            done.left.update(f["series_id"] for f in files[i:])
            break
    if done.queued or done.left:
        opened = db.one_row(conn, sql.SQL("SELECT count(*) FROM chapter WHERE {}").format(OPEN))[0]
        log.info(
            "conversions: %d queued; %d adopted file(s) left: %d chapters replacing adopted files are open, the cap "
            "is %d (export.rewrites_open)",
            done.queued.total(),
            done.left.total(),
            opened,
            cap,
        )
    return done


def _insert(cfg: config.Config, conn: db.Connection, file_id: int, params: dict[str, object]) -> int:
    """Insert the file's conversion chapter if it is still a candidate; return 1 when inserted, else 0."""
    row = conn.execute(
        sql.SQL("""SELECT p.* FROM published_file p JOIN series s ON s.id = p.series_id
        WHERE p.id = %(file)s AND {candidate}""").format(candidate=CANDIDATE),
        {**params, "file": file_id},
    ).fetchone()
    if row is None:  # taken, converted or held meanwhile
        return 0
    new = workflow.NewChapter(
        row["series_id"],
        row["identity"],
        config.to_stored(cfg, link_path(cfg, row)),
        suwayomi_chapter_id=None,
        source=None,
        source_url=None,
        published_file_id=row["id"],
    )
    return int(workflow.add_chapter(conn, new) is not None)


def link_path(cfg: config.Config, file: db.Row) -> Path:
    """Return a published file's link: <paths.staging>/adopted/<its id>/<its file name>."""
    name = PurePosixPath(file["library_path"]).name
    return config.abs_path(cfg["paths"]["staging"]) / FOLDER / str(file["id"]) / name


def link(ctx: workflow.Context, chapter: db.Row) -> str | None:
    """Make the conversion's download, a hard link to its file; return None once linked, else why it cannot be.

    A file at the link path that is the library file is kept; any other file there is removed unread; anything that is
    not a file is a link error, never removed. A missing library file, or a link that cannot be made, gives the
    reason. A dropped share raises Unreachable("share").
    """
    file_id = chapter["published_file_id"]
    file = db.one_row(ctx.conn, "SELECT library_path FROM published_file WHERE id = %s", (file_id,))
    source = config.to_local(ctx.cfg, file["library_path"])
    target = config.to_local(ctx.cfg, chapter["download_path"])
    try:
        with workflow.share_errors():
            if not errors.exists(source):
                return f"the adopted file {file['library_path']} is missing"
            return _link(ctx, chapter, source, target)
    except OSError as e:
        return f"the adopted file {file['library_path']} cannot be linked as {chapter['download_path']}: {e}"


def _link(ctx: workflow.Context, chapter: db.Row, source: Path, target: Path) -> str | None:
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError as e:
        if errors.dropped_share(e):
            raise
        mode = None
    if mode is not None:
        if not stat.S_ISREG(mode):
            return f"{chapter['download_path']} is not a file: a link error (nothing there was removed)"
        if target.samefile(source):
            return None
    workflow.holds_claim(ctx, chapter)
    if mode is not None:
        target.unlink()  # another file: never read
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, target)
    return None


def clean_links(cfg: config.Config, conn: db.Connection) -> None:
    """Remove the link folders that no open chapter's download is in, outside any transaction.

    A folder is removed with what it holds; an entry that cannot be removed is logged and left for the next run. A
    dropped share raises Unreachable("share").
    """
    root = config.abs_path(cfg["paths"]["staging"]) / FOLDER
    with workflow.share_errors():
        if not errors.exists(root):
            return
        entries = sorted(root.iterdir())
    downloads = conn.execute("SELECT download_path FROM chapter WHERE status <> 'done' AND download_path IS NOT NULL")
    kept = {str(PurePosixPath(row[0]).parent) for row in downloads}
    for entry in entries:
        if config.to_stored(cfg, entry) in kept:
            continue
        try:
            with workflow.share_errors():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)  # a link is a name: removing it leaves the library file
                else:
                    entry.unlink()
        except OSError as e:
            log.warning("conversions: %s not removed, tried again next run: %s", entry, e)
        else:
            log.info("conversions: removed %s: no open chapter's download is in it", entry)
