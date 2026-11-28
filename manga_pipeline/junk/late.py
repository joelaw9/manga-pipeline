"""`junk late` (SPEC "Junk decisions", decision commands): the published files whose junk edit is late.

A file is late when the junk decide run again on its deciding chapter's kept scan (chapter_edit.scan) with today's
decisions, watermarks and cut decisions removes other pixels than its edit (differences: rows, pages, bands, joins,
split, blanks; a decision id alone is none). Its files are those `rewrite rebuild` takes but for the original
(conversions.REBUILD_TAKES) whose junk_chapter has a kept scan. The database is read series by series, each in 1 short
snapshot ended before any share read; a strip render extracts the file's original into
<paths.scratch>/junk-late/<pid>/, removed when the file is done. Read only: it writes nothing and queues nothing.
"""

import os
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import NamedTuple

from PIL import Image

from manga_pipeline import archive, config, conversions, db, errors, images, page_store, workflow
from manga_pipeline.convert import plan
from manga_pipeline.db import sql
from manga_pipeline.junk import anchored, chapter_scan, store
from manga_pipeline.stages import junk as junk_stage

FOLDER = "junk-late"  # below paths.scratch: each run's folder, <pid>

# The candidate files f of series s, with their deciding chapter's edit row e holding a kept scan
CANDIDATES = sql.SQL("""FROM published_file f JOIN series s ON s.id = f.series_id
    JOIN chapter_edit e ON e.chapter_id = f.junk_chapter
    WHERE e.scan IS NOT NULL AND {takes}""").format(takes=conversions.REBUILD_TAKES)


class LateError(Exception):
    """A kept scan the decide cannot read or apply (exit 1), naming its file."""


class _UnreadableError(Exception):
    """A strip file's original cannot be read for a render: the file is listed with why, the run goes on."""


class Candidate(NamedTuple):
    """A candidate file as 1 snapshot read it: its row's fields, its edit, its kept scan and (strip) its page rows."""

    id: int
    library_path: str
    original: str | None
    edit: str
    scan: str
    watermarks: list[str]
    pages: Mapping[int, images.MeasuredPage]


class Series(NamedTuple):
    """1 series as 1 snapshot read it: its layout, its candidate files and today's decide inputs."""

    layout: str
    files: list[Candidate]
    inputs: junk_stage.DecideInputs


def run(cfg: config.Config, conn: db.Connection) -> list[str]:
    """Return `junk late`'s lines: each listed file, then the count. conn: a read-only connection (db.read_only).

    LateError for a kept scan the decide cannot read or apply; Unreachable when the share drops.
    """
    params = workflow.scope_params(cfg)
    store.index(conn)  # read once, outside the snapshots: each series' snapshot reuses it while no decision changes
    with conn.transaction():
        series_ids = [
            r[0] for r in conn.execute(sql.SQL("SELECT DISTINCT f.series_id {} ORDER BY 1").format(CANDIDATES), params)
        ]
    out: list[str] = []
    seen = 0
    for series_id in series_ids:
        with conn.transaction():
            series = _read(conn, series_id, params)
        seen += len(series.files)
        for kept in series.files:
            line = _judge(cfg, series, kept)
            if line is not None:
                out.append(f"file {kept.id} {kept.library_path}: {line}")
    return [*out, f"files with a kept scan {seen}, listed {len(out)}"]


def _read(conn: db.Connection, series_id: int, params: workflow.ScopeParams) -> Series:
    """Read 1 series' candidate files and decide inputs, inside the caller's snapshot."""
    series = db.one_row(
        conn,
        """SELECT s.*, c.layout FROM series s JOIN series_conversion c ON c.series_id = s.id WHERE s.id = %s""",
        (series_id,),
    )
    rows = conn.execute(
        sql.SQL("""SELECT f.id, f.library_path, f.original, f.junk_chapter, e.edit, e.scan, e.scan_watermarks
        {} AND f.series_id = %(series)s ORDER BY f.id""").format(CANDIDATES),
        {**params, "series": series_id},
    ).fetchall()
    strip = series["layout"] == "strip"
    files = [
        Candidate(
            r["id"],
            r["library_path"],
            r["original"],
            r["edit"],
            r["scan"],
            list(r["scan_watermarks"] or []),
            page_store.load(conn, r["junk_chapter"]) if strip else {},
        )
        for r in rows
    ]
    inputs = junk_stage.decide_inputs(conn, store.Decisions(conn, store.series_key(series)))
    return Series(series["layout"], files, inputs)


def _judge(cfg: config.Config, series: Series, kept: Candidate) -> str | None:
    """Return why the file is listed (late, refused, layout changed, original unreadable), else None."""
    try:
        stored = plan.check_edit(kept.edit)
    except plan.EditError as e:
        msg = f"file {kept.id}: its edit cannot be read: {e}"
        raise LateError(msg) from e
    try:
        sc = junk_stage.read_scan(kept.scan)
        layout = sc["layout"]
    except Exception as e:
        msg = f"file {kept.id}: its kept scan cannot be read: {e!r}"
        raise LateError(msg) from e
    if layout != series.layout:
        return f"the series' layout changed since its scan ({layout} -> {series.layout}); {_original(cfg, kept)}"
    original = _Original(cfg, kept)
    try:
        render = original.render if layout == "strip" else None
        try:
            now = _apply(kept, sc, series.inputs, series.inputs.watermarks, render)
            if now.problem:
                return f"its decide is now refused: {now.problem}; {_original(cfg, kept)}"
            found = differences(stored, now.edit)
            if not found:
                return None
        except _UnreadableError as e:
            return str(e)
        why = "decisions"
        if set(kept.watermarks) != set(series.inputs.watermarks):
            try:
                why = cause(stored, _apply(kept, sc, series.inputs, kept.watermarks, render), now.edit)
            except _UnreadableError as e:
                why = f"not told: {e}"
    finally:
        original.remove()
    return f"{', '.join(found)} (cause {why}); {_original(cfg, kept)}"


def _apply(
    kept: Candidate,
    sc: chapter_scan.Scan,
    inputs: junk_stage.DecideInputs,
    watermarks: Sequence[str],
    render: Callable[[int, int], Image.Image] | None,
) -> chapter_scan.Applied:
    """Run the decide on a kept scan; LateError naming the file for any error but the share's and the original's."""
    try:
        return chapter_scan.apply(sc, inputs.decide_unit, watermarks, inputs.cuts, render)
    except (_UnreadableError, errors.Unreachable):
        raise
    except Exception as e:
        msg = f"file {kept.id}: its kept scan cannot be decided: {e!r}"
        raise LateError(msg) from e


def cause(stored: plan.ChapterEdit, then: chapter_scan.Applied, now: plan.ChapterEdit) -> str:
    """Return a late file's cause from the decide with its kept watermark keys (then) and with today's (now).

    "watermarks" when today's decisions with the kept keys give its edit, "decisions" when the keys learned since change
    nothing, else (a refused decide with the kept keys among them) "both".
    """
    if then.problem:
        return "both"
    if not differences(stored, then.edit):
        return "watermarks"
    if not differences(then.edit, now):
        return "decisions"
    return "both"


class _Original:
    """A strip file's original, extracted on the first render into <paths.scratch>/junk-late/<pid>/, at most once."""

    def __init__(self, cfg: config.Config, kept: Candidate) -> None:
        """Hold the file whose original is extracted on first use."""
        self._cfg, self._kept = cfg, kept
        self._work = config.abs_path(cfg["paths"]["scratch"]) / FOLDER / str(os.getpid())
        self._render: Callable[[int, int], Image.Image] | None = None

    def render(self, start: int, end: int) -> Image.Image:
        """Return the image of strip rows [start, end) over the original's content pages."""
        if self._render is None:
            self._render = chapter_scan.renderer(self._extract())
        try:
            with workflow.share_errors():
                return self._render(start, end)
        except (OSError, *archive.UNREADABLE) as e:  # a page that does not decode: read only now
            msg = f"its original {self._kept.original} cannot be read: {e}"
            raise _UnreadableError(msg) from e

    def _extract(self) -> chapter_scan.Extracted:
        stored = self._kept.original
        why = _original_problem(self._cfg, self._kept)
        if why is not None or stored is None:
            raise _UnreadableError(why or "no original recorded")
        try:
            with workflow.share_errors():
                cbz = config.to_local(self._cfg, stored)
                paths, names, sizes, _ = junk_stage.extract(cbz, self._kept.pages, self._work)
        except (OSError, *archive.UNREADABLE) as e:
            msg = f"its original {self._kept.original} cannot be read: {e}"
            raise _UnreadableError(msg) from e
        return chapter_scan.Extracted(paths, names, sizes)

    def remove(self) -> None:
        """Remove the extracted pages."""
        shutil.rmtree(self._work, ignore_errors=True)


def _original_problem(cfg: config.Config, kept: Candidate) -> str | None:
    """Return why the file's original cannot be read here (none recorded, unusable here, gone), else None."""
    if kept.original is None:
        return "no original recorded"
    if workflow.unusable_here(kept.original):
        return f"its original {kept.original} cannot be opened on this machine"
    with workflow.share_errors():
        if not errors.exists(config.to_local(cfg, kept.original)):
            return f"its original {kept.original} is gone"
    return None


def _original(cfg: config.Config, kept: Candidate) -> str:
    """Return whether the file's original is still in retention, as a listed line ends."""
    return _original_problem(cfg, kept) or "original in retention"


# ---------------------------------------------------------------- the differences: pure


def differences(stored: plan.ChapterEdit, now: plan.ChapterEdit) -> list[str]:
    """Return the pixels now's edit removes or keeps unlike stored's, worded; [] when they remove the same.

    Strip ranges compared as row sets (adjacent ranges split differently are the same rows), pages, bands, joins,
    split and blanks; a decision id or a range's why alone is no difference.
    """
    before, after = _rows(stored["strip_removed"]), _rows(now["strip_removed"])
    out = [
        *_named("rows removed anew", _minus(after, before)),
        *_named("rows kept anew", _minus(before, after)),
        *_sets("pages dropped anew", "pages kept anew", _pages(stored["drop_pages"]), _pages(now["drop_pages"])),
        *_sets("bands anew on pages", "bands gone on pages", _bands(stored["bands"]), _bands(now["bands"])),
        *_sets("joins anew at pages", "joins gone at pages", _joins(stored["join"]), _joins(now["join"])),
        *_sets("blanks anew on pages", "blanks gone on pages", _blanks(stored["blank"]), _blanks(now["blank"])),
    ]
    out += [f"{k} {stored[k]} -> {now[k]}" for k in ("split", "split_pages") if stored[k] != now[k]]
    return out


def _rows(removed: Iterable[plan.RemovedRange]) -> list[tuple[int, int]]:
    return anchored.merged((r["start"], r["end"]) for r in removed)


def _minus(rows: Iterable[tuple[int, int]], taken: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    return [part for s, e in rows for part in anchored.uncovered(s, e, taken)]


def _named(what: str, rows: Sequence[tuple[int, int]]) -> list[str]:
    return [f"{what} {' '.join(f'{s}-{e}' for s, e in rows)}"] if rows else []


def _sets(gained: str, lost: str, before: set[tuple[int, ...]], after: set[tuple[int, ...]]) -> list[str]:
    """Return the pages of the entries after holds that before does not (gained), and of the reverse (lost)."""
    out: list[str] = []
    for what, extra in ((gained, after - before), (lost, before - after)):
        if extra:
            out.append(f"{what} {' '.join(str(p) for p in sorted({x[0] for x in extra}))}")
    return out


def _pages(pages: Iterable[int]) -> set[tuple[int, ...]]:
    return {(p,) for p in pages}


def _bands(bands: Iterable[plan.BandCrop]) -> set[tuple[int, ...]]:
    return {(b["page"], *b["box"]) for b in bands}


def _joins(joins: Iterable[Sequence[int]]) -> set[tuple[int, ...]]:
    return {tuple(j) for j in joins}


def _blanks(blanks: Iterable[plan.Blank]) -> set[tuple[int, ...]]:
    return {(b["page"], *b["box"], *b["fill"]) for b in blanks}
