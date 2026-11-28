"""Record the original of each pipeline file published before originals were recorded (plan phase-10-9, step 1).

SPEC "Publishing": a published file's row records where its download went. Confirm records it from now on; this fills
it once for each confirmed file of origin `pipeline` whose `original` is null, from its download chapter: the 1 done
download chapter of its series, not dropped, whose download's stem is the file's `source_name` (a rewrite may have
renamed the file since; its download's name does not change). The chapter's download is looked for where the
retention way put it, by its content digest (`retention.find`). Under lock.IDLE exclusive, 1 transaction per
BATCH files; nothing else is written. A file with no such chapter or more than 1, or whose download is not found, is
listed, never guessed.

Usage, from the project root:
python -m scripts.record_originals [--dry-run] [--config PATH]
Exit 0; 1 when a file is listed; 3 when a part runs.
"""

import argparse
import sys
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import PurePosixPath

from manga_pipeline import config, db, lock, retention, workflow

BATCH = 500


def last_downloads(conn: db.Connection, series_id: int) -> dict[str, list[db.Row]]:
    """Return the series' done, undropped download chapters by their download's stem."""
    rows = conn.execute(
        """SELECT id, download_path, content_digest FROM chapter
        WHERE series_id = %s AND kind = 'download' AND status = 'done' AND download_path IS NOT NULL
            AND content_digest IS NOT NULL AND coalesce(last_error, '') NOT LIKE 'dropped%%'
        ORDER BY id""",
        (series_id,),
    ).fetchall()
    by_stem: dict[str, list[db.Row]] = {}
    for r in rows:
        by_stem.setdefault(PurePosixPath(r["download_path"]).stem, []).append(r)
    return by_stem


def originals(cfg: config.Config, conn: db.Connection, *, write: bool) -> tuple[int, list[str]]:
    """Find the original of each confirmed pipeline file with none; return (found, [listed file: why]).

    write: record them, BATCH files a transaction as they are found, so an interrupted run keeps what it recorded.
    """
    files = conn.execute(
        """SELECT id, series_id, library_path, source_name FROM published_file
        WHERE origin = 'pipeline' AND state = 'confirmed' AND original IS NULL ORDER BY series_id, id"""
    ).fetchall()
    found, batch, listed = 0, list[tuple[str, int]](), list[str]()
    chapters, series = dict[str, list[db.Row]](), None
    for file in files:
        if file["series_id"] != series:
            series, chapters = file["series_id"], last_downloads(conn, file["series_id"])
        named = chapters.get(file["source_name"], [])
        if len(named) != 1:  # none, or 2 (a given-up copy of the same name is done and not dropped): never guessed
            ids = [c["id"] for c in named]
            listed.append(
                f"{file['library_path']}: {len(named)} done download chapters named {file['source_name']!r} {ids}"
            )
            continue
        chapter = named[0]
        with workflow.share_errors():
            kept = retention.find(cfg, config.to_local(cfg, chapter["download_path"]), chapter["content_digest"])
        if kept is None:
            listed.append(f"{file['library_path']}: chapter {chapter['id']}'s download is not in retention")
            continue
        found += 1
        batch.append((config.to_stored(cfg, kept), file["id"]))
        if write and len(batch) == BATCH:
            record(conn, batch)
            batch = []
    if write:
        record(conn, batch)
    return found, listed


def record(conn: db.Connection, batch: Sequence[tuple[str, int]]) -> None:
    """Record a batch of found originals in 1 transaction."""
    with db.transaction(conn):
        db.executemany(conn, "UPDATE published_file SET original = %s WHERE id = %s AND original IS NULL", batch)


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Record the originals of pipeline files published before.")
    parser.add_argument("--dry-run", action="store_true", help="count only")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Record the originals on the configured database; print the summary and each listed file."""
    args = parse(argv)
    cfg = config.init(args.config)
    start = time.monotonic()
    with closing(db.open_database(cfg, builds=False)) as conn:
        try:
            with lock.held(conn, cfg, (), lock.EXCLUSIVE):
                found, listed = originals(cfg, conn, write=not args.dry_run)
        except lock.LockHeld as e:
            sys.stderr.write(f"error: {e}\n")
            return 3
    verb = "found" if args.dry_run else "recorded"
    lines = [f"files {verb} {found}, listed {len(listed)}, in {time.monotonic() - start:.0f} s", *listed]
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 1 if listed else 0


if __name__ == "__main__":
    sys.exit(main())
