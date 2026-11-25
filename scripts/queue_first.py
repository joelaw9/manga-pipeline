"""Front-load adopted files' conversions (agent-workspace/plans/phase-9c-library-conversion.md, D-8; SPEC "State").

The program queues conversions by series id, then number; this script, run by hand or before each intake run during
the backfill, fills the room the cap has first with the series of the API key's user's Kavita collection titled
"Reading" (case ignored), then with the in-scope series that have an open download (needs_human included), through
the program's own queue (conversions.queue). It takes no lock and never touches the share: the queue's transactions
re-check each file and the cap.

Exit 1 when Kavita cannot be reached or refuses the key, or when not exactly 1 collection is titled "Reading"; exit 2
with no database built.

Usage, from the project root: python -m scripts.queue_first [--config PATH]
"""

import argparse
import sys
from contextlib import closing

from manga_pipeline import config, conversions, db, logs, workflow
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.db import sql
from manga_pipeline.errors import Unreachable

READING = "reading"  # the collection's title, compared case-folded


class ReadingError(Exception):
    """Not exactly 1 of the API key's user's Kavita collections is titled "Reading"."""


def reading(kavita: kavita_client.Kavita) -> list[int]:
    """Return the Kavita series ids of the collection titled "Reading" (case ignored); ReadingError unless 1 is."""
    found = [c for c in kavita.collections() if c["title"].casefold() == READING]
    if len(found) != 1:
        msg = f'{len(found)} Kavita collections are titled "Reading" (case ignored): exactly 1 must be'
        raise ReadingError(msg)
    return [s["id"] for s in kavita.collection_series(found[0]["id"])]


def front_load(cfg: config.Config, conn: db.Connection, kavita: kavita_client.Kavita) -> list[str]:
    """Queue the Reading series' conversions, then the open-download series'; return the lines to print."""
    kavita_ids = reading(kavita)
    known = {
        r["kavita_series_id"]: r
        for r in conn.execute("SELECT * FROM series WHERE kavita_series_id = ANY(%s::bigint[])", (kavita_ids,))
    }
    lines: list[str] = []
    order: list[int] = []
    for kid in kavita_ids:
        row = known.get(kid)
        if row is None:
            lines.append(f"Reading: Kavita series {kid} skipped: no series in the database")
        elif not workflow.row_in_scope(cfg, row):
            lines.append(f"Reading: series {row['id']} ({row['name']}) skipped: out of scope")
        elif row["status"] != "ok":
            lines.append(f"Reading: series {row['id']} ({row['name']}) skipped: needs_human")
        elif row["id"] not in order:
            order.append(row["id"])
    downloads = conn.execute(
        sql.SQL("""SELECT DISTINCT s.id FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.kind = 'download' AND c.status <> 'done' AND NOT {adopted} AND {scope} ORDER BY s.id""").format(
            adopted=workflow.REPLACES_ADOPTED.format(c=sql.Identifier("c")), scope=workflow.SCOPE
        ),
        workflow.scope_params(cfg),
    )
    order += [r["id"] for r in downloads if r["id"] not in order]
    done = conversions.queue(cfg, conn, series=order)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM series WHERE id = ANY(%s)", (order,))}
    lines += [
        f"series {sid} ({names[sid]}): {done.queued[sid]} queued, {done.left[sid]} left (the cap is full)"
        for sid in order
        if done.queued[sid] or done.left[sid]
    ]
    lines.append(f"{done.queued.total()} queued over {len(order)} series; {done.left.total()} left (the cap is full)")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Front-load the conversions on the configured database and Kavita; print what was queued."""
    parser = argparse.ArgumentParser(description="Queue the Reading and open-download series' conversions first.")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    args = parser.parse_args(argv)
    cfg = config.init(args.config)
    logs.setup(cfg, None)
    if not db.built(cfg):
        sys.stderr.write(f"no database {cfg['database']['name']} built yet: nothing to queue\n")
        return 2
    try:
        with closing(db.connect(cfg)) as conn:
            lines = front_load(cfg, conn, kavita_client.from_config(cfg))
    except (Unreachable, ReadingError) as e:
        sys.stderr.write(f"queue_first: {e}\n")
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
