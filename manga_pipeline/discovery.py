"""Discovery (SPEC "Suwayomi", "Series identification"): `intake` lists the downloads, maps each to its series (by its
library manga, else as a hand-placed folder) and to its Suwayomi chapter when it has one, and records each settled one
as a chapter at discover. Opens no CBZ but to identify a hand-placed folder by its ComicInfo Series.

What cannot be taken is collected and, once the listing is complete, replaces unmatched_download in 1 transaction.
"""

import logging
import os
import time
from pathlib import Path

from manga_pipeline import config, db, identify, names, workflow
from manga_pipeline.clients import suwayomi
from manga_pipeline.metadata import check

log = logging.getLogger(__name__)


def scan(ctx):
    """Record each new settled download as a chapter. A manga folder that cannot be listed is not taken (`_list`);
    any other folder that cannot be read raises its OSError (the caller
    decides whether the share is unreachable); an unreachable service raises Unreachable before anything is written
    but the series rows identification settled."""
    root = config.abs_path(ctx.cfg["paths"]["downloads"]) / suwayomi.MANGAS_FOLDER
    held = _held(ctx)
    listed, unlistable = _list(root)
    settled_by = time.time() - ctx.cfg["intake"]["settle_minutes"] * 60
    folders = {}
    for key, files in listed.items():
        files = [
            n
            for n in files
            if config.to_stored(ctx.cfg, root / key[0] / key[1] / n) not in held
            and _settled(root / key[0] / key[1] / n, settled_by)
        ]
        if files:
            folders[key] = files
    unmatched, found = list(unlistable), []
    if folders:
        by_folder = {}
        for manga in ctx.suwayomi.library_manga():
            key = suwayomi.download_folders(manga)
            if key is not None:  # no extension installed: no folder name to rebuild
                by_folder.setdefault(key, []).append(manga)
        kavita_series = identify.KavitaSeries(ctx)
        for key, files in sorted(folders.items()):
            paths = [root / key[0] / key[1] / name for name in files]
            mangas = by_folder.get(key, [])
            if len(mangas) > 1:
                unmatched += [(p, f"{len(mangas)} library manga map to its folder") for p in paths]
                continue
            if mangas:
                unmatched_here, found_here = _manga_downloads(ctx, mangas[0], paths, kavita_series)
            else:
                unmatched_here, found_here = _folder_downloads(ctx, f"{key[0]}/{key[1]}", paths, kavita_series)
            unmatched += unmatched_here
            found += found_here
    _record(ctx, found, unmatched)


def _settled(path, settled_by):
    """Whether the file was last modified at or before settled_by (SPEC "Suwayomi": a file still being written is
    skipped); a file gone since the listing is not."""
    try:
        return path.stat().st_mtime <= settled_by
    except FileNotFoundError as e:
        if workflow.dropped_share(e):
            raise
        return False


def _held(ctx):
    """The paths open chapters hold: their downloads, the re-downloads they expect and the rejected copies they are
    still removing (SPEC "Bad downloads"). Such a file is never a new chapter and never listed as not taken."""
    rows = ctx.conn.execute(
        """SELECT download_path FROM chapter WHERE status != 'done' AND download_path IS NOT NULL
        UNION SELECT redownload::jsonb #>> '{expected,path}' FROM chapter
        WHERE status != 'done' AND redownload::jsonb #>> '{expected,path}' IS NOT NULL
        UNION SELECT r.value ->> 'path' FROM chapter
            CROSS JOIN LATERAL jsonb_array_elements(chapter.redownload::jsonb -> 'remove') r
        WHERE chapter.status != 'done'"""
    )
    return {row[0] for row in rows}


def _list(root):
    """({(source folder, manga folder): [CBZ file names]}, [(manga folder path, reason)]) under <downloads>/mangas.
    A manga folder that cannot be listed (a name Samba cannot show Windows, e.g. one holding "?") is not taken, and the
    rest is listed; a manga folder gone since its source was listed is skipped. Any other folder that cannot be read
    raises, as does a manga folder's share error (SHARE_ERRNOS) or any error once the root itself cannot be read.
    Never Path.glob: it swallows the OSError of a folder it cannot read."""
    folders, unlistable = {}, []
    for source in _entries(root, dirs=True):
        for title in _entries(Path(root, source), dirs=True):
            folder = Path(root, source, title)
            try:
                entries = _entries(folder, dirs=False)
            except FileNotFoundError as e:  # removed since its source was listed (a confirm's delete): nothing to take
                if workflow.dropped_share(e):
                    raise
                continue
            except OSError as e:
                if e.errno in workflow.SHARE_ERRNOS:
                    raise
                # ponytail: Windows may list the root from its SMB cache while the share fails, and WinError 59 maps
                # to errno 22, not a SHARE_ERRNOS one; such a failure is listed, not ended on, until the next run
                _entries(root, dirs=True)
                unlistable.append((folder, f"its folder cannot be listed: {e.strerror or e}"))
                continue
            files = sorted(n for n in entries if n.lower().endswith(".cbz"))
            if files:
                folders[(source, title)] = files
    return folders, unlistable


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
        if len(hits) > 1:
            why = f"{len(hits)} chapters of Suwayomi manga {manga['id']} rebuild to this file name"
            unmatched.append((path, why))
        elif hits:
            found.append(_found(ctx, series, path, hits[0], manga["source"]["displayName"]))
        else:  # no chapter rebuilds to it: a hand-placed chapter of the manga's series
            found.append(_found(ctx, series, path, None, None))
    return unmatched, found


def _folder_downloads(ctx, folder, paths, kavita_series):
    """([(path, reason)] not taken, [new chapter fields]) for a hand-placed folder's downloads."""
    series = identify.series_for_folder(ctx, folder, paths, kavita_series)
    if isinstance(series, identify.NotTaken):
        return ([(p, series.reason) for p in paths] if series.listed else []), []
    return [], [_found(ctx, series, path, None, None) for path in paths]


def _found(ctx, series, path, chapter, source):
    """chapter: the Suwayomi chapter the file rebuilds to, None for a hand-placed one."""
    identity = names.identity(names.parse(path.stem))
    chapter_id, url = (chapter["id"], chapter["realUrl"] or None) if chapter else (None, None)
    return series["id"], identity, config.to_stored(ctx.cfg, path), chapter_id, source, url


def _record(ctx, found, unmatched):
    """Add the chapters not yet open at their path, and replace unmatched_download (first seen kept)."""
    stamp = workflow.now(ctx.conn)
    with db.transaction(ctx.conn):
        added = 0
        for series_id, identity, path, chapter_id, source, url in found:
            open_ = ctx.conn.execute(
                "SELECT 1 FROM chapter WHERE download_path = %s AND status != 'done'", (path,)
            ).fetchone()
            # Checked after the row: confirm moves a download away before its chapter is done, so a file still here
            # once its chapter reads done is a new download, not one an export confirmed since the listing.
            if not open_ and workflow.exists(config.to_local(ctx.cfg, path)):
                workflow.add_chapter(ctx.conn, series_id, identity, path, chapter_id, source, url)
                check.make_urgent(ctx.conn, series_id)  # a new chapter makes its series' check due at once
                added += 1
        seen = dict(ctx.conn.execute("SELECT path, first_seen FROM unmatched_download").fetchall())
        ctx.conn.execute("DELETE FROM unmatched_download")
        db.executemany(
            ctx.conn,
            "INSERT INTO unmatched_download (path, reason, first_seen) VALUES (%s, %s, %s)",
            [(s, why, seen.get(s, stamp)) for p, why in unmatched for s in [config.to_stored(ctx.cfg, p)]],
        )
    if added:
        log.info("%s: discovered %d new download(s)", ctx.part, added)
    if unmatched:
        log.info("%s: %d download(s) not taken (see status)", ctx.part, len(unmatched))
