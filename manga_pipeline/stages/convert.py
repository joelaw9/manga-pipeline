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
from dataclasses import dataclass

from manga_pipeline import archive, config, db, images, page_store, workflow
from manga_pipeline.convert import calibrate, check, covers, image, pages, plan, tools, vship

log = logging.getLogger(__name__)
# A chapter's scratch folders: its extracted download and the pages it writes (process_chapter, then the staged file).
EXTRACTED, OUTPUT = "x", "out"


@functools.cache
def loaded_scorer(kind, path):
    """Return 1 loaded, GPU-checked scorer per library path for the process.

    The library and its CUDA context are made once, not per chapter. kind: the scorer class (vship.Scorer).
    """
    return kind(path)


@contextlib.contextmanager
def encoder(cfg):
    """Provide the image.Encoder of 1 chapter's (or 1 cover's) encodes.

    It holds the configured image tools, the process's loaded scorer and fresh page workers, stopped on leaving.
    """
    tool_paths = {name: str(config.abs_path(cfg["tools"][name])) for name in tools.NAMES}
    scorer = loaded_scorer(vship.Scorer, config.abs_path(cfg["tools"]["vship"]))
    with image.pool(cfg["convert"]["workers"]) as workers:
        yield image.Encoder(workers, scorer, tool_paths)


def staged_name(conn, chapter):
    """<database id, 8 characters>-<chapter id>-<claim count>.cbz: 2 claims of 1 chapter never write 1 name."""
    return f"{db.database_id(conn)[:8]}-{workflow.claim_name(chapter)}.cbz"


def _edit(conn, chapter_id):
    """Return the chapter's checked edit.

    Raises plan.EditError when the row is missing or malformed (a program error).
    """
    row = conn.execute("SELECT edit FROM chapter_edit WHERE chapter_id = %s", (chapter_id,)).fetchone()
    if row is None:
        msg = "no chapter_edit row: the junk stage wrote none (a program error)"
        raise plan.EditError(msg)
    return plan.check_edit(row[0])


def _layout(conn, series_id):
    """Return the series' plan.SeriesLayout (series_conversion; page_aspect is JSON text)."""
    row = conn.execute(
        "SELECT layout, page_aspect FROM series_conversion WHERE series_id = %s", (series_id,)
    ).fetchone()
    if row is None:
        msg = f"series {series_id} has no layout (series_conversion)"
        raise RuntimeError(msg)
    aspect = None if row["page_aspect"] is None else json.loads(row["page_aspect"])
    if aspect is not None and (
        not isinstance(aspect, list) or not aspect or not all(isinstance(a, (int, float)) for a in aspect)
    ):
        msg = f"series {series_id} page_aspect must be a non-empty list of numbers, got {aspect!r}"
        raise RuntimeError(msg)
    return plan.SeriesLayout(row["layout"], aspect)


def _samples_wanted(row, chapter_id):
    """Return whether this group takes samples from this chapter.

    It does with no row yet, or open, short of calibrate.MAX_SAMPLES and none from this chapter yet.
    """
    return row is None or not (
        row["fixed"]
        or len(row["samples"]) >= calibrate.MAX_SAMPLES
        or any(s["chapter_id"] == chapter_id for s in row["samples"])
    )


def _sampling(stored, chapter_plan, chapter_id):
    """Return whether a group the plan's AVIF outcome encodes takes samples from this chapter.

    stored: calibrate.rows.
    """
    return any(_samples_wanted(stored.get(g), chapter_id) for g in plan.avif_groups(chapter_plan))


def _calibrate(ctx: workflow.Context, chapter, groups, *, encoder, work):
    """Add this chapter's samples to the series' open groups; return {group: q} after.

    groups is calibrate.units of the pages it samples ({} when no group wants samples).
    """
    stored = calibrate.rows(ctx.conn, chapter["series_id"])
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
    return calibrate.quality(stored)


def _samples(encoder, picked, work_dir) -> list[calibrate.NewSample]:
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


def _cover_skipped(chapter_plan, rec, cover):
    """Why a chapter that takes a volume cover went out without it (None when it has it, or takes none)."""
    if cover is None or rec["cover"]:
        return None
    fmts = sorted({q["format"] for q in chapter_plan.kept() if not q["cover"]})
    wanted = images.AVIF if rec["outcome"] == "avif" else plan.cover_format(fmts)
    if wanted == images.AVIF:
        return f"its AVIF cover scored {cover['avif']['score']}, below the floor"
    return f"its {rec['outcome']} pages ({', '.join(fmts)}) share no cover format"


def _output_pages(out_dir, rec):
    out = []
    for i, p in enumerate(rec["output"]):
        w, h = images.page_size(out_dir / p["name"])
        out.append(
            {"index": i, "name": p["name"], "width": w, "height": h, "bytes": (out_dir / p["name"]).stat().st_size}
        )
    return out


def _post_check(staged, rec, x_dir, chapter_plan):
    """RuntimeError naming every failed post-check of the staged file."""
    fails = check.check(staged, rec, x_dir, chapter_plan.kept())
    if fails:
        msg = f"post-check failed: {'; '.join(fails)}"
        raise RuntimeError(msg)


def run(ctx: workflow.Context, chapter):
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


def _convert(ctx: workflow.Context, chapter, edit, layout, *, scratch):
    x_dir, out_dir = scratch / EXTRACTED, scratch / OUTPUT
    try:
        chapter_plan = _plan(ctx, chapter, edit, layout, x_dir)
    except (plan.NeedsHuman, plan.EditError) as e:
        return workflow.NeedsHuman(str(e))
    cover = _volume_cover(ctx, chapter, chapter_plan)
    rec = _encode(ctx, chapter, chapter_plan, cover, scratch)
    staged = config.abs_path(ctx.cfg["paths"]["staging"]) / staged_name(ctx.conn, chapter)
    size = _write_staged(staged, rec, out_dir, x_dir, chapter_plan)
    result = _result(chapter_plan, rec, cover)
    output_pages = _output_pages(out_dir, rec)
    if result["cover_skipped"]:
        log.warning(
            "chapter %s: volume %s cover not inserted: %s",
            chapter["id"],
            cover and cover["volume"],
            result["cover_skipped"],
        )

    def record(conn):
        conn.execute(
            "UPDATE chapter SET staged_path = %s, staged_size = %s, convert_result = %s,"
            " output_pages = %s WHERE id = %s",
            (config.to_stored(ctx.cfg, staged), size, json.dumps(result), json.dumps(output_pages), chapter["id"]),
        )

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


def _plan(ctx: workflow.Context, chapter, edit, layout, x_dir):
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


def _volume_cover(ctx: workflow.Context, chapter, chapter_plan):
    """Return the chapter's volume cover, or None when it takes none.

    The cover is {"volume", "data", "width"}: read and checked, at the width it goes in at.
    """
    row = ctx.conn.execute("SELECT * FROM volume_cover WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    if row is None:
        return None
    return {"volume": row["volume"], "data": covers.load(ctx.cfg, row), "width": covers.width(chapter_plan)}


def _encode(ctx: workflow.Context, chapter, chapter_plan, cover, scratch):
    """Execute the plan with the run's encoder; return image.process_chapter's record.

    The volume cover's AVIF first (its result in cover["avif"]), then the route, the pages, the calibration samples and
    the encode.
    """
    work = scratch / "work"
    with encoder(ctx.cfg) as enc:
        if cover is not None:  # before the route: an AVIF cover that passed can make the chapter AVIF
            cover["avif"] = enc.cover(cover["data"], cover["width"], work / "cover")
        _route(chapter, chapter_plan, cover)
        sampling = _sampling(calibrate.rows(ctx.conn, chapter["series_id"]), chapter_plan, chapter["id"])
        prepared = _pages(chapter_plan, scratch, sampling=sampling)
        chapter_plan.quality = _calibrate(ctx, chapter, prepared.units, encoder=enc, work=work)
        return image.process_chapter(
            image.PageFiles.of(scratch / EXTRACTED, prepared.encoded, prepared.blanked),
            scratch / OUTPUT,
            work_dir=work / "encode",
            encoder=enc,
            chapter_plan=chapter_plan,
        )


def _route(chapter, chapter_plan, cover):
    """Set the plan's route and outcomes (cover: the volume cover with its AVIF encode, when it takes one).

    A mix of formats that stays untouched is logged.
    """
    plan.routes(chapter_plan, cover=cover)
    if chapter_plan.route == "untouched-flag":
        log.warning(
            "chapter %s: formats %s mix with others: untouched",
            chapter["id"],
            sorted({q["format"] for q in chapter_plan.kept()}),
        )


@dataclass
class _Pages:
    """The page step's outputs: the blanked pages, the rendered PNGs and the calibration units.

    The blanked pages ({page name: PNG}), the rendered PNGs the encode reads ([] when the plan renders nothing) and the
    calibration units of the pages the chapter samples ({} when it samples none).
    """

    blanked: dict
    encoded: list
    units: dict


def _pages(chapter_plan, scratch, *, sampling):
    """Make the pages the encode and the calibration read.

    Logo blanks apply to the AVIF outcomes only; calibration samples take the unblanked pages (as the bulk converter),
    so a rendered chapter with blanks is rendered unblanked too when a group wants its samples (sampling).
    """
    x_dir = scratch / EXTRACTED
    avif_first = chapter_plan.outcomes[0]["outcome"] == "avif"
    blanked = image.blank_pages(chapter_plan, x_dir, scratch / "blank") if avif_first else {}
    rendered = encoded = []
    if chapter_plan.render:
        if not blanked or sampling:
            rendered = encoded = image.render(chapter_plan, x_dir, scratch / "render")
        if blanked:
            sources = image.sources(chapter_plan, x_dir, blanked, scratch / "sources")
            encoded = image.render(chapter_plan, sources, scratch / "render-blanked")
    return _Pages(blanked, encoded, calibrate.units(chapter_plan, x_dir, rendered) if sampling else {})


def _write_staged(staged, rec, out_dir, x_dir, chapter_plan):
    """Write the output pages as the staged CBZ and post-check it; return its size.

    A staged file that fails is deleted: none is left behind without its chapter's record.
    """
    try:
        size = archive.write(staged, [(p["name"], (out_dir / p["name"]).read_bytes()) for p in rec["output"]])
        _post_check(staged, rec, x_dir, chapter_plan)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return size


def _result(chapter_plan, rec, cover):
    """Return the chapter's convert_result from the encode's record."""
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
        "cover": rec["cover"] and rec["cover"] | {"volume": cover and cover["volume"]},
        "cover_skipped": _cover_skipped(chapter_plan, rec, cover),
        "gray": rec["gray"],
        "avif_rejected": rec.get("avif_rejected"),
        "bytes_in": rec["bytes_in"],
        "bytes_out": rec["bytes_out"],
    }
