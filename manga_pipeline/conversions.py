"""Adopted files' conversions (SPEC "State") and rebuilds (SPEC "Retroactive updates"): their chapters and links.

`queue` gives a download chapter to each adopted file that is confirmed, in an ok series in scope, not busy and named
by no chapter, in order of series and number, while fewer than export.rewrites_open chapters that replace an adopted
file are open (counted apart from the rewrites; needs_human ones count). It only inserts chapters.

A conversion's download is a hard link to its library file, <paths.staging>/adopted/<published file id>/<file name>:
`link` makes it for the discover stage, before the download is read; `clean_links` removes the link folders no open
chapter's download is in. Only `intake` runs them, on the LXC, where staging and the library are 1 filesystem.

`rebuild` gives a pipeline file (or a converted adopted one) a rebuild: the same chapter, whose download is a hard link
to the file's original in retention, named as that download was (<source name><suffix>), so discover parses the
identity its first publish had. `download_chapters` finds a file's download chapters by name.
"""

import logging
import os
import shutil
import stat
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from manga_pipeline import comicinfo, config, db, errors, names, rewrite, workflow
from manga_pipeline.db import sql

log = logging.getLogger(__name__)

FOLDER = workflow.LINK_FOLDER  # below paths.staging: the link folders
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
            "conversions: %d queued; %d adopted file(s) left: %d conversions and rebuilds are open, the cap "
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
    """Make the chapter's download, a hard link; return None once linked, else why it cannot be.

    An adopted file's conversion links the library file; any other file's (a rebuild) links its original, never the
    published file. A file at the link path that is the source is kept; any other file there is removed unread;
    anything that is not a file is a link error, never removed. A missing source, or a link that cannot be made, gives
    the reason. A dropped share raises Unreachable("share").
    """
    file_id = chapter["published_file_id"]
    file = db.one_row(ctx.conn, "SELECT library_path, origin, original FROM published_file WHERE id = %s", (file_id,))
    if file["origin"] == "library":
        what, stored = "the adopted file", file["library_path"]
    elif file["original"] is None:
        return f"published file {file_id} records no original: a rebuild never links the published file"
    else:
        what, stored = "the original", file["original"]
    source = config.to_local(ctx.cfg, stored)
    target = config.to_local(ctx.cfg, chapter["download_path"])
    try:
        with workflow.share_errors():
            if not errors.exists(source):
                return f"{what} {stored} is missing"
            return _link(ctx, chapter, source, target)
    except OSError as e:
        return f"{what} {stored} cannot be linked as {chapter['download_path']}: {e}"


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


class NotQueuedError(Exception):
    """A file `rewrite rebuild` named was not queued (exit 1); each is listed with why."""


def download_chapters(conn: db.Connection, series_id: int) -> dict[str, list[db.Row]]:
    """Return the series' done download chapters, not dropped, by their download's stem, newest first."""
    rows = conn.execute(
        """SELECT id, download_path, content_digest, source, source_url, suwayomi_chapter_id FROM chapter
        WHERE series_id = %s AND kind = 'download' AND status = 'done' AND download_path IS NOT NULL
            AND content_digest IS NOT NULL AND coalesce(last_error, '') NOT LIKE 'dropped%%'
        ORDER BY id DESC""",
        (series_id,),
    ).fetchall()
    by_stem: dict[str, list[db.Row]] = {}
    for r in rows:
        by_stem.setdefault(PurePosixPath(r["download_path"]).stem, []).append(r)
    return by_stem


def rebuild_link_path(cfg: config.Config, file: db.Row) -> Path:
    """Return a rebuild's link: <paths.staging>/adopted/<file id>/<its source name><its original's suffix>."""
    suffix = PurePosixPath(file["original"]).suffix
    return config.abs_path(cfg["paths"]["staging"]) / FOLDER / str(file["id"]) / f"{file['source_name']}{suffix}"


# A file f of series s a rebuild may take now (SPEC "Retroactive updates"; workflow.SCOPE's parameters).
REBUILDABLE = sql.SQL("""f.origin = 'pipeline' AND f.state = 'confirmed' AND f.joined_parts IS NULL
    AND f.original IS NOT NULL AND NOT {busy} AND s.status = 'ok' AND {scope}""").format(
    busy=rewrite.BUSY.format(p=sql.Identifier("f")), scope=workflow.SCOPE
)


def rebuild(cfg: config.Config, conn: db.Connection, file_ids: Sequence[int]) -> tuple[int, list[str]]:
    """Give each named file a rebuild; return (queued, [listed file: why]).

    Each file's share reads come first, outside any transaction; then 1 transaction re-checks it and inserts its
    chapter. A dropped share raises Unreachable("share").
    """
    queued, listed, seen = 0, list[str](), set[int]()
    for file_id in file_ids:
        if file_id in seen:
            listed.append(f"file {file_id}: named twice")
            continue
        seen.add(file_id)
        why = _rebuild_one(cfg, conn, file_id)
        if why is None:
            queued += 1
        else:
            listed.append(f"file {file_id}: {why}")
    if queued:
        log.info("rebuilds: %d queued", queued)
    return queued, listed


def _rebuild_one(cfg: config.Config, conn: db.Connection, file_id: int) -> str | None:
    """Queue 1 file's rebuild; return None when queued, else why not."""
    file = conn.execute("SELECT * FROM published_file WHERE id = %s", (file_id,)).fetchone()
    why = None if file is None else _lasting(file)
    if file is None or why is not None:
        return why or "no such published file"
    original = config.to_local(cfg, file["original"])
    with workflow.share_errors():
        if not errors.exists(original):
            return f"its original {file['original']} is no longer in retention"
        number = comicinfo.download_number(original)
    parsed = names.identity(names.parse(file["source_name"], number))
    if parsed != file["identity"]:
        return f"its identity {file['identity']} would be parsed as {parsed} (first published from a re-download)"
    named = [
        c
        for c in download_chapters(conn, file["series_id"]).get(file["source_name"], [])
        if c["source"] == file["source"]
    ]
    chapter = named[0] if named else None
    params: dict[str, object] = {**workflow.scope_params(cfg), "file": file_id, "original": file["original"]}
    with db.transaction(conn):
        ok = conn.execute(
            sql.SQL("""SELECT 1 FROM published_file f JOIN series s ON s.id = f.series_id
            WHERE f.id = %(file)s AND f.original = %(original)s AND {rebuildable}""").format(rebuildable=REBUILDABLE),
            params,
        ).fetchone()
        if ok is None:
            return _passing(cfg, conn, file_id)
        new = workflow.NewChapter(
            file["series_id"],
            file["identity"],
            config.to_stored(cfg, rebuild_link_path(cfg, file)),
            suwayomi_chapter_id=None if chapter is None else chapter["suwayomi_chapter_id"],
            source=file["source"],
            source_url=None if chapter is None else chapter["source_url"],
            published_file_id=file_id,
        )
        if workflow.add_chapter(conn, new) is None:
            return "queued"
    return None


def _lasting(file: db.Row) -> str | None:
    """Return why a file can never be rebuilt as it is, else None."""
    if file["origin"] != "pipeline":
        return f"origin {file['origin']}: only a pipeline file is rebuilt"
    if file["state"] != "confirmed":
        return "not confirmed yet"
    if file["joined_parts"] is not None:
        return "a joined file: no single original"
    if file["original"] is None:
        return "no original recorded"
    if file["source_name"] is None:
        return "no source name recorded: its download's name is unknown"
    return None


def _passing(cfg: config.Config, conn: db.Connection, file_id: int) -> str:
    """Return why a file a rebuild could take is not taken now (its row re-read in the caller's transaction)."""
    row = conn.execute(
        sql.SQL("""SELECT f.*, s.status AS series_status, {scope} AS in_scope,
            EXISTS (SELECT 1 FROM chapter c WHERE c.published_file_id = f.id AND c.status <> 'done'
                AND {rebuild}) AS rebuilding,
            {busy} AS busy
        FROM published_file f JOIN series s ON s.id = f.series_id WHERE f.id = %(file)s""").format(
            scope=workflow.SCOPE,
            rebuild=workflow.REBUILD.format(c=sql.Identifier("c")),
            busy=rewrite.BUSY.format(p=sql.Identifier("f")),
        ),
        {**workflow.scope_params(cfg), "file": file_id},
    ).fetchone()
    if row is None:  # a join removed it since it was read
        return "no such published file"
    if row["rebuilding"]:
        return "queued"
    if row["series_status"] != "ok" or not row["in_scope"]:
        return f"its series is {row['series_status']}" if row["in_scope"] else "its series is out of scope"
    if row["busy"]:
        return "busy"
    return _lasting(row) or "changed since it was read: name it again"
