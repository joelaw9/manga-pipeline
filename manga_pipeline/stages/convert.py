"""Stage convert (SPEC "Chapter stages", "Conversion", "Calibration"): re-encode the pages into a staged CBZ.

Re-encode the download's pages as the bulk converter does and write the store-mode CBZ of pages at
<paths.staging>/<database id, first 8 characters>-<chapter id>-<claim count>.cbz. The database id in the name keeps a
file left in staging by another database from passing as this chapter's output; the claim count keeps 2 claims of 1
chapter from writing 1 file.

Every work file is in <paths.scratch>/<chapter id>-<claim count>/, removed when the stage ends, whatever the outcome;
the folders a killed run left are removed by process at its start (process.clean_scratch), not here. Nothing is written
in the series folder.
"""

import contextlib
import functools
import json
import logging
import shutil
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TypedDict

from manga_pipeline import archive, config, db, images, page_store, workflow
from manga_pipeline.convert import calibrate, check, covers, image, notes, pages, plan, tools, vship

log = logging.getLogger(__name__)
# A chapter's scratch folders: its extracted download and the pages it writes (process_chapter, then the staged file).
EXTRACTED, OUTPUT = "x", "out"


class ResultCover(covers.InsertedCover):
    """The volume cover a convert_result records: the cover the encode inserted, and the volume it is the cover of."""

    volume: str | None


class ConvertResult(TypedDict):
    """A chapter's convert_result (JSON): what the encode's record (image.process_chapter) says of the staged file."""

    route: str | None
    outcome: str
    quality: dict[str, int]
    mean_score: float | None
    min_score: float | None
    pages_in: int
    pages_out: int
    dropped: list[str]
    reshape: image.Reshape | None
    blanked: list[str]
    cover: ResultCover | None
    cover_skipped: str | None
    gray: bool
    avif_rejected: str | None
    bytes_in: int
    bytes_out: int


class StagedPage(TypedDict):
    """An output page of the staged file as chapter.output_pages records it: index, name, width, height, bytes."""

    index: int
    name: str
    width: int
    height: int
    bytes: int


def read_result(text: str) -> ConvertResult:
    """Return a converted chapter's convert_result (JSON text). Unchecked."""
    return json.loads(text)


def read_output_pages(text: str) -> list[StagedPage]:
    """Return a converted chapter's output_pages (JSON text). Unchecked."""
    return json.loads(text)


@functools.cache
def loaded_scorer(kind: type[vship.Scorer], path: Path) -> vship.Scorer:
    """Return 1 loaded, GPU-checked scorer per library path for the process.

    The library and its CUDA context are made once, not per chapter. kind: the scorer class (vship.Scorer).
    """
    return kind(path)


@contextlib.contextmanager
def encoder(cfg: config.Config) -> Generator[image.Encoder]:
    """Provide the image.Encoder of 1 chapter's (or 1 cover's) encodes.

    It holds the configured image tools, the process's loaded scorer and fresh page workers, stopped on leaving.
    """
    tool_paths = {name: str(config.abs_path(cfg["tools"][name])) for name in tools.NAMES}
    scorer = loaded_scorer(vship.Scorer, config.abs_path(cfg["tools"]["vship"]))
    with image.pool(cfg["convert"]["workers"]) as workers:
        yield image.Encoder(workers, scorer, tool_paths)


def staged_name(conn: db.Connection, chapter: db.Row) -> str:
    """<database id, 8 characters>-<chapter id>-<claim count>.cbz: 2 claims of 1 chapter never write 1 name."""
    return f"{db.database_id(conn)[:8]}-{workflow.claim_name(chapter)}.cbz"


def _edit(conn: db.Connection, chapter_id: int) -> plan.ChapterEdit:
    """Return the chapter's checked edit.

    Raises plan.EditError when the row is missing or malformed (a program error).
    """
    row = conn.execute("SELECT edit FROM chapter_edit WHERE chapter_id = %s", (chapter_id,)).fetchone()
    if row is None:
        msg = "no chapter_edit row: the junk stage wrote none (a program error)"
        raise plan.EditError(msg)
    return plan.check_edit(row[0])


def _layout(conn: db.Connection, series_id: int) -> plan.SeriesLayout:
    """Return the series' plan.SeriesLayout (series_conversion; page_aspect is JSON text)."""
    row = conn.execute(
        "SELECT layout, page_aspect FROM series_conversion WHERE series_id = %s", (series_id,)
    ).fetchone()
    if row is None:
        msg = f"series {series_id} has no layout (series_conversion)"
        raise RuntimeError(msg)
    aspect = None if row["page_aspect"] is None else plan.read_page_aspect(series_id, row["page_aspect"])
    return plan.SeriesLayout(row["layout"], aspect)


def _samples_wanted(row: calibrate.Group | None, chapter_id: int) -> bool:
    """Return whether this group takes samples from this chapter.

    It does with no row yet, or open, short of calibrate.MAX_SAMPLES and none from this chapter yet.
    """
    return row is None or not (
        row["fixed"]
        or len(row["samples"]) >= calibrate.MAX_SAMPLES
        or any(s["chapter_id"] == chapter_id for s in row["samples"])
    )


def _sampling(stored: Mapping[str, calibrate.Group], chapter_plan: plan.RoutedPlan, chapter_id: int) -> bool:
    """Return whether a group the plan's AVIF outcome encodes takes samples from this chapter.

    stored: calibrate.rows.
    """
    return any(_samples_wanted(stored.get(g), chapter_id) for g in plan.avif_groups(chapter_plan))


def _calibrate(
    ctx: workflow.Context,
    chapter: db.Row,
    groups: Mapping[str, Sequence[calibrate.Unit]],
    *,
    encoder: image.Encoder,
    work: Path,
) -> tuple[dict[str, int], list[str]]:
    """Add this chapter's samples to the series' open groups; return {group: q} after, and a line per group it fixed.

    groups is calibrate.units of the pages it samples ({} when no group wants samples).
    """
    stored = calibrate.rows(ctx.conn, chapter["series_id"])
    fixed: list[str] = []
    for group, group_units in groups.items():
        row = stored.get(group)
        if not group_units or not _samples_wanted(row, chapter["id"]):
            continue
        picked = calibrate.pick(group_units, {s["sha1"] for s in row["samples"]} if row else set())
        if not picked:
            continue
        new = _samples(encoder, picked, work / f"calibrate-{group}")
        stored[group] = calibrate.add(
            ctx.conn, chapter, group, new, guard=lambda _conn: workflow.holds_claim(ctx, chapter)
        )
        log.info("series %s %s calibration: %s", chapter["series_id"], group, stored[group]["why"])
        if stored[group]["fixed"]:
            g = stored[group]
            chapters = len({s["chapter_id"] for s in g["samples"]})
            fixed.append(f"calibration: {group} fixed at q{g['q']}: {g['why']}, from {chapters} chapters")
    return calibrate.quality(stored), fixed


def _samples(encoder: image.Encoder, picked: Sequence[calibrate.Unit], work_dir: Path) -> list[calibrate.NewSample]:
    """Probe the picked units (calibrate.pick); return them as the samples calibrate.add stores."""
    scores = calibrate.probe(encoder, picked, work_dir)
    return [
        {
            "sha1": u["sha1"],
            "rendered": u["rendered"],
            "bytes_in": u["path"].stat().st_size,
            "scores": scores[u["sha1"]],
        }
        for u in picked
    ]


class _EncodedCover(NamedTuple):
    """A chapter's volume cover once its AVIF is encoded: the volume it is the cover of, and the cover routes reads."""

    volume: str
    cover: plan.VolumeCover


def _cover_skipped(chapter_plan: plan.ChapterPlan, rec: image.ChapterRecord, cover: _EncodedCover | None) -> str | None:
    """Why a chapter that takes a volume cover went out without it (None when it has it, or takes none)."""
    if cover is None or rec["cover"]:
        return None
    if rec["edits"] == "all":  # rendered pages: AVIF or PNG
        fmts = [images.AVIF if rec["outcome"] == "avif" else images.PNG]
    else:
        fmts = sorted({q["format"] for q in chapter_plan.kept() if not q["cover"]})
    wanted = images.AVIF if rec["outcome"] == "avif" else plan.cover_format(fmts)
    if wanted == images.AVIF:
        return f"its AVIF cover scored {cover.cover['avif']['score']}, below the floor"
    return f"its {rec['outcome']} pages ({', '.join(fmts)}) share no cover format"


def _output_pages(out_dir: Path, rec: image.ChapterRecord) -> list[StagedPage]:
    out: list[StagedPage] = []
    for i, p in enumerate(rec["output"]):
        w, h = images.page_size(out_dir / p["name"])
        out.append(
            {"index": i, "name": p["name"], "width": w, "height": h, "bytes": (out_dir / p["name"]).stat().st_size}
        )
    return out


def _post_check(staged: Path, rec: image.ChapterRecord, chapter_plan: plan.ChapterPlan) -> None:
    """RuntimeError naming every failed post-check of the staged file."""
    fails = check.check(staged, rec, chapter_plan.kept())
    if fails:
        msg = f"post-check failed: {'; '.join(fails)}"
        raise RuntimeError(msg)


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Convert the chapter's download into its staged CBZ in its own scratch folder, removed whatever the outcome.

    A missing or malformed edit, a page that cannot be taken or an edit that drops every page is needs_human.
    """
    cfg = ctx.cfg
    try:
        edit = _edit(ctx.conn, chapter["id"])
    except plan.EditError as e:
        return workflow.NeedsHuman(str(e))
    layout = _layout(ctx.conn, chapter["series_id"])
    scratch = config.abs_path(cfg["paths"]["scratch"]) / workflow.claim_name(chapter)
    try:
        return _convert(ctx, chapter, edit, layout, scratch=scratch)
    finally:
        try:
            shutil.rmtree(scratch)
        except FileNotFoundError:
            pass
        except OSError as e:  # the outcome stands; the folder is reported, not hidden
            log.warning("chapter %s: scratch folder %s not removed: %s", chapter["id"], scratch, e)


def _convert(
    ctx: workflow.Context, chapter: db.Row, edit: plan.ChapterEdit, layout: plan.SeriesLayout, *, scratch: Path
) -> workflow.Done | workflow.NeedsHuman:
    x_dir, out_dir = scratch / EXTRACTED, scratch / OUTPUT
    try:
        chapter_plan = _plan(ctx, chapter, edit, layout, x_dir)
    except (plan.NeedsHuman, plan.EditError) as e:
        return workflow.NeedsHuman(str(e))
    rec, cover, calibrated = _encode(ctx, chapter, chapter_plan, _volume_cover(ctx, chapter, chapter_plan), scratch)
    staged = config.abs_path(ctx.cfg["paths"]["staging"]) / staged_name(ctx.conn, chapter)
    size = _write_staged(staged, rec, out_dir, chapter_plan)
    result = _result(chapter_plan, rec, cover)
    chapter_notes = notes.notes(
        chapter_plan,
        rec,
        sizes=notes.Sizes(size, config.to_local(ctx.cfg, chapter["download_path"]).stat().st_size),
        odd_chroma=_odd_chroma(chapter_plan, x_dir),
        calibrated=calibrated,
    )
    output_pages = _output_pages(out_dir, rec)
    if result["cover_skipped"]:
        log.warning(
            "chapter %s: volume %s cover not inserted: %s",
            chapter["id"],
            None if cover is None else cover.volume,
            result["cover_skipped"],
        )

    def record(conn: db.Connection) -> None:
        conn.execute(
            "UPDATE chapter SET staged_path = %s, staged_size = %s, convert_result = %s,"
            " output_pages = %s WHERE id = %s",
            (config.to_stored(ctx.cfg, staged), size, json.dumps(result), json.dumps(output_pages), chapter["id"]),
        )
        workflow.replace_notes(conn, chapter["id"], notes.PREFIX, chapter_notes)

    log.info(
        "chapter %s: %s via %s, %d -> %d pages, %d -> %d bytes",
        chapter["id"],
        rec["outcome"],
        rec["route"],
        rec["pages"],
        len(rec["output"]),
        rec["bytes_in"],
        rec["bytes_out"],
    )
    return workflow.Done(record)


def _plan(
    ctx: workflow.Context, chapter: db.Row, edit: plan.ChapterEdit, layout: plan.SeriesLayout, x_dir: Path
) -> plan.ChapterPlan:
    """Return the chapter's plan: its download extracted into x_dir and measured.

    The stored page rows are used where they exist. Raises plan.NeedsHuman (a page that cannot be taken, an edit that
    drops every page) or plan.EditError.
    """
    cbz = config.to_local(ctx.cfg, chapter["download_path"])
    files, problems = pages.extract(cbz, x_dir)
    if problems:
        msg = "; ".join(problems)
        raise plan.NeedsHuman(msg)
    measured = images.measure_pages(cbz, known=page_store.load(ctx.conn, chapter["id"]))
    chapter_plan = plan.build(f"chapter {chapter['id']}", measured, files, edit, layout)
    if not chapter_plan.kept():
        msg = "the chapter edit drops every page"
        raise plan.NeedsHuman(msg)
    return chapter_plan


class _CoverFile(NamedTuple):
    """A chapter's volume cover as handed over (volume_cover): its volume, its file's bytes, the width it goes in at."""

    volume: str
    data: bytes
    width: int


def _volume_cover(ctx: workflow.Context, chapter: db.Row, chapter_plan: plan.ChapterPlan) -> _CoverFile | None:
    """Return the chapter's volume cover, or None when it takes none.

    The cover's volume and file: read and checked, at the width it goes in at.
    """
    row = ctx.conn.execute("SELECT * FROM volume_cover WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    if row is None:
        return None
    return _CoverFile(row["volume"], covers.load(ctx.cfg, row), covers.width(chapter_plan))


def _odd_chroma(chapter_plan: plan.ChapterPlan, x_dir: Path) -> list[str]:
    """Return the kept colour JPEG pages whose chroma subsampling avifenc cannot keep (stored as 444: headers read)."""
    jpegs = [q["name"] for q in chapter_plan.kept() if q["format"] == images.JPEG and q["chroma"] == "444"]
    return [name for name in jpegs if images.odd_jpeg_chroma((x_dir / name).read_bytes())]


def _encode(
    ctx: workflow.Context, chapter: db.Row, chapter_plan: plan.ChapterPlan, cover_file: _CoverFile | None, scratch: Path
) -> tuple[image.ChapterRecord, _EncodedCover | None, list[str]]:
    """Execute the plan with the run's encoder.

    Return image.process_chapter's record, the volume cover encoded and the lines of the calibration groups it fixed.

    The volume cover's AVIF first, then the route, the pages, the calibration samples and the encode.
    """
    work = scratch / "work"
    with encoder(ctx.cfg) as enc:
        # before the route: an AVIF cover that passed can make the chapter AVIF
        cover = None if cover_file is None else _cover(enc, cover_file, work / "cover")
        routed = _route(chapter, chapter_plan, None if cover is None else cover.cover)
        sampling = _sampling(calibrate.rows(ctx.conn, chapter["series_id"]), routed, chapter["id"])
        prepared = _pages(routed, scratch, sampling=sampling)
        routed.quality, calibrated = _calibrate(ctx, chapter, prepared.units, encoder=enc, work=work)
        rec = image.process_chapter(
            image.PageFiles.of(scratch / EXTRACTED, prepared.encoded, prepared.blanked),
            scratch / OUTPUT,
            work_dir=work / "encode",
            encoder=enc,
            chapter_plan=routed,
        )
        return rec, cover, calibrated


def _cover(enc: image.Encoder, cover_file: _CoverFile, work_dir: Path) -> _EncodedCover:
    """Return the volume cover with its AVIF encode (image.Encoder.cover), and its volume."""
    avif = enc.cover(cover_file.data, cover_file.width, work_dir)
    return _EncodedCover(cover_file.volume, {"data": cover_file.data, "width": cover_file.width, "avif": avif})


def _route(chapter: db.Row, chapter_plan: plan.ChapterPlan, cover: plan.VolumeCover | None) -> plan.RoutedPlan:
    """Return the plan with its route and outcomes (cover: the volume cover with its AVIF encode, when it takes one).

    A mix of formats that stays untouched is logged.
    """
    routed = plan.routes(chapter_plan, cover=cover)
    if routed.route == "untouched-flag":
        log.warning(
            "chapter %s: formats %s mix with others: untouched",
            chapter["id"],
            sorted({q["format"] for q in chapter_plan.kept()}),
        )
    return routed


@dataclass
class _Pages:
    """The page step's outputs: the blanked pages, the rendered PNGs and the calibration units.

    The blanked pages ({page name: PNG}), the rendered PNGs the encode reads ([] when the plan renders nothing) and the
    calibration units of the pages the chapter samples ({} when it samples none).
    """

    blanked: dict[str, Path]
    encoded: list[Path]
    units: dict[str, list[calibrate.Unit]]


def _pages(chapter_plan: plan.RoutedPlan, scratch: Path, *, sampling: bool) -> _Pages:
    """Make the pages the encode and the calibration read.

    Logo blanks apply to the AVIF outcomes only; calibration samples take the unblanked pages (as the bulk converter),
    so a rendered chapter with blanks is rendered unblanked too when a group wants its samples (sampling).
    """
    x_dir = scratch / EXTRACTED
    avif_first = chapter_plan.outcomes.avif is not None
    blanked = image.blank_pages(chapter_plan, x_dir, scratch / "blank") if avif_first else {}
    rendered: list[Path]
    encoded: list[Path]
    rendered = encoded = []
    if chapter_plan.render:
        if not blanked or sampling:
            rendered = encoded = image.render(chapter_plan, x_dir, scratch / "render")
        if blanked:
            sources = image.sources(chapter_plan, x_dir, blanked, scratch / "sources")
            encoded = image.render(chapter_plan, sources, scratch / "render-blanked")
    return _Pages(blanked, encoded, calibrate.units(chapter_plan, x_dir, rendered) if sampling else {})


def _write_staged(staged: Path, rec: image.ChapterRecord, out_dir: Path, chapter_plan: plan.ChapterPlan) -> int:
    """Write the output pages as the staged CBZ and post-check it; return its size.

    A staged file that fails is deleted: none is left behind without its chapter's record.
    """
    try:
        size = archive.write(staged, [(p["name"], (out_dir / p["name"]).read_bytes()) for p in rec["output"]])
        _post_check(staged, rec, chapter_plan)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return size


def _result(chapter_plan: plan.ChapterPlan, rec: image.ChapterRecord, cover: _EncodedCover | None) -> ConvertResult:
    """Return the chapter's convert_result from the encode's record and the volume cover it was given."""
    return {
        "route": rec["route"],
        "outcome": rec["outcome"],
        "quality": rec["quality"],
        "mean_score": rec["mean_score"],
        "min_score": rec["min_score"],
        "pages_in": rec["pages"],
        "pages_out": len(rec["output"]),
        "dropped": rec["dropped"],
        "reshape": rec.get("reshape"),
        "blanked": rec["blanked"],
        "cover": None if rec["cover"] is None else {**rec["cover"], "volume": None if cover is None else cover.volume},
        "cover_skipped": _cover_skipped(chapter_plan, rec, cover),
        "gray": rec["gray"],
        "avif_rejected": rec.get("avif_rejected"),
        "bytes_in": rec["bytes_in"],
        "bytes_out": rec["bytes_out"],
    }
