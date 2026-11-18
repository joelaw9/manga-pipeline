"""Stage junk (SPEC "Chapter stages", "State", "Bad downloads", "Junk decisions"): checks, then scan or decide.

First the put-aside check (the series' baseline, taken at validate, recorded what was handled), then the library-copy
check (SPEC "Library copies"), then a numbered notice title's judgement (notices.py), then junk.

A chapter with no current scan is scanned (pages extracted to its scratch folder, measured, its units keyed, OCR on
the strip units no decision covers, every content page's logo measured) and stays ready (Ready), so every ready junk
chapter of a series is scanned before any is decided. A chapter with a current scan is decided from it with no
decode: done with its chapter edit, waiting on junk_decision with its candidates (their images written first), or
needs_human (a removal that would leave too little; a candidate Claude was unsure of or past the review limit).
"""

import dataclasses
import json
import os
import re
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from PIL import Image

from manga_pipeline import (
    archive,
    config,
    db,
    errors,
    handled,
    images,
    library_copy,
    notices,
    page_store,
    strip,
    volumes,
    workflow,
)
from manga_pipeline.convert import plan
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates, logo_units, ocr, page_units, store, strip_units

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


class StoredStripScan(strip_units.StripScan):
    """A strip chapter's scan as junk_scan holds it: strip_units.scan's, with its content pages and their logos."""

    content: list[int]  # the content pages' download indexes
    marks: list[logo_units.StripLogo]


type Scan = StoredStripScan | page_units.PageScan | page_units.SplitScan  # junk_scan.scan, told apart by its layout


def read_scan(text: str) -> Scan:
    """Return a chapter's stored scan (junk_scan.scan, JSON text). Unchecked."""
    return json.loads(text)


def _candidate_ids(text: str) -> list[int]:
    """Return the candidates a chapter waits on (junk_scan.candidates, JSON text): their ids. Unchecked."""
    return json.loads(text)


def run(ctx: workflow.Context, chapter: db.Row) -> workflow.OUTCOMES:
    """Run the put-aside, library-copy and notice checks, then scan the chapter, or decide it from its current scan."""
    series = db.one_row(ctx.conn, "SELECT * FROM series WHERE id = %s", (chapter["series_id"],))
    why = handled.put_aside(ctx, chapter, chapter["content_digest"])
    if why:
        return workflow.Dropped(why)
    outcome = library_copy.judge(ctx, chapter) or notices.judge(ctx, chapter)
    if outcome:
        return outcome
    if not chapter["content_digest"]:
        msg = f"chapter {chapter['id']} has no content digest: discovery sets it before junk"
        raise ValueError(msg)
    key = store.series_key(series)
    row = current_scan(ctx.conn, chapter)
    if row is None:
        return scan(ctx, chapter, series, key)
    return decide(ctx, chapter, key, row)


def current_scan(conn: db.Connection, chapter: db.Row) -> db.Row | None:
    """Return the chapter's junk_scan row when it is current, else None.

    Current: same series, same content digest, holding logos (a scan made before logo detection holds none).
    """
    current = workflow.SCANNED.format(c=sql.Identifier("c"))
    query = sql.SQL("SELECT s.* FROM chapter c JOIN junk_scan s ON s.chapter_id = c.id WHERE c.id = %s AND {}")
    return conn.execute(query.format(current), (chapter["id"],)).fetchone()


# ---------------------------------------------------------------- pages on local disk


def _scratch(ctx: workflow.Context, chapter: db.Row) -> Path:
    return config.abs_path(ctx.cfg["paths"]["scratch"]) / workflow.claim_name(chapter) / "junk"


def extract(
    ctx: workflow.Context, chapter: db.Row, work: Path
) -> tuple[list[Path], list[str], list[strip.Size], list[images.MeasuredPage]]:
    """Write the download's pages to `work` in archive order; return (paths, names, sizes, measurements)."""
    cbz = config.to_local(ctx.cfg, chapter["download_path"])
    measured = images.measure_pages(cbz, page_store.load(ctx.conn, chapter["id"]))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    paths: list[Path]
    names: list[str]
    paths, names = [], []
    for i, (name, data) in enumerate(archive.pages(cbz)):
        path = work / f"{i:04d}{Path(name).suffix.lower()}"
        path.write_bytes(data)
        paths.append(path)
        names.append(name)
    sizes = [(m["width"], m["height"]) for m in measured]
    return paths, names, sizes, measured


def _layout(ctx: workflow.Context, chapter: db.Row, series: db.Row, content_paths: list[Path]) -> str:
    """Return the series' layout (SPEC "State").

    Its override file's `layout`, stored as the series' layout; else the stored one; else measured, for a new series
    over up to strip.SAMPLE_CHAPTERS of its chapters (this one among them), for any other over this chapter.
    OverrideError for a malformed override file.
    """
    wanted = volumes.override_layout(config.abs_path(ctx.cfg["paths"]["naming_overrides"]), series["rules_key"])
    if wanted:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                """INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, %s, 'override')
                ON CONFLICT (series_id) DO UPDATE SET layout = excluded.layout, origin = excluded.origin
                WHERE series_conversion.layout <> excluded.layout OR series_conversion.origin <> excluded.origin""",
                (series["id"], wanted),
            )
        return wanted
    stored = ctx.conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series["id"],)).fetchone()
    if stored is not None:
        return stored[0]
    chapters = [content_paths]
    if series["origin"] == "new":
        chapters += _sample(ctx, chapter, series)
    return strip.ensure_series_layout(ctx.conn, series["id"], chapters)


def _sample(ctx: workflow.Context, chapter: db.Row, series: db.Row) -> list[list[Path]]:
    """Return the content pages of up to SAMPLE_CHAPTERS - 1 more of a new series' chapters past validate.

    The chapters are spread out by id (first, last, then evenly between: first, middle and last for 3), this chapter
    counting as 1 of them, each extracted to the claim's scratch folder; only chapters whose download this machine
    can use and that still exist (the claim query's count, SPEC "State").
    """
    rows = ctx.conn.execute(
        """SELECT id, download_path FROM chapter
        WHERE series_id = %s AND kind = 'download' AND status <> 'done' AND stage NOT IN ('discover', 'validate')
        ORDER BY id""",
        (series["id"],),
    ).fetchall()
    usable = [
        r
        for r in rows
        if r["download_path"]
        and not (os.name == "nt" and re.search(workflow.WINDOWS_UNUSABLE, r["download_path"]))
        and (r["id"] == chapter["id"] or errors.exists(config.to_local(ctx.cfg, r["download_path"])))
    ]
    n, last = len(usable), strip.SAMPLE_CHAPTERS - 1
    spread = [0, n - 1, *(k * n // last for k in range(1, last))]
    picked = list(dict.fromkeys(usable[i]["id"] for i in spread)) if usable else []
    others = [i for i in picked if i != chapter["id"]][: strip.SAMPLE_CHAPTERS - 1]  # this chapter is always measured
    out: list[list[Path]] = []
    for r in usable:
        if r["id"] not in others:
            continue
        work = _scratch(ctx, chapter).parent / "sample" / str(r["id"])
        work.mkdir(parents=True, exist_ok=True)
        pages = archive.pages(config.to_local(ctx.cfg, r["download_path"]))
        content = strip.content_indexes([name for name, _ in pages])
        paths: list[Path] = []
        for i in content:
            name, data = pages[i]
            path = work / f"{i:04d}{Path(name).suffix.lower()}"
            path.write_bytes(data)
            paths.append(path)
        out.append(paths)
    return out


def _aspects(ctx: workflow.Context, series: db.Row, sizes: Sequence[strip.Size]) -> list[float]:
    """Return the series' page aspect candidates: stored, else computed from this chapter and stored."""
    row = db.one_row(ctx.conn, "SELECT page_aspect FROM series_conversion WHERE series_id = %s", (series["id"],))
    if row["page_aspect"] is not None:
        return plan.read_page_aspect(series["id"], row["page_aspect"])
    aspects = strip.page_aspect([sizes])
    if aspects is not None:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE series_conversion SET page_aspect = coalesce(page_aspect, %s) WHERE series_id = %s",
                (json.dumps(aspects), series["id"]),
            )
    return aspects or []


# ---------------------------------------------------------------- scan


def scan(ctx: workflow.Context, chapter: db.Row, series: db.Row, key: str) -> workflow.OUTCOMES:
    """Scan the chapter's pages by the series' layout and record the scan; the chapter stays ready (Ready).

    The pages are extracted to the claim's scratch folder, removed afterwards. A malformed override file, or strip OCR
    text holding a NUL character, is needs_human.
    """
    work = _scratch(ctx, chapter)
    try:
        paths, names, sizes, measured = extract(ctx, chapter, work)
        content = strip.content_indexes(names)
        try:
            layout = _layout(ctx, chapter, series, [paths[i] for i in content])
        except volumes.VolumeError as e:
            return workflow.NeedsHuman(str(e))
        if layout == "strip":
            sc, units = _scan_strip(ctx, key, paths, sizes, content)
            nul = _nul_rows(sc)
            if nul:  # PostgreSQL's jsonb cannot hold it: storing the scan would fail every claim query
                rows = ", ".join(f"{s}-{e}" for s, e in nul)
                return workflow.NeedsHuman(f"OCR text of strip rows {rows} holds a NUL character: it cannot be stored")
        else:
            uniform = [m["junk_signature"] is None for m in measured]
            sc = page_units.scan(paths, names, sizes, uniform, _aspects(ctx, series, sizes))
            units = _page_units(sc)
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)  # the claim's folder: nothing of it is kept
    at = workflow.now(ctx.conn)
    seen = candidates.seen_rows(key, chapter["identity"], units, at)

    def record(conn: db.Connection) -> None:
        conn.execute(
            """INSERT INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (chapter_id) DO UPDATE SET series_id = excluded.series_id,
                content_digest = excluded.content_digest, scan = excluded.scan, scanned_at = excluded.scanned_at,
                candidates = '[]'""",
            (chapter["id"], chapter["series_id"], chapter["content_digest"], json.dumps(sc), at),
        )
        candidates.write_seen(conn, seen)

    return workflow.Ready(record=record)


def _scan_strip(
    ctx: workflow.Context, key: str, paths: Sequence[Path], sizes: Sequence[strip.Size], content: list[int]
) -> tuple[StoredStripScan, list[tuple[str, str]]]:
    cpaths = [paths[i] for i in content]
    rows: list[NDArray[np.bool_]]
    logos: list[logo_units.Logo]
    rows, logos = [], []
    for i, path in zip(content, cpaths, strict=True):
        im = strip.open_page(path)
        rows.append(strip.page_rows(im))
        logo = logo_units.measure(i, im)
        if logo is not None:
            logos.append(logo)
    st = strip.make_strip(cpaths, [sizes[i] for i in content], rows)
    placed = dict(zip(content, st.pages, strict=True))
    marks = [_strip_logo(logo, placed[logo["page"]]) for logo in logos]
    decisions = store.Decisions(ctx.conn, key)
    device = ctx.cfg["junk"]["ocr_device"]
    units = strip_units.scan(
        st,
        lambda im: ocr.text(im, device),
        lambda sig: decisions.decide("block", sig) is None,
    )
    sc: StoredStripScan = {**units, "content": content, "marks": marks}
    return sc, [("block", b["sig"]) for b in sc["blocks"]]


def _strip_logo(logo: logo_units.Logo, page: strip.LaidPage) -> logo_units.StripLogo:
    """Return a logo with its tight box's rows in the strip (its page laid there), end exclusive."""
    _, y0, _, y1 = logo["tight"]
    top, th, h = page["top"], page["th"], page["h"]
    return {**logo, "rows": [top + y0 * th // h, top + -(-y1 * th // h)]}


def _nul_rows(sc: strip_units.StripScan) -> list[tuple[int, int]]:
    """Return [(start, end)] of the strip units whose OCR text holds a NUL.

    Those are the edge blocks and their join pieces, the only units OCR reads.
    """
    units = [u for b in sc["blocks"] for u in (b, *b["joins"])]
    return [(u["start"], u["end"]) for u in units if "\x00" in u.get("text", "")]


def _page_units(sc: page_units.PageScan | page_units.SplitScan) -> list[tuple[str, str]]:
    if sc["split"] is True:
        return []
    out: list[tuple[str, str]] = []
    for u in sc["edges"]:
        if u["frame"]:
            out.append(("frame", u["frame"]))
        if u["sig"]:
            out.append(("page", u["sig"]))
    out += [("band", b["sig"]) for found in sc["bands"].values() for b in found]
    return out


# ---------------------------------------------------------------- decide


class _Applied(NamedTuple):
    """What decisions make of a chapter's scan: its edit, watermarks to remember, undecided units, refusal.

    Its chapter edit, the watermark rows [(series key, key)] to remember, the units left undecided (the layout's, then
    the logos'), and why the edit is refused (strip_units' kept_problem; None when it is not).
    """

    edit: plan.ChapterEdit
    watermarks: list[tuple[str, str]]
    undecided: Sequence[candidates.Unit]
    problem: str | None


def _apply(conn: db.Connection, sc: Scan, key: str, decide_unit: store.DecideUnit) -> _Applied:
    """Apply the decisions on a scan: its layout's, then the logos' on what that leaves.

    A strip's layout decisions take the series' known watermarks; a refused strip edit stops before the logos.
    """
    edit: plan.ChapterEdit = {
        "drop_pages": [],
        "bands": [],
        "join": [],
        "split": False,
        "strip_removed": [],
        "blank": [],
    }
    watermarks: list[tuple[str, str]] = []
    undecided: list[candidates.Unit]
    if sc["layout"] == "strip":
        known = conn.execute("SELECT key FROM junk_watermark WHERE series_key = %s", (key,)).fetchall()
        got = strip_units.decide(sc, decide_unit, [images.sig_from_str(r["key"]) for r in known])
        if got["kept_problem"]:
            return _Applied(edit, [], [], got["kept_problem"])
        edit["strip_removed"] = got["removed"]
        watermarks = [(key, k) for k in got["watermarks"]]
        logos = logo_units.decide(sc["marks"], logo_units.strip_survives(edit["strip_removed"]), decide_unit)
        undecided = [*got["undecided"], *logos["undecided"]]
    else:
        got = page_units.decide(sc, decide_unit)
        edit = {**edit, **got["edit"]}
        logos = logo_units.decide(sc["marks"], logo_units.page_survives(edit), decide_unit)
        undecided = [*got["undecided"], *logos["undecided"]]
    edit["blank"] = logos["blank"]
    return _Applied(edit, watermarks, undecided, None)


def _remember(conn: db.Connection, watermarks: Iterable[tuple[str, str]]) -> None:
    """With the outcome: the series' later chapters know these bands as watermarks."""
    db.executemany(
        conn, "INSERT INTO junk_watermark (series_key, key) VALUES (%s, %s) ON CONFLICT DO NOTHING", watermarks
    )


def _finished(
    ctx: workflow.Context, chapter: db.Row, edit: plan.ChapterEdit, watermarks: list[tuple[str, str]]
) -> workflow.OUTCOMES:
    """Return the outcome of a chapter that offers no candidate.

    The library copy check's (a duplicate dropped, or a better copy of a busy file held: no edit kept), else Done with
    its chapter edit.
    """
    copy = library_copy.after_junk(ctx, chapter, edit)
    if copy is not None:

        def closed(conn: db.Connection) -> None:
            _remember(conn, watermarks)
            if isinstance(copy, workflow.Dropped):  # a held copy keeps its scan for its retry
                conn.execute("DELETE FROM junk_scan WHERE chapter_id = %s", (chapter["id"],))
            if copy.record:
                copy.record(conn)

        return dataclasses.replace(copy, record=closed)

    def done(conn: db.Connection) -> None:
        _remember(conn, watermarks)
        values = (chapter["id"], json.dumps(edit))
        conn.execute(
            "INSERT INTO chapter_edit (chapter_id, edit) VALUES (%s, %s) ON CONFLICT"
            " (chapter_id) DO UPDATE SET edit = excluded.edit",
            values,
        )
        conn.execute("DELETE FROM junk_scan WHERE chapter_id = %s", (chapter["id"],))

    return workflow.Done(record=done)


def decide(ctx: workflow.Context, chapter: db.Row, key: str, row: db.Row) -> workflow.OUTCOMES:
    """Decide the chapter from its current scan, with no decode.

    Done with its chapter edit when no candidate is left (or the library copy check's outcome, when it has one);
    waiting on junk_decision with its candidates (their images written first); or needs_human (a removal that would
    leave too little; a candidate Claude was unsure of or past the review limit).
    """
    sc = read_scan(row["scan"])
    decisions = store.Decisions(ctx.conn, key)

    def decide_unit(kind: str, text: str) -> db.Row | None:
        return decisions.decide(kind, store.parse_key(kind, text))

    applied = _apply(ctx.conn, sc, key, decide_unit)
    if applied.problem:
        return workflow.NeedsHuman(applied.problem)
    junk = ctx.cfg["junk"]
    words: ocr.WordLists = {
        "keywords": junk["keywords"],
        "weak": junk["weak"],
        "paired": junk["paired"],
        "suppress": junk["suppress"],
    }
    picks = candidates.select(ctx.conn, key, chapter["identity"], applied.undecided, words)
    if not picks:
        return _finished(ctx, chapter, applied.edit, applied.watermarks)
    limit = ctx.cfg["junk"]["review_limit"]
    rows = [candidates.find(ctx.conn, decisions, p["kind"], p["key"]) for p in picks]
    held = [r for r in rows if r is not None and (r["verdict"] == "unsure" or candidates.stuck(r, limit))]
    if held:
        # the chapter keeps the candidates it waits on open, so junk decide can name them
        def hold(conn: db.Connection) -> None:
            _remember(conn, applied.watermarks)
            ids = sorted(r["id"] for r in rows if r is not None)
            conn.execute("UPDATE junk_scan SET candidates = %s WHERE chapter_id = %s", (json.dumps(ids), chapter["id"]))

        return workflow.NeedsHuman(held_reason(held, limit), record=hold)
    new = [p for p, r in zip(picks, rows, strict=True) if r is None]
    if new:
        write_images(ctx, chapter, key, new)
    at = workflow.now(ctx.conn)

    def wait(conn: db.Connection) -> None:
        _remember(conn, applied.watermarks)
        ids = candidates.join(conn, key, picks, at)
        conn.execute("UPDATE junk_scan SET candidates = %s WHERE chapter_id = %s", (json.dumps(ids), chapter["id"]))

    return workflow.Waiting("junk_decision", {"candidates": len(picks)}, record=wait)


def held_reason(rows: Iterable[db.Row], limit: int) -> str:
    """Return the needs_human reason naming each held candidate: Claude was unsure, or undecided after limit reviews."""
    parts = [
        f"candidate {r['id']} ({r['kind']}): "
        + ("Claude was unsure" if r["verdict"] == "unsure" else f"undecided after {limit} reviews")
        for r in rows
    ]
    return "junk " + "; ".join(parts) + " -> junk decide ID junk|keep, then retry"


def junk_decision_over(ctx: workflow.Context, chapter: db.Row) -> bool | workflow.NeedsHuman:
    """Return whether the junk_decision wait is over (SPEC "Junk decisions"): True, NeedsHuman or False.

    Over (True) when the chapter's scan is stale (it rescans) or a candidate it waits on is covered by a decision or
    gone: the chapter is decided again and waits on what is still undecided then (a decision can make another candidate
    moot: a dropped page's band). Else NeedsHuman when one is unsure or stuck; else False.
    """
    row = ctx.conn.execute("SELECT * FROM junk_scan WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    if row is None or current_scan(ctx.conn, chapter) is None:
        return True
    limit = ctx.cfg["junk"]["review_limit"]
    ids = _candidate_ids(row["candidates"])
    held: list[db.Row] = []
    decisions = store.by_series(ctx.conn)
    for cid in ids:
        c = ctx.conn.execute("SELECT * FROM junk_candidate WHERE id = %s", (cid,)).fetchone()
        if c is None:
            return True  # cleaned up: it was closed
        if decisions(c["series_key"]).decide(c["kind"], store.parse_key(c["kind"], c["key"])) is not None:
            return True
        if c["verdict"] == "unsure" or candidates.stuck(c, limit):
            held.append(c)
    if held:
        return workflow.NeedsHuman(held_reason(held, limit))
    return not ids


# ---------------------------------------------------------------- candidate images


def write_images(ctx: workflow.Context, chapter: db.Row, key: str, picks: Sequence[candidates.Candidate]) -> None:
    """Write the images of new candidates (SPEC "Junk decisions", candidates).

    The download's pages are extracted again to the scratch folder, which is removed afterwards. A strip unit is
    rendered over the content pages, as the scan laid them.
    """
    work = _scratch(ctx, chapter)
    try:
        paths, names, sizes, _ = extract(ctx, chapter, work)
        content = strip.content_indexes(names)
        for p in picks:
            unit = p["unit"]
            if "logo" in unit:  # a logo variant
                page = images.open_flat(paths[unit["page"]])[0].convert("RGB")
                unit_im, context_im = logo_units.review_images(page, unit["logo"])
            elif "start" in unit:  # a strip unit: an edge block, its probe or a tail piece
                unit_im, context_im = _strip_images(content, paths, sizes, unit)
            else:  # a page unit: an edge page or frame, or a band
                unit_im, context_im = _page_images(paths, unit)
            info: dict[str, str | int] = {
                "series": key,
                "kind": p["kind"],
                "reason": p["reason"],
                "text": p["unit"].get("text", ""),
                "chapters": candidates.chapters_showing(ctx.conn, key, seen_kind(p["kind"]), p["key"]),
            }
            name = candidates.folder_name(key, p["kind"], p["key"], chapter)
            workflow.holds_claim(ctx, chapter)  # after rendering, immediately before the folder is written
            candidates.write_images(ctx.cfg, name, unit_im, context_im, info)
            p["folder"] = name  # the row join opens names it
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def seen_kind(kind: str) -> str:
    """Return the kind a unit is recorded as seen under (a probe is part of a seen edge block)."""
    return "block" if kind == "probe" else kind


def _strip_images(
    content: Sequence[int],
    paths: Sequence[Path],
    sizes: Sequence[strip.Size],
    u: strip_units.BlockUnit | strip_units.PieceUnit | candidates.ProbeUnit,
) -> tuple[Image.Image, Image.Image]:
    width, laid, height = strip.strip_layout([sizes[i] for i in content])
    pages: list[strip.StripPage] = [{**p, "path": str(paths[i])} for p, i in zip(laid, content, strict=True)]
    unit_im = strip.render_rows(pages, width, u["start"], u["end"])
    lo, hi = max(0, u["start"] - candidates.CONTEXT_PX), min(height, u["end"] + candidates.CONTEXT_PX)
    return unit_im, strip.render_rows(pages, width, lo, hi)


def _page_images(
    paths: Sequence[Path], u: page_units.PageUnit | page_units.BandUnit
) -> tuple[Image.Image, Image.Image]:
    i = u["page"]
    page = images.open_flat(paths[i])[0].convert("RGB")
    unit_im = page.crop(_box(u["box"])) if "box" in u else page  # a band's crop
    near = [images.open_flat(paths[j])[0].convert("RGB") for j in (i - 1, i, i + 1) if 0 <= j < len(paths)]
    h = 800
    near = [im.resize((max(1, round(im.width * h / im.height)), h)) for im in near]
    context = Image.new("RGB", (sum(im.width for im in near) + 8 * (len(near) - 1), h), "white")
    x = 0
    for im in near:
        context.paste(im, (x, 0))
        x += im.width + 8
    return unit_im, context


def _box(box: Sequence[int]) -> tuple[int, int, int, int]:
    """Return a band's box [x0, y0, x1, y1] as the 4-tuple Pillow's crop takes."""
    x0, y0, x1, y1 = box
    return x0, y0, x1, y1
