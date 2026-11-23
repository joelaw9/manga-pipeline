"""Move series folders (agent-workspace/plans/fix-new-series-source-folder.md, step 1; SPEC "Publishing").

Each series' folder moves to <library root>/<parent>/<its folder name>: parent is --to-parent ("" = the library root,
else a folder directly under it), else its source folder (metadata.admit.source_folder; Suwayomi's sources listed
once, under the lock). Under lock.IDLE exclusive: no part runs meanwhile. Every series is refused when the library
root is not there or --to-parent names no folder under it. Per series, in this order: refused when it is not a new
series, or while a chapter is claimed or not done at publish or confirm, or a published file is rewrite.BUSY; refused
when it has no target. Then, so rows and disk never disagree: already there only when its folder holds its published
files; rows only when a rename was not committed (old folder gone, the target holding exactly its published files, of
which it has some); refused when the target exists otherwise; with published files, a rename of its old folder,
refused when that is missing or does not hold them all; without, rows only
when its old folder is absent or holds nothing (an empty one removed), else a rename. A rename is followed by 1
transaction rewriting series.folder, published_file.library_path and chapter.library_path, and undone when the
transaction's writes fail.
--dry-run prints what each series would get and writes nothing. No Kavita scan: the person requests it.

Usage, from the project root:
python -m scripts.move_series_folder SERIES_ID... [--to-parent FOLDER] [--dry-run] [--config PATH]
Exit 0; 2 when a series was refused; 3 when a part runs.
"""

import argparse
import sys
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from manga_pipeline import config, db, errors, lock, rewrite
from manga_pipeline.clients import suwayomi
from manga_pipeline.db import sql
from manga_pipeline.metadata import admit
from manga_pipeline.replies.suwayomi import Source

# What a move does: its rows only (a rename not committed; no files), or the rename and its rows
THERE, RESUME, ROWS, RENAME = "already there", "rows only (resumed)", "rows only (no files)", "moved"


class MoveRefusedError(Exception):
    """The series cannot be moved now; the message says why."""


@dataclass(frozen=True)
class Move:
    """A series' move: its old and new folder (stored form) and what it does (THERE, RESUME, ROWS or RENAME)."""

    series_id: int
    old: str
    new: str
    act: str

    def line(self) -> str:
        """Return the move as the script prints it."""
        return f"series {self.series_id}: {self.old} -> {self.new}: {self.act}"


def decide(
    cfg: config.Config, conn: db.Connection, series_id: int, parent: str | None, sources: Sequence[Source]
) -> Move:
    """Return the series' move (see the module docstring), reading only; MoveRefusedError when it may not move now.

    parent: the folder under the library root ("" = the root), None for the series' source folder (from sources).
    """
    series = conn.execute("SELECT name, origin, folder FROM series WHERE id = %s", (series_id,)).fetchone()
    if series is None:
        msg = "no such series"
        raise MoveRefusedError(msg)
    if series["origin"] != "new":  # a mistyped id never moves an established library series
        msg = f"{series['name']} is not a new series"
        raise MoveRefusedError(msg)
    if series["folder"] is None:
        msg = f"{series['name']} has no folder"
        raise MoveRefusedError(msg)
    claimed, busy = db.one_row(
        conn,
        sql.SQL("""SELECT
            EXISTS (SELECT 1 FROM chapter WHERE series_id = %(id)s
                AND (status = 'claimed' OR (stage IN ('publish', 'confirm') AND status <> 'done'))),
            EXISTS (SELECT 1 FROM published_file p WHERE p.series_id = %(id)s AND {busy})""").format(
            busy=rewrite.BUSY.format(p=sql.Identifier("p"))
        ),
        {"id": series_id},
    )
    if claimed:
        msg = "a chapter is claimed, or on its way through publish or confirm"
        raise MoveRefusedError(msg)
    if busy:
        msg = "a published file is busy (a rewrite, a copy or a wait for Kavita)"
        raise MoveRefusedError(msg)
    if parent is None:
        parent, why = admit.source_folder(conn, series_id, sources)
        if parent is None:
            raise MoveRefusedError(why)
    try:
        new = config.to_stored(
            cfg, config.abs_path(cfg["paths"]["library"]) / parent / PurePosixPath(series["folder"]).name
        )
    except config.PathError as e:
        raise MoveRefusedError(str(e)) from e
    return Move(series_id, series["folder"], new, _act(cfg, conn, series_id, series["folder"], new))


def _act(cfg: config.Config, conn: db.Connection, series_id: int, old_stored: str, new_stored: str) -> str:
    """Return what the move does, by its folders on the share; MoveRefusedError when rows and disk would disagree.

    A series with published files moves only a folder that holds them (or resumes a rename that did); one without
    moves its rows alone when its old folder holds nothing.
    """
    old, new = config.to_local(cfg, old_stored), config.to_local(cfg, new_stored)
    published = {
        PurePosixPath(r[0]).name
        for r in conn.execute("SELECT library_path FROM published_file WHERE series_id = %s", (series_id,))
    }
    held = f"{old_stored}: the folder does not hold its published files"
    if old_stored == new_stored:
        if published and not (errors.exists(old) and published <= _names(old)):
            raise MoveRefusedError(held)
        return THERE
    if errors.exists(new):
        if published and not errors.exists(old) and _names(new) == published:
            return RESUME
        msg = f"{old_stored} -> {new_stored}: the target exists"
        raise MoveRefusedError(msg)
    if errors.exists(old):
        if published and not published <= _names(old):
            raise MoveRefusedError(held)
        return RENAME if published or any(old.iterdir()) else ROWS
    if published:
        msg = f"{old_stored} is missing, yet the series has published files"
        raise MoveRefusedError(msg)
    return ROWS


def _names(folder: Path) -> set[str]:
    """Return the names in a folder."""
    return {p.name for p in folder.iterdir()}


def apply(cfg: config.Config, conn: db.Connection, move: Move) -> None:
    """Carry out a move decide returned: the rename (undone when the rows are not written), then the rows."""
    if move.act == THERE:
        return
    old, new = config.to_local(cfg, move.old), config.to_local(cfg, move.new)
    if move.act == ROWS and errors.exists(old):
        old.rmdir()
    if move.act != RENAME:
        with db.transaction(conn):
            _write_rows(conn, move)
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    with db.transaction(conn):
        try:
            _write_rows(conn, move)
        except BaseException:
            # Raised inside the transaction: it rolls back, the rows still name the old folder, so the folder goes
            # back. A failure at the commit itself is not caught here: whether it committed is unknown, the folder
            # stays renamed, and a rerun resumes (rows only) or refuses.
            new.rename(old)
            raise


def _write_rows(conn: db.Connection, move: Move) -> None:
    """Write the move's rows (caller's transaction): the series' folder, and each library path under the old one."""
    params = {"id": move.series_id, "old": move.old, "new": move.new}
    conn.execute("UPDATE series SET folder = %(new)s WHERE id = %(id)s", params)
    for table in ("published_file", "chapter"):
        conn.execute(
            sql.SQL("""UPDATE {table} SET library_path = %(new)s || substr(library_path, length(%(old)s) + 1)
            WHERE series_id = %(id)s AND starts_with(library_path, %(old)s || '/')""").format(
                table=sql.Identifier(table)
            ),
            params,
        )


def _run_problem(cfg: config.Config, parent: str | None) -> str | None:
    """Why no series may move in this run, or None: the library root is not there, or --to-parent names no folder."""
    library = config.abs_path(cfg["paths"]["library"])
    if not errors.exists(library):
        return f"the library folder {library} is not there (share not mounted?)"
    if parent and parent not in {p.name for p in library.iterdir() if p.is_dir()}:
        return f"--to-parent {parent!r} is not a folder directly under the library root"
    return None


def run(cfg: config.Config, conn: db.Connection, args: argparse.Namespace) -> bool:
    """Decide, and unless args.dry_run carry out, each series' move, printing 1 line each; return whether any refused.

    Refused at once, every series, when the library root is not there or --to-parent names no folder in it; only then
    are Suwayomi's sources listed, when no --to-parent is given. A move that fails raises: its rename is undone when
    its rows were not written, and the series after it are not tried.
    """
    problem = _run_problem(cfg, args.to_parent)
    if problem:
        sys.stderr.write(f"error: {problem}\n")
        return True
    sources = suwayomi.from_config(cfg).sources() if args.to_parent is None else []
    refused = False
    for series_id in args.series_ids:
        try:
            move = decide(cfg, conn, series_id, args.to_parent, sources)
        except MoveRefusedError as e:
            sys.stdout.write(f"series {series_id}: refused: {e}\n")
            refused = True
            continue
        if not args.dry_run:
            apply(cfg, conn, move)
        sys.stdout.write(f"{move.line()}\n")
    return refused


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Return the script's arguments."""
    parser = argparse.ArgumentParser(description="Move series folders into their source folder (or --to-parent).")
    parser.add_argument("series_ids", metavar="SERIES_ID", type=int, nargs="+")
    parser.add_argument("--to-parent", help='the folder under the library root to move into ("" = the root)')
    parser.add_argument("--dry-run", action="store_true", help="print each series' move only; write nothing")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Move the series' folders on the configured database and share; print 1 line per series."""
    args = parse(argv)
    cfg = config.init(args.config)
    with closing(db.open_database(cfg, builds=False)) as conn:
        try:
            with lock.held(conn, cfg, (), lock.EXCLUSIVE):
                refused = run(cfg, conn, args)
        except lock.LockHeld as e:
            sys.stderr.write(f"error: {e}\n")
            return 3
    return 2 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
