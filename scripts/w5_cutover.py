"""Close the tail-piece candidates chapters wait on unbundled (plan phase-10-5, step 2; SPEC "Junk decisions").

Before the rest of card (W5), every tail piece was its own candidate. A chapter of a strip series at the junk stage,
waiting or needs_human, whose recorded candidates include a piece candidate has its recorded candidates and bundles
cleared: its old candidates close, and `process` ends its junk_decision wait (an empty record is a wait that is over)
and decides it again, recording its bundle; a needs_human chapter decides again on its retry. Nothing else is written:
no chapter status, no decision. Under lock.IDLE exclusive: no part runs meanwhile; 1 transaction. --dry-run counts and
writes nothing.

Usage, from the project root:
python -m scripts.w5_cutover [--dry-run] [--config PATH]
Exit 0; 3 when a part runs.
"""

import argparse
import sys
from contextlib import closing

from manga_pipeline import config, db, lock

CHAPTERS = """SELECT c.id, c.status FROM chapter c
    JOIN junk_scan js ON js.chapter_id = c.id
    JOIN series_conversion sc ON sc.series_id = c.series_id
    WHERE c.stage = 'junk' AND c.status IN ('waiting', 'needs_human') AND sc.layout = 'strip'
        AND EXISTS (SELECT 1 FROM junk_candidate k WHERE k.kind = 'piece' AND js.candidates::jsonb @> to_jsonb(k.id))
    ORDER BY c.id"""


def run(conn: db.Connection, *, dry_run: bool) -> list[str]:
    """Clear the chapters' recorded candidates and bundles in 1 transaction (none with dry_run); return the lines."""
    with db.transaction(conn):
        rows = conn.execute(CHAPTERS).fetchall()
        ids = [r["id"] for r in rows]
        if not dry_run:
            conn.execute("UPDATE junk_scan SET candidates = '[]', bundles = '[]' WHERE chapter_id = ANY(%s)", (ids,))
    by_status = {s: sum(r["status"] == s for r in rows) for s in ("waiting", "needs_human")}
    counts = ", ".join(f"{s} {n}" for s, n in by_status.items())
    return [f"chapters: {len(ids)} ({counts})" + (" (dry run: nothing cleared)" if dry_run else " cleared")]


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Return the script's arguments."""
    parser = argparse.ArgumentParser(description="Clear the unbundled tail-piece candidates chapters wait on.")
    parser.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Clear them on the configured database; print the count."""
    args = parse(argv)
    cfg = config.init(args.config)
    with closing(db.open_database(cfg, builds=False)) as conn:
        try:
            with lock.held(conn, cfg, (), lock.EXCLUSIVE):
                lines = run(conn, dry_run=args.dry_run)
        except lock.LockHeld as e:
            sys.stderr.write(f"error: {e}\n")
            return 3
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
