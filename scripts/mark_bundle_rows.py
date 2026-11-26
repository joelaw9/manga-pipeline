"""Mark the imported decisions of bulk-converter bundle rows (plan phase-10-5, step 2; SPEC "Junk decisions").

The bulk converter's rest-of-card bundle rows (review id "rn...") came over as plain piece decisions before the bundle
mark existed. An active piece decision whose source names an old row, `junk.sqlite3 junk row N` (the one-off store
import) or `record junk row N` (a record's), is marked when old row N in the bulk converter's store is active, has an
"rn" review id, and has the decision's series (none: global), origin `piece` and key text (`tiles|repr(aspect)`); a
row N that is an active "rn" row with another series, origin or key is a mismatch, counted and left unmarked. The
store is opened read only and immutable. Under lock.IDLE exclusive: no part runs meanwhile; 1 transaction.
--dry-run counts and writes nothing.

Usage, from the project root:
python -m scripts.mark_bundle_rows STORE [--dry-run] [--config PATH]
Exit 0; 2 when STORE is no file; 3 when a part runs.
"""

import argparse
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

from manga_pipeline import config, db, lock
from manga_pipeline.db import sql
from manga_pipeline.junk import importing, store

FORMS = ("junk.sqlite3", "record")  # a source's text before " junk row N": the store import's, a record's
ROW_INFIX = " junk row "

type OldRow = tuple[str, str, str]  # an old row's (series key, origin, key text)


def bundle_rows(path: Path) -> dict[int, OldRow]:
    """Return the bulk converter's active bundle rows by id: (series key ("" global), origin, key text)."""
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as old:
        rows: list[tuple[int, str | None, str, str, float]] = old.execute(
            "SELECT id, series, origin, tiles, aspect FROM junk WHERE superseded IS NULL AND review_id LIKE ?",
            (f"{importing.BUNDLE_PREFIX}%",),
        ).fetchall()
    return {i: (series or store.GLOBAL, origin, f"{tiles}|{aspect!r}") for i, series, origin, tiles, aspect in rows}


def old_row(source: str | None) -> tuple[str, int] | None:
    """Return (form, N) of a source naming an old row (`<form> junk row N`), else None."""
    form, infix, n = (source or "").rpartition(ROW_INFIX)
    return (form, int(n)) if infix and form in FORMS and n.isdigit() else None


def plan(conn: db.Connection, old: dict[int, OldRow]) -> tuple[list[int], Counter[str]]:
    """Return the ids of the decisions to mark, and the counts per form: rows, marked, mismatched, already marked."""
    rows = conn.execute(
        sql.SQL(
            "SELECT id, series_key, kind, key, source, bundle FROM junk_decision WHERE kind = 'piece' AND {}"
        ).format(store.ACTIVE)
    ).fetchall()
    marks: list[int] = []
    counts: Counter[str] = Counter()
    for r in rows:
        named = old_row(r["source"])
        if named is None:
            continue
        form, n = named
        counts[f"{form} rows"] += 1
        if n not in old:
            continue
        if old[n] != (r["series_key"], r["kind"], r["key"]):
            counts[f"{form} mismatched"] += 1
        elif r["bundle"]:
            counts[f"{form} marked already"] += 1
        else:
            counts[f"{form} to mark"] += 1
            marks.append(r["id"])
    return marks, counts


def run(conn: db.Connection, old: dict[int, OldRow], *, dry_run: bool) -> list[str]:
    """Mark the rows plan finds in 1 transaction (none with dry_run); return the lines to print."""
    with db.transaction(conn):
        marks, counts = plan(conn, old)
        marked = 0
        if not dry_run:
            marked = conn.execute(
                "UPDATE junk_decision SET bundle = true WHERE id = ANY(%s) AND NOT bundle", (marks,)
            ).rowcount
    lines = [f"{k}: {counts[k]}" for k in sorted(counts)]
    return [*lines, f"marked: {marked}" + (" (dry run)" if dry_run else "")]


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Return the script's arguments."""
    parser = argparse.ArgumentParser(description="Mark imported decisions of bulk-converter bundle rows.")
    parser.add_argument("store", type=Path, help="the bulk converter's junk.sqlite3")
    parser.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Mark the bundle rows on the configured database; print the counts."""
    args = parse(argv)
    if not args.store.is_file():
        sys.stderr.write(f"error: {args.store} is no file: the bulk converter's store is needed\n")
        return 2
    old = bundle_rows(args.store)
    cfg = config.init(args.config)
    with closing(db.open_database(cfg, builds=False)) as conn:
        try:
            with lock.held(conn, cfg, (), lock.EXCLUSIVE):
                lines = run(conn, old, dry_run=args.dry_run)
        except lock.LockHeld as e:
            sys.stderr.write(f"error: {e}\n")
            return 3
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
