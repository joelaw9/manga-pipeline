"""Mark the imported decisions of bulk-converter bundle rows (plan phase-10-5, step 2; SPEC "Junk decisions").

The bulk converter's rest-of-card bundle rows (review id "rn...") came over as plain piece decisions before the bundle
mark existed. Run it after the deploy has applied schema step 0078 (after `state version --yes` or the first locked
command): it needs the `bundle` column.

An active piece decision is traced to its import: through `redecided decision X` sources (junk redecide carries the
mark) to a source naming an old row, `junk.sqlite3 junk row N` (the one-off store import) or `record junk row N` (a
record's). It is marked when old row N in the bulk converter's store is active, has an "rn" review id, and has the
import's series (none: global), origin `piece` and key text (`tiles|repr(aspect)`); a row N that is an active "rn" row
with another series, origin or key is a mismatch, counted and left unmarked. An "rn" keep whose key (series or global,
origin, key text) also has an active keep of another review id is left unmarked and listed: the old judged_keep
counted that keep, so the key was judged. The store is opened read only and immutable. Under lock.IDLE exclusive: no
part runs meanwhile; 1 transaction. --dry-run counts and lists and writes nothing.

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
from typing import NamedTuple

from manga_pipeline import config, db, lock
from manga_pipeline.junk import importing, store

FORMS = ("junk.sqlite3", "record")  # a source's text before " junk row N": the store import's, a record's
ROW_INFIX = " junk row "
REDECIDED = "redecided decision "  # junk redecide's source: the decision it replaced

type Key = tuple[str, str, str]  # an old row's (series key ("" global), origin, key text)


class OldStore(NamedTuple):
    """The bulk converter's active "rn" rows by id (their key, and whether a keep), and the keys of its other keeps."""

    bundles: dict[int, tuple[Key, bool]]
    judged: set[Key]


def old_store(path: Path) -> OldStore:
    """Read the bulk converter's store (read only, immutable): its active "rn" rows, and the keys of its other keeps."""
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as old:
        rows: list[tuple[int, str | None, str, int | None, str, str, float]] = old.execute(
            """SELECT id, series, decision, review_id GLOB ?, origin, tiles, aspect FROM junk
            WHERE superseded IS NULL""",
            (f"{importing.BUNDLE_PREFIX}*",),  # GLOB: case-sensitive, as the bulk converter's prefix test
        ).fetchall()
    bundles: dict[int, tuple[Key, bool]] = {}
    judged: set[Key] = set()
    for i, series, decision, rn, origin, tiles, aspect in rows:
        k = (series or store.GLOBAL, origin, f"{tiles}|{aspect!r}")
        if rn:  # NULL for no review id: not a bundle's
            bundles[i] = (k, decision == "keep")
        elif decision == "keep":
            judged.add(k)
    return OldStore(bundles, judged)


def old_row(source: str | None) -> tuple[str, int] | None:
    """Return (form, N) of a source naming an old row (`<form> junk row N`), else None."""
    form, infix, n = (source or "").rpartition(ROW_INFIX)
    return (form, int(n)) if infix and form in FORMS and n.isdigit() else None


def imported_from(row: db.Row, by_id: dict[int, db.Row]) -> tuple[db.Row, bool]:
    """Return the decision a row was redecided from, through every redecide (itself when none), and whether traced."""
    traced = False
    while (row["source"] or "").startswith(REDECIDED):
        row, traced = by_id[int(row["source"].removeprefix(REDECIDED))], True
    return row, traced


def plan(conn: db.Connection, old: OldStore) -> tuple[list[int], Counter[str], list[str]]:
    """Return the ids of the decisions to mark, the counts per form, and the keys left unmarked as judged."""
    rows = conn.execute("SELECT * FROM junk_decision WHERE kind = 'piece' ORDER BY id").fetchall()
    by_id = {r["id"]: r for r in rows}
    marks: list[int] = []
    counts: Counter[str] = Counter()
    judged: list[str] = []
    for r in rows:
        if not store.is_active(r):
            continue
        root, traced = imported_from(r, by_id)
        named = old_row(root["source"])
        if named is None:
            continue
        form = named[0] + (" via redecide" if traced else "")
        counts[f"{form} rows"] += 1
        if named[1] not in old.bundles:
            continue
        k, keep = old.bundles[named[1]]
        if k != (root["series_key"], root["kind"], root["key"]):
            counts[f"{form} mismatched"] += 1
        elif keep and (k in old.judged or (store.GLOBAL, *k[1:]) in old.judged):
            counts[f"{form} judged by another keep"] += 1
            judged.append(f"judged by another keep: decision {r['id']} ({k[0] or 'global'}, {k[2]})")
        elif r["bundle"]:
            counts[f"{form} marked already"] += 1
        else:
            counts[f"{form} to mark"] += 1
            marks.append(r["id"])
    return marks, counts, judged


def run(conn: db.Connection, old: OldStore, *, dry_run: bool) -> list[str]:
    """Mark the rows plan finds in 1 transaction (none with dry_run); return the lines to print."""
    with db.transaction(conn):
        marks, counts, judged = plan(conn, old)
        marked = 0
        if not dry_run:
            marked = conn.execute(
                "UPDATE junk_decision SET bundle = true WHERE id = ANY(%s) AND NOT bundle", (marks,)
            ).rowcount
    lines = [f"{k}: {counts[k]}" for k in sorted(counts)]
    return [*lines, *judged, f"marked: {marked}" + (" (dry run)" if dry_run else "")]


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
    old = old_store(args.store)
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
