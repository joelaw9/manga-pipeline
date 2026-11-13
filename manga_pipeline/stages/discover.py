"""Stage discover (SPEC "Chapter stages"). Stub: every *.cbz under paths.downloads is a chapter of the series named
by its parent folder (identity = the file stem); the stage itself accepts it. Phase 3 replaces it."""

import os
from pathlib import Path

from manga_pipeline import config, db, workflow


def scan(ctx):
    """Add each in-scope download not yet recorded as a chapter, creating its series (origin new) when unknown. A
    folder that cannot be read raises its OSError (workflow.share_errors decides whether the share is unreachable)."""
    downloads = config.abs_path(ctx.cfg["paths"]["downloads"])
    found = []
    # Not Path.rglob: it swallows the OSError of a folder it cannot read, which would hide an unreachable share.
    for folder, _, files in os.walk(downloads, onerror=_raise):
        found += [Path(folder, name) for name in files if Path(name).suffix == ".cbz"]
    found = [p for p in sorted(found) if workflow.in_scope(ctx.cfg, p.parent.name, None)]
    with db.transaction(ctx.conn):
        for path in found:
            if ctx.conn.execute("SELECT 1 FROM chapter WHERE download_path = ?", (str(path),)).fetchone():
                continue
            workflow.add_chapter(ctx.conn, _series_id(ctx.conn, path.parent.name), path.stem, str(path))


def run(ctx, chapter):
    return workflow.Done()


def _raise(error):
    raise error


def _series_id(conn, name):
    row = conn.execute("SELECT id FROM series WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO series (name, origin) VALUES (?, 'new') RETURNING id", (name,)).fetchone()[0]
