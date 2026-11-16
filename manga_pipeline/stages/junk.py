"""Stage junk (SPEC "Chapter stages", "State", "Bad downloads", "Junk decisions"): first the put-aside check (the
series' baseline, taken at validate, recorded what was handled), then the library-copy check (SPEC "Library
copies"), then a numbered notice title's judgement (notices.py), then junk.

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
from pathlib import Path
from typing import NamedTuple

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
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates, logo_units, ocr, page_units, store, strip_units

WORDS = ("keywords", "weak", "paired", "suppress")


def run(ctx: workflow.Context, chapter):
    series = ctx.conn.execute("SELECT * FROM series WHERE id = %s", (chapter["series_id"],)).fetchone()
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


def current_scan(conn, chapter):
    """The chapter's junk_scan row when it is current (same series, same content digest, holding logos: a scan made
    before logo detection holds none), else None."""
    current = workflow.SCANNED.format(c=sql.Identifier("c"))
    query = sql.SQL("SELECT s.* FROM chapter c JOIN junk_scan s ON s.chapter_id = c.id WHERE c.id = %s AND {}")
    return conn.execute(query.format(current), (chapter["id"],)).fetchone()


# ---------------------------------------------------------------- pages on local disk


def _scratch(ctx: workflow.Context, chapter):
    return config.abs_path(ctx.cfg["paths"]["scratch"]) / workflow.claim_name(chapter) / "junk"


def extract(ctx: workflow.Context, chapter, work):
    """The download's pages written to `work` in archive order: (paths, names, sizes, measurements)."""
    cbz = config.to_local(ctx.cfg, chapter["download_path"])
    measured = images.measure_pages(cbz, page_store.load(ctx.conn, chapter["id"]))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    paths, names = [], []
    for i, (name, data) in enumerate(archive.pages(cbz)):
        path = work / f"{i:04d}{Path(name).suffix.lower()}"
        path.write_bytes(data)
        paths.append(path)
        names.append(name)
    sizes = [(m["width"], m["height"]) for m in measured]
    return paths, names, sizes, measured


def _layout(ctx: workflow.Context, chapter, series, content_paths):
    """The series' layout (SPEC "State"): its override file's `layout`, stored as the series' layout; else the stored
    one; else measured, for a new series over up to 3 of its chapters (this one among them), for any other over this
    chapter. OverrideError for a malformed override file."""
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


def _sample(ctx: workflow.Context, chapter, series):
    """The content pages of up to 2 more of a new series' chapters past validate (first, middle and last by id, this
    chapter counting as 1 of them), each extracted to the claim's scratch folder; only chapters whose download this
    machine can use and that still exist (the claim query's count, SPEC "State")."""
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
    picked = list(dict.fromkeys(r["id"] for r in (usable[0], usable[-1], usable[len(usable) // 2]))) if usable else []
    others = [i for i in picked if i != chapter["id"]][:2]  # this chapter is always measured: 2 more at most
    out = []
    for r in usable:
        if r["id"] not in others:
            continue
        work = _scratch(ctx, chapter).parent / "sample" / str(r["id"])
        work.mkdir(parents=True, exist_ok=True)
        pages = archive.pages(config.to_local(ctx.cfg, r["download_path"]))
        content = strip.content_indexes([name for name, _ in pages])
        paths = []
        for i in content:
            name, data = pages[i]
            path = work / f"{i:04d}{Path(name).suffix.lower()}"
            path.write_bytes(data)
            paths.append(path)
        out.append(paths)
    return out


def _aspects(ctx: workflow.Context, series, sizes):
    """The series' page aspect candidates: stored, else computed from this chapter and stored."""
    row = db.one_row(ctx.conn, "SELECT page_aspect FROM series_conversion WHERE series_id = %s", (series["id"],))
    if row["page_aspect"] is not None:
        return json.loads(row["page_aspect"])
    aspects = strip.page_aspect([sizes])
    if aspects is not None:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE series_conversion SET page_aspect = coalesce(page_aspect, %s) WHERE series_id = %s",
                (json.dumps(aspects), series["id"]),
            )
    return aspects or []


# ---------------------------------------------------------------- scan


def scan(ctx: workflow.Context, chapter, series, key):
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

    def record(conn):
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


def _scan_strip(ctx: workflow.Context, key, paths, sizes, content):
    cpaths = [paths[i] for i in content]
    rows, logos = [], []
    for i, path in zip(content, cpaths, strict=True):
        im = strip.open_page(path)
        rows.append(strip.page_rows(im))
        logo = logo_units.measure(i, im)
        if logo is not None:
            logos.append(logo)
    st = strip.make_strip(cpaths, [sizes[i] for i in content], rows)
    placed = dict(zip(content, st.pages, strict=True))
    for logo in logos:  # its tight box's rows in the strip, end exclusive
        p, (_, y0, _, y1) = placed[logo["page"]], logo["tight"]
        logo["rows"] = [p["top"] + y0 * p["th"] // p["h"], p["top"] + -(-y1 * p["th"] // p["h"])]
    decisions = store.Decisions(ctx.conn, key)
    device = ctx.cfg["junk"]["ocr_device"]
    sc = strip_units.scan(
        st,
        lambda im: ocr.text(im, device),
        lambda sig: decisions.decide("block", sig) is None,
    )
    sc["content"] = content
    sc["marks"] = logos
    return sc, [("block", b["sig"]) for b in sc["blocks"]]


def _nul_rows(sc):
    """[(start, end)] of the strip units whose OCR text holds a NUL: the edge blocks and their join pieces, the only
    units OCR reads."""
    units = [u for b in sc["blocks"] for u in (b, *b["joins"])]
    return [(u["start"], u["end"]) for u in units if "\x00" in u.get("text", "")]


def _page_units(sc):
    if sc["split"]:
        return []
    out = []
    for u in sc["edges"]:
        if u["frame"]:
            out.append(("frame", u["frame"]))
        if u["sig"]:
            out.append(("page", u["sig"]))
    out += [("band", b["sig"]) for found in sc["bands"].values() for b in found]
    return out


# ---------------------------------------------------------------- decide


class _Applied(NamedTuple):
    """What decisions make of a chapter's scan: its chapter edit, the watermark rows [(series key, key)] to remember,
    the units left undecided (the layout's, then the logos'), and why the edit is refused (strip_units'
    kept_problem; None when it is not)."""

    edit: dict
    watermarks: list[tuple[str, str]]
    undecided: list[dict]
    problem: str | None


def _apply(conn, sc, key, decide_unit):
    """The decisions on a scan: its layout's (a strip's with the series' known watermarks), then the logos' on what
    that leaves (a refused strip edit stops before the logos)."""
    edit = {"drop_pages": [], "bands": [], "join": [], "split": False, "strip_removed": [], "blank": []}
    watermarks = []
    if sc["layout"] == "strip":
        known = conn.execute("SELECT key FROM junk_watermark WHERE series_key = %s", (key,)).fetchall()
        got = strip_units.decide(sc, decide_unit, [images.sig_from_str(r["key"]) for r in known])
        if got["kept_problem"]:
            return _Applied(edit, [], [], got["kept_problem"])
        edit["strip_removed"] = got["removed"]
        watermarks = [(key, k) for k in got["watermarks"]]
        survives = logo_units.strip_survives(edit["strip_removed"])
    else:
        got = page_units.decide(sc, decide_unit)
        edit.update(got["edit"])
        survives = logo_units.page_survives(edit)
    logos = logo_units.decide(sc["marks"], survives, decide_unit)
    edit["blank"] = logos["blank"]
    return _Applied(edit, watermarks, got["undecided"] + logos["undecided"], None)


def _remember(conn, watermarks):
    """With the outcome: the series' later chapters know these bands as watermarks."""
    db.executemany(
        conn, "INSERT INTO junk_watermark (series_key, key) VALUES (%s, %s) ON CONFLICT DO NOTHING", watermarks
    )


def _finished(ctx: workflow.Context, chapter, edit, watermarks):
    """The outcome of a chapter that offers no candidate: the library copy check's (a duplicate dropped, or a better
    copy of a busy file held: no edit kept), else Done with its chapter edit."""
    copy = library_copy.after_junk(ctx, chapter, edit)
    if copy is not None:

        def closed(conn):
            _remember(conn, watermarks)
            if isinstance(copy, workflow.Dropped):  # a held copy keeps its scan for its retry
                conn.execute("DELETE FROM junk_scan WHERE chapter_id = %s", (chapter["id"],))
            if copy.record:
                copy.record(conn)

        return dataclasses.replace(copy, record=closed)

    def done(conn):
        _remember(conn, watermarks)
        values = (chapter["id"], json.dumps(edit))
        conn.execute(
            "INSERT INTO chapter_edit (chapter_id, edit) VALUES (%s, %s) ON CONFLICT"
            " (chapter_id) DO UPDATE SET edit = excluded.edit",
            values,
        )
        conn.execute("DELETE FROM junk_scan WHERE chapter_id = %s", (chapter["id"],))

    return workflow.Done(record=done)


def decide(ctx: workflow.Context, chapter, key, row):
    sc = json.loads(row["scan"])
    decisions = store.Decisions(ctx.conn, key)

    def decide_unit(kind, text):
        return decisions.decide(kind, store.parse_key(kind, text))

    applied = _apply(ctx.conn, sc, key, decide_unit)
    if applied.problem:
        return workflow.NeedsHuman(applied.problem)
    words = {w: ctx.cfg["junk"][w] for w in WORDS}
    picks = candidates.select(ctx.conn, key, chapter["identity"], applied.undecided, words)
    if not picks:
        return _finished(ctx, chapter, applied.edit, applied.watermarks)
    limit = ctx.cfg["junk"]["review_limit"]
    rows = [candidates.find(ctx.conn, decisions, p["kind"], p["key"]) for p in picks]
    held = [r for r in rows if r is not None and (r["verdict"] == "unsure" or candidates.stuck(r, limit))]
    if held:

        def hold(conn):  # the chapter keeps the candidates it waits on open, so junk decide can name them
            _remember(conn, applied.watermarks)
            ids = sorted(r["id"] for r in rows if r is not None)
            conn.execute("UPDATE junk_scan SET candidates = %s WHERE chapter_id = %s", (json.dumps(ids), chapter["id"]))

        return workflow.NeedsHuman(held_reason(held, limit), record=hold)
    new = [p for p, r in zip(picks, rows, strict=True) if r is None]
    if new:
        write_images(ctx, chapter, key, sc, new)
    at = workflow.now(ctx.conn)

    def wait(conn):
        _remember(conn, applied.watermarks)
        ids = candidates.join(conn, key, picks, at)
        conn.execute("UPDATE junk_scan SET candidates = %s WHERE chapter_id = %s", (json.dumps(ids), chapter["id"]))

    return workflow.Waiting("junk_decision", {"candidates": len(picks)}, record=wait)


def held_reason(rows, limit):
    parts = [
        f"candidate {r['id']} ({r['kind']}): "
        + ("Claude was unsure" if r["verdict"] == "unsure" else f"undecided after {limit} reviews")
        for r in rows
    ]
    return "junk " + "; ".join(parts) + " -> junk decide ID junk|keep, then retry"


def junk_decision_over(ctx: workflow.Context, chapter):
    """The junk_decision wait (SPEC "Junk decisions"): over (True) when the chapter's scan is stale (it rescans) or a
    candidate it waits on is covered by a decision or gone: the chapter is decided again and waits on what is still
    undecided then (a decision can make another candidate moot: a dropped page's band). Else NeedsHuman when one is
    unsure or stuck; else False."""
    row = ctx.conn.execute("SELECT * FROM junk_scan WHERE chapter_id = %s", (chapter["id"],)).fetchone()
    if row is None or current_scan(ctx.conn, chapter) is None:
        return True
    limit = ctx.cfg["junk"]["review_limit"]
    ids, held = json.loads(row["candidates"]), []
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


def write_images(ctx: workflow.Context, chapter, key, sc, picks):
    """Write the images of new candidates (SPEC "Junk decisions", candidates): the download's pages are extracted
    again to the scratch folder, which is removed afterwards."""
    work = _scratch(ctx, chapter)
    try:
        paths, _names, sizes, _ = extract(ctx, chapter, work)
        for p in picks:
            if p["kind"] == "logo":
                page = images.open_flat(paths[p["unit"]["page"]])[0].convert("RGB")
                unit_im, context_im = logo_units.review_images(page, p["unit"]["logo"])
            elif sc["layout"] == "strip":
                unit_im, context_im = _strip_images(sc, paths, sizes, p)
            else:
                unit_im, context_im = _page_images(paths, p)
            info = {
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


def seen_kind(kind):
    """The kind a unit is recorded as seen under (a probe is part of a seen edge block)."""
    return "block" if kind == "probe" else kind


def _strip_images(sc, paths, sizes, pick):
    content = sc["content"]
    width, pages, height = strip.strip_layout([sizes[i] for i in content])
    for p, i in zip(pages, content, strict=True):
        p["path"] = str(paths[i])
    u = pick["unit"]
    unit_im = strip.render_rows(pages, width, u["start"], u["end"])
    lo, hi = max(0, u["start"] - candidates.CONTEXT_PX), min(height, u["end"] + candidates.CONTEXT_PX)
    return unit_im, strip.render_rows(pages, width, lo, hi)


def _page_images(paths, pick):
    u = pick["unit"]
    i = u["page"]
    page = images.open_flat(paths[i])[0].convert("RGB")
    unit_im = page.crop(tuple(u["box"])) if pick["kind"] == "band" else page
    near = [images.open_flat(paths[j])[0].convert("RGB") for j in (i - 1, i, i + 1) if 0 <= j < len(paths)]
    h = 800
    near = [im.resize((max(1, round(im.width * h / im.height)), h)) for im in near]
    context = Image.new("RGB", (sum(im.width for im in near) + 8 * (len(near) - 1), h), "white")
    x = 0
    for im in near:
        context.paste(im, (x, 0))
        x += im.width + 8
    return unit_im, context
