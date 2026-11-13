"""Discovery (SPEC "Suwayomi", "Series identification"): `intake` lists the downloads, maps each to its library manga,
series and Suwayomi chapter, and records each complete one as a chapter at discover. Opens no CBZ.

What cannot be taken is collected and, once the listing is complete, replaces unmatched_download in 1 transaction.
"""

import logging
import os
from pathlib import Path

from manga_pipeline import config, db, identify, names, workflow
from manga_pipeline.clients import suwayomi

log = logging.getLogger(__name__)


def scan(ctx):
    """Record each new complete download as a chapter. A folder that cannot be read raises its OSError (the caller
    decides whether the share is unreachable); an unreachable service raises Unreachable before anything is written
    but the series rows identification settled."""
    root = config.abs_path(ctx.cfg["paths"]["downloads"]) / suwayomi.MANGAS_FOLDER
    folders = _list(root)
    unmatched, found = [], []
    if folders:
        by_folder = {}
        for manga in ctx.suwayomi.library_manga():
            if manga["source"] is not None:  # no extension installed: no folder name to rebuild
                key = (suwayomi.source_folder(manga["source"]["displayName"]), suwayomi.manga_folder(manga["title"]))
                by_folder.setdefault(key, []).append(manga)
        kavita_series = identify.KavitaSeries(ctx)
        for key, files in sorted(folders.items()):
            paths = [root / key[0] / key[1] / name for name in files]
            mangas = by_folder.get(key, [])
            if len(mangas) != 1:
                why = (
                    "no library manga maps to its folder"
                    if not mangas
                    else f"{len(mangas)} library manga map to its folder"
                )
                unmatched += [(p, why) for p in paths]
                continue
            unmatched_here, found_here = _manga_downloads(ctx, mangas[0], paths, kavita_series)
            unmatched += unmatched_here
            found += found_here
    _record(ctx, found, unmatched)


def _list(root):
    """{(source folder, manga folder): [CBZ file names]} under <downloads>/mangas. Never Path.glob: it swallows the
    OSError of a folder it cannot read."""
    folders = {}
    for source in _entries(root, dirs=True):
        for title in _entries(Path(root, source), dirs=True):
            files = sorted(n for n in _entries(Path(root, source, title), dirs=False) if n.lower().endswith(".cbz"))
            if files:
                folders[(source, title)] = files
    return folders


def _entries(folder, dirs):
    with os.scandir(folder) as it:
        return sorted(e.name for e in it if e.is_dir() == dirs)


def _manga_downloads(ctx, manga, paths, kavita_series):
    """([(path, reason)] not taken, [new chapter fields]) for 1 manga's downloads."""
    series = identify.series_for(ctx, manga, kavita_series)
    if isinstance(series, identify.NotTaken):
        return ([(p, series.reason) for p in paths] if series.listed else []), []
    chapters = {}
    for chapter in ctx.suwayomi.chapters(manga["id"]):
        chapters.setdefault(suwayomi.chapter_file(chapter["scanlator"], chapter["name"]), []).append(chapter)
    unmatched, found = [], []
    for path in paths:
        hits = chapters.get(path.name, [])
        if len(hits) != 1:
            why = (
                f"no chapter of Suwayomi manga {manga['id']} rebuilds to this file name"
                if not hits
                else (f"{len(hits)} chapters of Suwayomi manga {manga['id']} rebuild to this file name")
            )
            unmatched.append((path, why))
        elif hits[0]["isDownloaded"]:  # not flagged: Suwayomi is still writing it
            identity = names.identity(names.parse(path.stem))
            found.append((series["id"], identity, str(path), hits[0]["id"], str(manga["sourceId"])))
    return unmatched, found


def _record(ctx, found, unmatched):
    """Add the chapters not yet open at their path, and replace unmatched_download (first seen kept)."""
    stamp = workflow.now()
    with db.transaction(ctx.conn):
        added = 0
        for series_id, identity, path, chapter_id, source in found:
            open_ = ctx.conn.execute(
                "SELECT 1 FROM chapter WHERE download_path = ? AND status != 'done'", (path,)
            ).fetchone()
            # Checked after the row: confirm moves a download away before its chapter is done, so a file still here
            # once its chapter reads done is a new download, not one an export confirmed since the listing.
            if not open_ and os.path.exists(path):
                workflow.add_chapter(ctx.conn, series_id, identity, path, chapter_id, source)
                added += 1
        seen = dict(ctx.conn.execute("SELECT path, first_seen FROM unmatched_download").fetchall())
        ctx.conn.execute("DELETE FROM unmatched_download")
        ctx.conn.executemany(
            "INSERT INTO unmatched_download (path, reason, first_seen) VALUES (?, ?, ?)",
            [(str(p), why, seen.get(str(p), stamp)) for p, why in unmatched],
        )
    if added:
        log.info("%s: discovered %d new download(s)", ctx.part, added)
    if unmatched:
        log.info("%s: %d download(s) not taken (see status)", ctx.part, len(unmatched))
