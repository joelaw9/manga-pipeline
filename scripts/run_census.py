"""The split-chapter run census (agent-workspace/plans/phase-10-3-split-chapters.md, step 1 and the 10.18 pilot).

Read only, no lock: a plain connection whose session is read only, as scripts/id_replay.py. Over every series'
published files, `parts.runs` with the origin test off (every run the rule sees, whatever its files' origin) and with
it on (the runs a join takes today), plus the part numbers beside a whole N that no join takes (SPEC "Split chapters":
P7/P9, by the whole file's origin). Prints the report as JSON.

Usage, from the project root: python -m scripts.run_census [--config PATH]
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from typing import TypedDict

from manga_pipeline import config, db, names
from manga_pipeline.metadata import parts


class Report(TypedDict):
    """The census: runs (origin test off) and their series, the runs a join takes today, parts beside a whole N."""

    runs: int
    series: int
    joinable_now: int
    beside_whole: dict[str, int]
    list: list[tuple[int, str | None, int, int]]


def census(files: Mapping[int, Sequence[parts.File]]) -> Report:
    """Return the census of the series' files ({series id: its published files})."""
    every: list[tuple[int, str | None, int, int]] = []
    now = 0
    beside: Counter[str] = Counter()
    for sid, series_files in sorted(files.items()):
        every += [(sid, r.season, r.whole, r.top) for r in parts.runs(series_files, any_origin=True)]
        now += len(parts.runs(series_files))
        beside.update(_beside_whole(series_files))
    return Report(
        runs=len(every),
        series=len({sid for sid, *_ in every}),
        joinable_now=now,
        beside_whole=dict(beside),
        list=every,
    )


def _beside_whole(files: Iterable[parts.File]) -> Counter[str]:
    """Count the chapters N of 1 series with a whole file N that is no joined file and part files N.j, by its origin."""
    wholes: dict[tuple[str | None, int], str] = {}
    with_parts: set[tuple[str | None, int]] = set()
    for f in files:
        key = None if f.side else names.number_key(f.number)
        if key is None:
            continue
        if not key[1]:
            if f.joined is None:
                wholes[f.season, key[0]] = f.origin
        else:
            with_parts.add((f.season, key[0]))
    return Counter(origin for n, origin in wholes.items() if n in with_parts)


def read(conn: db.Connection) -> dict[int, list[parts.File]]:
    """Return every series' published files as the rule reads them."""
    out: dict[int, list[parts.File]] = {}
    for r in conn.execute(
        "SELECT id, series_id, identity, comicinfo_volume, origin, state, joined_parts FROM published_file ORDER BY id"
    ):
        _, season, side, number, _ = names.read_identity(r["identity"])
        out.setdefault(r["series_id"], []).append(
            parts.File(
                id=r["id"],
                season=season,
                side=side,
                number=number,
                volume=r["comicinfo_volume"],
                origin=r["origin"],
                confirmed=r["state"] == "confirmed",
                joined=r["joined_parts"],
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    """Run the census on the configured database; print its report."""
    parser = argparse.ArgumentParser(description="The split-chapter run census: read only, JSON on stdout.")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    args = parser.parse_args(argv)
    cfg = config.init(args.config)
    with closing(db.open_connection(cfg)) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        report = census(read(conn))
    sys.stdout.write(json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
