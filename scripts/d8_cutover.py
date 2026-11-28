"""Delete the open junk candidates no review has seen (plan phase-10-8, D4; SPEC "Junk decisions", Candidates).

Before 10.8 a candidate's images had no position, no washing of removed ranges and no cited decisions. An open
candidate with no review (`reviews` 0, no verdict) is deleted, so that the chapters waiting on it are decided again
and open it again with the new images: `process` ends a junk_decision wait whose candidate is gone. A candidate a
review has seen (it counts its reviews, or it is `unsure`) is kept, and so is one listed by a needs_human chapter or a
chapter of a needs_human series (`process` ends no wait of either: deleted, it would leave a wait nothing ends until
a retry). No chapter's list is cleared and nothing else is written; the deleted rows' folders are removed by junk
review's cleanup once ORPHAN_KEEP_S old. Run it once, under `state pause`, after `state version --yes` and before the
resume. Under lock.IDLE exclusive: no part runs meanwhile; 1 transaction. --dry-run counts and writes nothing.

Usage, from the project root:
python -m scripts.d8_cutover [--dry-run] [--config PATH]
Exit 0; 3 when a part runs.
"""

import argparse
import sys
from contextlib import closing

from manga_pipeline import config, db, lock
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates

HELD = sql.SQL("""SELECT DISTINCT jsonb_array_elements(js.candidates::jsonb)::bigint FROM {} AND (
    c.status = 'needs_human' OR EXISTS (SELECT 1 FROM series s WHERE s.id = c.series_id AND s.status = 'needs_human')
)""").format(candidates.WAITING_SCANS)


def run(conn: db.Connection, *, dry_run: bool) -> list[str]:
    """Delete the open unreviewed candidates no held chapter lists, in 1 transaction; return the lines."""
    with db.transaction(conn):
        held = {r[0] for r in conn.execute(HELD)}
        ids = [
            r["id"]
            for r in candidates.open_rows(conn)
            if r["reviews"] == 0 and r["verdict"] is None and r["id"] not in held
        ]
        if not dry_run:
            conn.execute("DELETE FROM junk_candidate WHERE id = ANY(%s)", (ids,))
    return [f"open unreviewed candidates: {len(ids)}" + (" (dry run: nothing deleted)" if dry_run else " deleted")]


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Return the script's arguments."""
    parser = argparse.ArgumentParser(description="Delete the open junk candidates no review has seen.")
    parser.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Delete them on the configured database; print the count."""
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
