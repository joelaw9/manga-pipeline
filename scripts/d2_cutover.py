"""Decide again the chapters waiting on junk candidates (plan phase-10-6, step 3; SPEC "Junk decisions", Candidates).

Before PR 3 a unit of a series' first 3 chapters was a candidate for that alone (the first-3 rule, removed by D2). A
chapter at the junk stage waiting on junk_decision has its recorded candidates and bundles cleared: `process` ends its
wait (an empty record is a wait that is over) and decides it again from its stored scan, so a candidate it waited on
only by the first-3 rule is no longer waited on; a candidate with another reason is offered again. A needs_human
chapter is left alone: its retry decides it again. The junk refill is not changed: it records the first decide after a
scan only. Nothing else is written: no chapter status, no decision. Run it once after PR 3 is deployed, under
`state pause`. Under lock.IDLE exclusive: no part runs meanwhile; 1 transaction. --dry-run counts and writes nothing.

Usage, from the project root:
python -m scripts.d2_cutover [--dry-run] [--config PATH]
Exit 0; 3 when a part runs.
"""

import argparse
import sys
from contextlib import closing

from manga_pipeline import config, db, lock

CHAPTERS = """SELECT c.id FROM chapter c JOIN junk_scan js ON js.chapter_id = c.id
    WHERE c.stage = 'junk' AND c.status = 'waiting' AND c.waiting_on = 'junk_decision'
    ORDER BY c.id"""


def run(conn: db.Connection, *, dry_run: bool) -> list[str]:
    """Clear the chapters' recorded candidates and bundles in 1 transaction (none with dry_run); return the lines."""
    with db.transaction(conn):
        ids = [r["id"] for r in conn.execute(CHAPTERS).fetchall()]
        if not dry_run:
            conn.execute("UPDATE junk_scan SET candidates = '[]', bundles = '[]' WHERE chapter_id = ANY(%s)", (ids,))
    return [
        f"chapters waiting on junk candidates: {len(ids)}" + (" (dry run: nothing cleared)" if dry_run else " cleared")
    ]


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Return the script's arguments."""
    parser = argparse.ArgumentParser(description="Decide again the chapters waiting on junk candidates.")
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
