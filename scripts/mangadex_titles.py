"""Store every series' MangaDex English chapter list once (plan phase-10-r20-mangadex-titles; SPEC "Publishing").

The metadata check stores the list from now on, but a check comes round at `metadata.checks_per_run` series a run:
this stores it now for every in-scope series whose stored MangaDex record (`("mangadex", "manga")`, the last check's)
holds an id, fetched by that id (never an id resolved again here), through the check's own store
(`check.store_mangadex_list`), 1 transaction per series. Nothing else is written. Under lock.IDLE exclusive: no part
runs meanwhile. A series whose fetch fails is named and left as it was; run again with --series for those.

Usage, from the project root:
python -m scripts.mangadex_titles [--series ID...] [--config PATH]
A MangaDex that cannot be reached stops the run (the rest not tried).
Exit 0; 1 when a fetch failed or the run stopped; 2 a listed series out of scope or with no stored MangaDex id;
3 when a part runs.
"""

import argparse
import sys
from collections.abc import Sequence
from contextlib import closing

from manga_pipeline import config, db, lock, workflow
from manga_pipeline.clients import http
from manga_pipeline.db import sql
from manga_pipeline.errors import Unreachable
from manga_pipeline.metadata import check, sources
from manga_pipeline.metadata.records import STORED


class RefusedError(Exception):
    """A listed series cannot be stored (exit 2)."""


FAILURES = (Unreachable, http.HttpError, http.HttpTimeoutError, sources.SourceError)


def targets(cfg: config.Config, conn: db.Connection, series: Sequence[int] | None) -> list[tuple[int, str]]:
    """Return (series id, MangaDex id) of every in-scope series (or the listed ones) whose stored record holds an id."""
    source, kind = STORED["mangadex"]
    rows = conn.execute(
        sql.SQL("""SELECT s.id, f.payload ->> 'id' AS md FROM series s
        JOIN fetched f ON f.series_id = s.id AND f.source = %(source)s AND f.kind = %(kind)s
        WHERE {scope} AND (%(ids)s::bigint[] IS NULL OR s.id = ANY(%(ids)s)) AND f.payload ->> 'id' IS NOT NULL
        ORDER BY s.id""").format(scope=workflow.SCOPE),
        {
            **workflow.scope_params(cfg),
            "source": source,
            "kind": kind,
            "ids": None if series is None else list(series),
        },
    ).fetchall()
    return [(r["id"], r["md"]) for r in rows]


def run(cfg: config.Config, conn: db.Connection, mangadex: sources.MangaDex, series: Sequence[int] | None) -> list[str]:
    """Fetch and store each target's chapter list; return the summary lines (the failed series named)."""
    found = targets(cfg, conn, series)
    if missing := sorted(set(series or ()) - {s for s, _ in found}):
        msg = f"series out of scope or with no stored MangaDex id: {missing}"
        raise RefusedError(msg)
    stored, failed = 0, list[str]()
    for n, (series_id, md_id) in enumerate(found):
        try:
            chapters = mangadex.chapter_list(md_id)
        except Unreachable as e:  # the service is down for the run: every later fetch would fail at once
            failed.append(f"stopped at series {series_id}, {len(found) - n} not stored: {e}")
            break
        except FAILURES as e:
            failed.append(f"series {series_id}: {type(e).__name__}: {e}")
            continue
        with db.transaction(conn):
            check.store_mangadex_list(conn, series_id, chapters, workflow.now(conn))
        stored += 1
    return [f"series read {len(found)}, stored {stored}, failed {len(failed)}", *failed]


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description="Store every series' MangaDex English chapter list once.")
    parser.add_argument("--series", type=int, nargs="+", help="only these series (ids)")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, mangadex: sources.MangaDex | None = None) -> int:
    """Store the lists on the configured database; print the summary. mangadex: the client (default: a new one)."""
    args = parse(argv)
    cfg = config.init(args.config)
    with closing(db.open_database(cfg, builds=False)) as conn:
        try:
            with lock.held(conn, cfg, (), lock.EXCLUSIVE):
                lines = run(cfg, conn, mangadex or sources.MangaDex(), args.series)
        except lock.LockHeld as e:
            sys.stderr.write(f"error: {e}\n")
            return 3
        except RefusedError as e:
            sys.stderr.write(f"refused: {e}\n")
            return 2
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 1 if len(lines) > 1 else 0  # the summary line, then 1 per failed series


if __name__ == "__main__":
    sys.exit(main())
