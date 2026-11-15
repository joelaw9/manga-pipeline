"""Stage convert (SPEC "Chapter stages", "Conversion", "Calibration"): re-encode the download's pages as the bulk
converter does and write the store-mode CBZ of pages at <paths.staging>/<database id, first 8 characters>-<chapter
id>-<claim count>.cbz. The database id in the name keeps a file left in staging by another database from passing as
this chapter's output; the claim count keeps 2 claims of 1 chapter from writing 1 file.

Every work file is in <paths.scratch>/<chapter id>-<claim count>/, removed when the stage ends, whatever the outcome;
the folders a killed run left are removed by process at its start (process.clean_scratch), not here. Nothing is written
in the series folder.
"""

import functools
import json
import logging
import shutil

from manga_pipeline import archive, config, db, images, page_store, workflow
from manga_pipeline.convert import calibrate, check, covers, image, pages, plan, tools, vship

log = logging.getLogger(__name__)


@functools.cache
def _scorer(kind, path):
    """1 loaded, GPU-checked scorer per library path for the process: the library and its CUDA context are made once,
    not per chapter. kind: the scorer class (vship.Scorer)."""
    return kind(path)


def staged_name(conn, chapter):
    """<database id, 8 characters>-<chapter id>-<claim count>.cbz: 2 claims of 1 chapter never write 1 name."""
    return f"{db.database_id(conn)[:8]}-{workflow.claim_name(chapter)}.cbz"


def _edit(conn, chapter_id):
    """The chapter's checked edit; raises plan.EditError when the row is missing or malformed (a program error)."""
    row = conn.execute("SELECT edit FROM chapter_edit WHERE chapter_id = %s", (chapter_id,)).fetchone()
    if row is None:
        raise plan.EditError("no chapter_edit row: the junk stage wrote none (a program error)")
    return plan.check_edit(row[0])


def _layout(conn, series_id):
    """(layout, page aspects or None) of the series (series_conversion; page_aspect is JSON text)."""
    row = conn.execute(
        "SELECT layout, page_aspect FROM series_conversion WHERE series_id = %s", (series_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"series {series_id} has no layout (series_conversion)")
    aspect = None if row["page_aspect"] is None else json.loads(row["page_aspect"])
    if aspect is not None and (
        not isinstance(aspect, list) or not aspect or not all(isinstance(a, (int, float)) for a in aspect)
    ):
        raise RuntimeError(f"series {series_id} page_aspect must be a non-empty list of numbers, got {aspect!r}")
    return row["layout"], aspect


def _samples_wanted(row, chapter_id):
    """Does this group take samples from this chapter: no row yet, or open, short of calibrate.MAX_SAMPLES and none
    from this chapter yet?"""
    return row is None or not (
        row["fixed"]
        or len(row["samples"]) >= calibrate.MAX_SAMPLES
        or any(s["chapter_id"] == chapter_id for s in row["samples"])
    )


def _calibrate(ctx, chapter, chapter_plan, x_dir, rendered, work, pool, scorer, tool_paths):
    """Add this chapter's samples to the series' open groups its AVIF outcome encodes; returns {group: q} after."""
    stored = calibrate.rows(ctx.conn, chapter["series_id"])
    if not any(_samples_wanted(stored.get(g), chapter["id"]) for g in plan.avif_groups(chapter_plan)):
        return calibrate.quality(stored)  # (a blanked rendered chapter then has no unblanked render to sample)
    groups = calibrate.units(chapter_plan, x_dir, rendered)
    for group, group_units in groups.items():
        row = stored.get(group)
        if not group_units or not _samples_wanted(row, chapter["id"]):
            continue
        picked = calibrate.pick(group_units, {s["sha1"] for s in row["samples"]} if row else set())
        if not picked:
            continue
        scores = calibrate.probe(pool, scorer, picked, work / f"calibrate-{group}", tool_paths)
        new = [
            {
                "sha1": u["sha1"],
                "rendered": u["rendered"],
                "bytes_in": u["path"].stat().st_size,
                "scores": scores[u["sha1"]],
            }
            for u in picked
        ]
        stored[group] = calibrate.add(
            ctx.conn, chapter["series_id"], group, chapter["id"], new, lambda conn: workflow.holds_claim(ctx, chapter)
        )
        log.info("series %s %s calibration: %s", chapter["series_id"], group, stored[group]["why"])
    return calibrate.quality(stored)


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


def run(ctx, chapter):
    cfg = ctx.cfg
    try:
        edit = _edit(ctx.conn, chapter["id"])
    except plan.EditError as e:
        return workflow.NeedsHuman(str(e))
    layout, aspect = _layout(ctx.conn, chapter["series_id"])
    scratch = config.abs_path(cfg["paths"]["scratch"]) / workflow.claim_name(chapter)
    try:
        return _convert(ctx, chapter, edit, layout, aspect, scratch)
    finally:
        try:
            shutil.rmtree(scratch)
        except FileNotFoundError:
            pass
        except OSError as e:  # the outcome stands; the folder is reported, not hidden
            log.warning("chapter %s: scratch folder %s not removed: %s", chapter["id"], scratch, e)


def _convert(ctx, chapter, edit, layout, aspect, scratch):
    cfg = ctx.cfg
    cbz = config.to_local(cfg, chapter["download_path"])
    x_dir, render_dir, out_dir, work = scratch / "x", scratch / "render", scratch / "out", scratch / "work"
    files, problems = pages.extract(cbz, x_dir)
    if problems:
        return workflow.NeedsHuman("; ".join(problems))
    measured = images.measure_pages(cbz, known=page_store.load(ctx.conn, chapter["id"]))
    stored = calibrate.quality(calibrate.rows(ctx.conn, chapter["series_id"]))
    try:
        chapter_plan = plan.build(f"chapter {chapter['id']}", measured, files, edit, layout, aspect, stored)
    except (plan.NeedsHuman, plan.EditError) as e:
        return workflow.NeedsHuman(str(e))
    if not chapter_plan.kept():
        return workflow.NeedsHuman("the chapter edit drops every page")
    cover_row = ctx.conn.execute("SELECT * FROM volume_cover WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    cover = None
    if cover_row is not None:
        cover = {"data": covers.load(cfg, cover_row), "width": covers.width(chapter_plan)}
    tool_paths = {name: str(config.abs_path(cfg["tools"][name])) for name in tools.NAMES}
    scorer = _scorer(vship.Scorer, config.abs_path(cfg["tools"]["vship"]))
    with image.pool(cfg["convert"]["workers"]) as workers:
        if cover is not None:  # before the route: an AVIF cover that passed can make the chapter AVIF
            cover["avif"] = image.encode_cover(
                workers, scorer, cover["data"], cover["width"], work / "cover", tool_paths
            )
        plan.routes(chapter_plan, None if cover is None else cover["avif"]["ok"])
        if chapter_plan.route == "untouched-flag":
            log.warning(
                "chapter %s: formats %s mix with others: untouched",
                chapter["id"],
                sorted({q["format"] for q in chapter_plan.kept()}),
            )
        # logo blanks apply to the AVIF outcomes only; calibration samples take the unblanked pages (as the bulk
        # converter), so a rendered chapter with blanks is rendered unblanked too when a group wants its samples
        avif_first = chapter_plan.outcomes[0]["outcome"] == "avif"
        blanked = image.blank_pages(chapter_plan, x_dir, scratch / "blank") if avif_first else {}
        rendered = encoded = []
        if chapter_plan.render:
            stored = calibrate.rows(ctx.conn, chapter["series_id"])
            sampling = any(_samples_wanted(stored.get(g), chapter["id"]) for g in plan.avif_groups(chapter_plan))
            if not blanked or sampling:
                rendered = encoded = image.render(chapter_plan, x_dir, render_dir)
            if blanked:
                sources = image.sources(chapter_plan, x_dir, blanked, scratch / "sources")
                encoded = image.render(chapter_plan, sources, scratch / "render-blanked")
        chapter_plan.quality = _calibrate(
            ctx, chapter, chapter_plan, x_dir, rendered, work, workers, scorer, tool_paths
        )
        rec = image.process_chapter(
            x_dir, encoded, blanked, cover, out_dir, work / "encode", workers, scorer, chapter_plan, tool_paths
        )
    staged = config.abs_path(cfg["paths"]["staging"]) / staged_name(ctx.conn, chapter)
    try:
        size = archive.write(staged, [(p["name"], (out_dir / p["name"]).read_bytes()) for p in rec["output"]])
        fails = check.check(staged, rec, x_dir, chapter_plan.kept())
        if fails:
            raise RuntimeError(f"post-check failed: {'; '.join(fails)}")
    except BaseException:
        staged.unlink(missing_ok=True)  # a staged file without its chapter's record is never left behind
        raise
    result = {
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
        "cover": rec["cover"] and rec["cover"] | {"volume": cover_row["volume"]},
        "cover_skipped": _cover_skipped(chapter_plan, rec, cover),
        "gray": rec["gray"],
        "avif_rejected": rec.get("avif_rejected"),
        "bytes_in": rec["bytes_in"],
        "bytes_out": rec["bytes_out"],
    }
    output_pages = _output_pages(out_dir, rec)
    if result["cover_skipped"]:
        log.warning(
            "chapter %s: volume %s cover not inserted: %s", chapter["id"], cover_row["volume"], result["cover_skipped"]
        )

    def record(conn):
        conn.execute(
            "UPDATE chapter SET staged_path = %s, staged_size = %s, convert_result = %s,"
            " output_pages = %s WHERE id = %s",
            (config.to_stored(cfg, staged), size, json.dumps(result), json.dumps(output_pages), chapter["id"]),
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
