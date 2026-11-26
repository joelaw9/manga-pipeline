"""Calibrate adopted series from a spread of their files, as the bulk converter did (SPEC "Calibration").

Plan phase-10-29.

For each adopted series in scope (layout pages or strip, status ok, holding an adopted file) that holds no group fixed
by a spread: its adopted files (origin library, confirmed) in number order; those with an AVIF page as convert would
plan them are usable (every strip file is); 3 are picked spread over them; each is planned with no junk edit (a strip
file rendered; a page file split at the series' page aspect when stitched, and rendered), and its AVIF pages are its
units. Per group, 40 units are shared among the 3, the file with fewest first, evenly spaced in each; a repeated page
is probed once and counted each time; 20 or more are probed and the fix rule (calibrate.choose) applied, fewer fix the
group at q80. Every group but a record's is written fixed, its why starting `spread:`, in 1 transaction per series.

A file whose pages cannot be decoded or planned is left out and the 3 are picked again over the rest; the share failing
or an encode error leaves the series as it was and the run goes on. Series with an open conversion chapter go first.
Under lock.IDLE exclusive: no part runs meanwhile. --series takes the listed series that pass the rest, whatever their
groups; --dry-run writes nothing.

Usage, from the project root:
python -m scripts.calibrate_spread [--series ID ...] [--dry-run] [--config PATH]
Exit 0; 1 when the scorer cannot be loaded; 3 when a part runs.
"""

import argparse
import math
import os
import shutil
import sys
import zipfile
from collections.abc import Iterator, Sequence
from concurrent.futures import Executor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from manga_pipeline import archive, config, conversions, db, images, lock, strip, workflow
from manga_pipeline.convert import calibrate, image, pages, plan, tools, vship
from manga_pipeline.db import sql
from manga_pipeline.stages import convert

PICKED = 3  # files picked per series (the bulk converter's calibration_max_chapters)
PAGES = 40  # sample pages per group, shared among them (its calibration_pages)
WHY = "spread: "  # the why of every group this script fixes; a series holding one is done
FOLDER = "calibrate-spread"  # below paths.scratch: 1 folder per run (its pid)
NO_EDIT: plan.ChapterEdit = {
    "split": False,
    "drop_pages": [],
    "bands": [],
    "join": [],
    "strip_removed": [],
    "blank": [],
}

SERIES = sql.SQL("""SELECT s.id, s.name, c.layout, c.page_aspect FROM series s
    JOIN series_conversion c ON c.series_id = s.id
    WHERE c.layout IN ('pages', 'strip') AND s.status = 'ok' AND {scope}
        AND EXISTS (SELECT 1 FROM published_file p WHERE p.series_id = s.id AND p.origin = 'library'
            AND p.state = 'confirmed')
        AND (s.id = ANY(%(series)s::bigint[]) OR (%(series)s::bigint[] IS NULL AND NOT EXISTS (
            SELECT 1 FROM series_calibration g WHERE g.series_id = s.id AND g.why LIKE 'spread:%%')))
    ORDER BY NOT EXISTS (SELECT 1 FROM chapter WHERE chapter.series_id = s.id AND {open}), s.id""").format(
    scope=workflow.SCOPE, open=conversions.OPEN
)
FILES = """SELECT id, library_path FROM published_file WHERE series_id = %s AND origin = 'library'
    AND state = 'confirmed' ORDER BY comicinfo_number::double precision, id"""


@dataclass(frozen=True)
class Bad:
    """A file left out, and why."""

    name: str
    why: str


@dataclass
class Result:
    """1 series' spread: {group: (q, why)}, the files picked, the files left out."""

    groups: dict[str, tuple[int, str]] = field(default_factory=dict[str, tuple[int, str]])
    picked: list[str] = field(default_factory=list[str])
    left_out: list[Bad] = field(default_factory=list[Bad])


def spread(n: int, m: int = PICKED) -> list[int]:
    """Return the indexes of at most m of n items, spread evenly from the first to the last (the bulk converter's)."""
    if n <= m:
        return list(range(n))
    if m == 1:
        return [0]
    return sorted({round(i * (n - 1) / (m - 1)) for i in range(m)})


def share[T](per: Sequence[Sequence[T]], total: int = PAGES) -> list[list[T]]:
    """Share total items among the lists, the shortest first, evenly spaced in each (the bulk converter's sample)."""
    left, chosen = total, dict[int, list[T]]()
    order = sorted(range(len(per)), key=lambda k: len(per[k]))
    for i, k in enumerate(order):
        chosen[k] = calibrate.spaced(per[k], math.ceil(left / (len(order) - i)))
        left -= len(chosen[k])
    return [chosen[k] for k in range(len(per))]


# ---------------------------------------------------------------- 1 file, in a page worker


def _measure(cbz: Path) -> list[images.MeasuredPage] | Bad:
    """Return the file's measured pages, or why it cannot be measured; a share error raises Unreachable."""
    try:
        with workflow.share_errors():
            return images.measure_pages(cbz)
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        return Bad(cbz.name, f"cannot be measured: {e}")


def _content_sizes(measured: Sequence[images.MeasuredPage]) -> list[strip.Size]:
    """Return the (w, h) of the file's pages that are not cover-named."""
    return [(p["width"], p["height"]) for p in measured if not archive.is_cover_name(p["name"])]


def _planned(
    cbz: Path, measured: Sequence[images.MeasuredPage], x_dir: Path, layout: plan.SeriesLayout
) -> plan.RoutedPlan | Bad:
    """Return the file's plan as convert plans it with no junk edit (a stitched page file split), or why not."""
    stitched = layout.kind == "pages" and strip.is_stitched(_content_sizes(measured), layout.page_aspect)
    try:
        with workflow.share_errors():
            files, problems = pages.extract(cbz, x_dir)
            if problems:
                return Bad(cbz.name, f"cannot be extracted: {'; '.join(problems)}")
            return plan.routes(plan.build(cbz.name, measured, files, {**NO_EDIT, "split": stitched}, layout))
    except (OSError, ValueError, zipfile.BadZipFile) as e:  # plan.NeedsHuman and plan.EditError are ValueErrors
        return Bad(cbz.name, f"cannot be planned: {e}")


def usable(cbz: Path, measured: Sequence[images.MeasuredPage], x_dir: Path, layout: plan.SeriesLayout) -> bool | Bad:
    """Return whether the file has an AVIF page (a unit), computed from its plan without rendering; or why not known."""
    try:
        routed = _planned(cbz, measured, x_dir, layout)
        if isinstance(routed, Bad):
            return routed
        return bool(routed.render) or any(calibrate.units(routed, x_dir, []).values())
    finally:
        shutil.rmtree(x_dir, ignore_errors=True)


def units(cbz: Path, x_dir: Path, layout: plan.SeriesLayout) -> dict[str, list[calibrate.Unit]] | Bad:
    """Return the file's units by group (rendered when its plan renders), or why it has none to give."""
    measured = _measure(cbz)
    if isinstance(measured, Bad):
        return measured
    routed = _planned(cbz, measured, x_dir, layout)
    if isinstance(routed, Bad):
        return routed
    rendered = image.render(routed, x_dir, x_dir.with_name(f"{x_dir.name}-render")) if routed.render else []
    got = calibrate.units(routed, x_dir, rendered)
    return got if any(got.values()) else Bad(cbz.name, "no AVIF page")


# ---------------------------------------------------------------- 1 series


def _usable_pages(
    files: Sequence[Path], stored: list[float] | None, pool: Executor, work: Path, out: Result
) -> tuple[list[Path], plan.SeriesLayout]:
    """Return a page series' usable files in order and its layout with the page aspects; files left out go to out.

    The aspects: the stored ones, else the files' own (strip.page_aspect over their content pages).
    """
    measured = dict(zip(files, pool.map(_measure, files), strict=True))
    out.left_out += [m for m in measured.values() if isinstance(m, Bad)]
    good = {f: m for f, m in measured.items() if not isinstance(m, Bad)}
    aspects = stored if stored is not None else strip.page_aspect([_content_sizes(m) for m in good.values()])
    series_layout = plan.SeriesLayout("pages", aspects)
    jobs = [(f, pool.submit(usable, f, m, work / f"u{i}", series_layout)) for i, (f, m) in enumerate(good.items())]
    found = [(f, j.result()) for f, j in jobs]
    out.left_out += [u for _, u in found if isinstance(u, Bad)]
    return [f for f, u in found if u is True], series_layout


def _pick(
    usable_files: list[Path], layout: plan.SeriesLayout, pool: Executor, work: Path, out: Result
) -> dict[Path, dict[str, list[calibrate.Unit]]]:
    """Return the 3 picked files' units, picking again over the rest while a picked file gives none."""
    prepared: dict[Path, dict[str, list[calibrate.Unit]]] = {}
    while True:
        picked = [usable_files[i] for i in spread(len(usable_files))]
        todo = [f for f in picked if f not in prepared]
        if not todo:
            return {f: prepared[f] for f in picked}
        jobs = [(f, pool.submit(units, f, work / "picked" / f.name, layout)) for f in todo]  # 1 folder: unique names
        for f, job in jobs:
            got = job.result()
            if isinstance(got, Bad):
                out.left_out.append(got)
                usable_files.remove(f)
            else:
                prepared[f] = got


def _probe_group(
    encoder: image.Encoder,
    per_file: Sequence[Sequence[calibrate.Unit]],
    work: Path,
    scores: dict[str, calibrate.Scores],
) -> tuple[int, str]:
    """Return (q, why) of 1 group from the units shared among the picked files; scores: probed so far, by sha1."""
    chosen = share(per_file)
    n = sum(len(c) for c in chosen)
    if n < calibrate.MIN_SAMPLES:
        return calibrate.CEILING, f"q{calibrate.CEILING}: {n} sample pages < {calibrate.MIN_SAMPLES}"
    for k, file_units in enumerate(chosen):
        fresh = list({u["sha1"]: u for u in file_units if u["sha1"] not in scores}.values())
        if fresh:  # 1 probe per file: probe names its work files by the unit's stem, which repeats across files
            scores.update(calibrate.probe(encoder, fresh, work / f"probe-f{k}"))
    samples: list[calibrate.NewSample] = [
        {
            "sha1": u["sha1"],
            "rendered": u["rendered"],
            "bytes_in": u["path"].stat().st_size,
            "scores": scores[u["sha1"]],
        }
        for c in chosen
        for u in c
    ]
    chose = calibrate.choose(samples)
    if chose is None:  # n >= MIN_SAMPLES: choose always decides
        msg = f"calibrate.choose left {n} samples undecided"
        raise AssertionError(msg)
    return chose


def calibrate_series(
    files: Sequence[Path], layout: str, aspects: list[float] | None, encoder: image.Encoder, work: Path
) -> Result:
    """Return 1 series' spread over its files (local CBZ paths in number order); writes only below work.

    layout: "pages" or "strip"; aspects: its stored page aspects or None. A share error raises Unreachable; an encode
    error raises tools.ToolError or vship.VshipError.
    """
    out = Result()
    if layout == "strip":  # a strip plan always renders, so has an AVIF outcome: every file is usable
        usable_files, series_layout = list(files), plan.SeriesLayout("strip", aspects)
    else:
        usable_files, series_layout = _usable_pages(files, aspects, encoder.pool, work, out)
    if not usable_files:
        return out
    picked = _pick(usable_files, series_layout, encoder.pool, work, out)
    out.picked = [f.name for f in picked]
    scores: dict[str, calibrate.Scores] = {}
    for group in plan.QUALITY_GROUPS:
        per_file = [got.get(group, []) for got in picked.values()]
        out.groups[group] = _probe_group(encoder, per_file, work / group, scores)
    return out


# ---------------------------------------------------------------- the run


def _write(conn: db.Connection, series_id: int, result: Result) -> None:
    """Write the series' groups (a record's left alone), fixed, why `spread: `, no samples, in 1 transaction."""
    files = ", ".join(result.picked)
    with db.transaction(conn):
        stored = calibrate.rows(conn, series_id)
        for group, (q, why) in result.groups.items():
            row = stored.get(group)
            if row is not None and row["origin"] == "record":
                continue
            conn.execute(
                """INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why, samples)
                VALUES (%s, %s, %s, 1, 'computed', %s, '[]')
                ON CONFLICT (series_id, grp) DO UPDATE SET q = excluded.q, fixed = 1, origin = 'computed',
                    why = excluded.why, samples = '[]'""",
                (series_id, group, q, f"{WHY}{why}; files {files}"),
            )


def _line(series: db.Row, result: Result, before: dict[str, calibrate.Group]) -> str:
    """Return the series' output line: each group's q and why, the files picked and left out, the rows replaced."""
    head = f"series {series['id']} ({series['name']}, {series['layout']})"
    if not result.picked:
        return f"{head}: no usable file; nothing written" + "".join(f"; {b.name} {b.why}" for b in result.left_out)
    groups = "; ".join(f"{g} q{q} ({why})" for g, (q, why) in result.groups.items())
    old = "; ".join(
        f"was {g} q{r['q']} fixed {r['fixed']} ({r['why']}), {len(r['samples'])} samples" for g, r in before.items()
    )
    left = "".join(f"; left out {b.name}: {b.why}" for b in result.left_out)
    return f"{head}: {groups}; files {', '.join(result.picked)}{left}" + (f"; {old}" if old else "")


def _scratch(cfg: config.Config) -> Path:
    """Return this run's scratch folder, the folders of earlier runs removed (no other run holds the lock)."""
    base = config.abs_path(cfg["paths"]["scratch"]) / FOLDER
    shutil.rmtree(base, ignore_errors=True)
    mine = base / str(os.getpid())
    mine.mkdir(parents=True)
    return mine


def _files(cfg: config.Config, conn: db.Connection, series_id: int) -> list[Path]:
    """Return the series' adopted files on this machine, in number order."""
    return [config.to_local(cfg, r["library_path"]) for r in conn.execute(FILES, (series_id,))]


def _stored_aspects(series: db.Row) -> list[float] | None:
    """Return the series' stored page aspects, or None."""
    return plan.read_page_aspect(series["id"], series["page_aspect"]) if series["page_aspect"] else None


def run(
    cfg: config.Config, conn: db.Connection, encoder: image.Encoder, series: Sequence[int] | None, *, dry_run: bool
) -> Iterator[str]:
    """Spread every series the run takes, yielding 1 line each as it is done, then the summary."""
    params = {**workflow.scope_params(cfg), "series": None if series is None else list(series)}
    rows = conn.execute(SERIES, params).fetchall()
    work = _scratch(cfg)
    spread_n = 0
    for row in rows:
        series_work = work / f"s{row['id']}"
        try:
            result = calibrate_series(
                _files(cfg, conn, row["id"]), row["layout"], _stored_aspects(row), encoder, series_work
            )
        except (workflow.Unreachable, tools.ToolError, vship.VshipError) as e:  # nothing written
            yield f"series {row['id']} ({row['name']}, {row['layout']}): failed, nothing written: {e}"
            continue
        finally:
            shutil.rmtree(series_work, ignore_errors=True)
        before = calibrate.rows(conn, row["id"])
        if result.picked and not dry_run:
            _write(conn, row["id"], result)
        spread_n += bool(result.picked)
        yield _line(row, result, before)
    shutil.rmtree(work, ignore_errors=True)
    done = "spread (dry run: nothing written)" if dry_run else "spread and written"
    yield f"series: {len(rows)}; {done}: {spread_n}; not spread: {len(rows) - spread_n}"


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Return the script's arguments."""
    parser = argparse.ArgumentParser(description="Calibrate adopted series from a spread of their files.")
    parser.add_argument("--series", type=int, nargs="+", help="only these series, whatever their groups")
    parser.add_argument("--dry-run", action="store_true", help="compute and print; write nothing")
    parser.add_argument("--config", help="config file (default: config.yaml)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Spread the series on the configured database; print 1 line per series and a summary."""
    args = parse(argv)
    cfg = config.init(args.config)
    with closing(db.open_database(cfg, builds=False)) as conn:
        try:
            with lock.held(conn, cfg, (), lock.EXCLUSIVE), convert.encoder(cfg) as encoder:
                for line in run(cfg, conn, encoder, args.series, dry_run=args.dry_run):
                    sys.stdout.write(f"{line}\n")
                    sys.stdout.flush()
        except lock.LockHeld as e:
            sys.stderr.write(f"error: {e}\n")
            return 3
        except vship.VshipError as e:
            sys.stderr.write(f"error: the scorer cannot be loaded: {e}\n")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
