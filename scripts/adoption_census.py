"""The adoption census (agent-workspace/plans/library-adoption.md, step A0): what adopting the library would do.

Read only: for every in-scope series Kavita lists now (as the baseline pass takes them) with no bulk-converter record
and no baseline, `adoption.prepare` (every read adoption makes, nothing written; its layout sample goes to
<paths.scratch>/adopt-*/ and is removed), then the counts as JSON on stdout: step A0's, and the 2 the plan's "Phase A
done when" compares (ok series adoptable now; ok series with a completed check that D7 holds). A share that cannot be
reached stops it.

Usage, from the project root: python -m scripts.adoption_census [--config PATH]
"""

import argparse
import json
import sys
import time
import zipfile
from collections import Counter
from contextlib import closing
from pathlib import PurePosixPath

from manga_pipeline import adoption, archive, baseline, config, db, errors, workflow
from manga_pipeline.clients import http
from manga_pipeline.db import sql

LOCAL = ("library", "mangas", "Local")  # the bulk converter's scope: <paths.library>/mangas/Local/<title>


def census(ctx: workflow.Context) -> dict[str, object]:
    """Return the census counts (see the module docstring) of the context's database and services."""
    conn = ctx.conn
    listed = ctx.kavita.series_in_library(ctx.cfg["kavita"]["library_id"])
    rows = conn.execute(
        sql.SQL("""SELECT s.* FROM series s
            WHERE s.origin = 'kavita' AND s.kavita_series_id = ANY(%(listed)s) AND s.baseline_at IS NULL AND {scope}
            ORDER BY s.id""").format(scope=workflow.SCOPE),
        {**workflow.scope_params(ctx.cfg), "listed": [s["id"] for s in listed]},
    ).fetchall()
    held: Counter[str] = Counter()
    formats: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    seconds: list[float] = []
    counted = unchecked = at_discover = filename_volumes = downloads = local = adoptable = held_checked = 0
    for done, series in enumerate(rows):
        if done % 25 == 0:  # progress for a long run; stdout stays the JSON
            sys.stderr.write(f"census: {done}/{len(rows)} series\n")
        try:  # the folder take resolves before it decides record or adoption
            folder = adoption.folder_of(ctx, series)
        except (http.HttpError, http.HttpTimeoutError) as e:  # this series' Kavita reply: counted, the census goes on
            failed[type(e).__name__] += 1
            continue
        if isinstance(folder, str) and baseline.has_record(ctx.cfg, folder):
            continue
        counted += 1
        if isinstance(folder, str) and PurePosixPath(folder).parts[:3] == LOCAL:
            local += 1
        waits = adoption.waiting(conn, series["id"])
        unchecked += waits == adoption.CHECK_WAIT
        at_discover += waits == adoption.DISCOVER_WAIT
        downloads += _downloads(ctx, series["id"])
        start = time.monotonic()
        try:
            found = adoption.prepare(ctx, series, folder)
        except (http.HttpError, http.HttpTimeoutError) as e:  # this series' Kavita reply: counted, the census goes on
            failed[type(e).__name__] += 1
            continue
        seconds.append(time.monotonic() - start)
        ok = series["status"] == "ok"
        if isinstance(found, list):
            held.update({p.kind for p in found})
            held["series held"] += 1
            held_checked += ok and waits != adoption.CHECK_WAIT
            continue
        adoptable += ok and waits is None
        for f in found.files:
            formats.update(_formats(ctx.cfg, f.library_path))
            filename_volumes += f.volume is not None
    waiting = db.one_row(
        conn, "SELECT count(*) FROM chapter WHERE status = 'waiting' AND waiting_on = 'bulk_conversion'", ()
    )[0]
    return {
        "series without a record": counted,
        "held (D7), series per reason": dict(held),
        "page formats (files)": dict(formats),
        "files with a filename volume": filename_volumes,
        "seconds per series": {
            "mean": round(sum(seconds) / len(seconds), 3) if seconds else None,
            "max": round(max(seconds), 3) if seconds else None,
            "total": round(sum(seconds), 1),
        },
        "series whose check has not completed": unchecked,
        "series with a chapter at discover": at_discover,
        "downloads present for those series": downloads,
        "chapters waiting on bulk_conversion": waiting,
        "Local/ series without a record": local,
        "series not read (Kavita error)": dict(failed),
        "Phase A: ok series adoptable now": adoptable,
        "Phase A: ok series with a completed check held (D7)": held_checked,
    }


def _formats(cfg: config.Config, path: str) -> set[str]:
    """Return the file extensions of a library file's pages (its member list only)."""
    with zipfile.ZipFile(config.to_local(cfg, path)) as z:
        return {PurePosixPath(p).suffix.lower() for p in archive.archive_pages(z.namelist())}


def _downloads(ctx: workflow.Context, series_id: int) -> int:
    """Return how many of the series' open download chapters have their file in the downloads folder."""
    paths = ctx.conn.execute(
        """SELECT download_path FROM chapter WHERE series_id = %s AND kind = 'download' AND status <> 'done'
        AND download_path IS NOT NULL""",
        (series_id,),
    ).fetchall()
    return sum(errors.exists(config.to_local(ctx.cfg, r[0])) for r in paths)


def main(argv: list[str] | None = None) -> int:
    """Run the census on the configured database and services; print the counts."""
    parser = argparse.ArgumentParser(description="The adoption census (plan step A0): counts as JSON.")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    args = parser.parse_args(argv)
    cfg = config.init(args.config)
    with closing(db.read_only(cfg)) as conn:
        counts = census(workflow.context(cfg, conn, "census"))
    sys.stdout.write(json.dumps(counts, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
