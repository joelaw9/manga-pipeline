"""Stage junk (SPEC "Chapter stages", "State", "Bad downloads", "Junk decisions"): first the series' baseline (a series
Kavita holds waits on bulk_conversion until its record parses), then the put-aside check that needed it, then junk.

A chapter with no current scan is scanned (pages extracted to its scratch folder, measured, its units keyed, OCR on
the strip units no decision covers) and stays ready (Ready), so every ready junk chapter of a series is scanned before
any is decided. A chapter with a current scan is decided from it with no decode: done with its chapter edit, waiting
on junk_decision with its candidates (their images written first), or needs_human (a removal that would leave too
little; a candidate Claude was unsure of or past the review limit).
"""

import json
import shutil
from pathlib import Path

from PIL import Image

from manga_pipeline import archive, baseline, config, db, handled, images, page_store, strip, workflow
from manga_pipeline.junk import candidates, ocr, page_units, store, strip_units

WORDS = ("keywords", "weak", "paired", "suppress")


def run(ctx, chapter):
    series = ctx.conn.execute("SELECT * FROM series WHERE id = ?", (chapter["series_id"],)).fetchone()
    if baseline.needed(series):
        why = baseline.take(ctx, series)
        if why:
            return workflow.Waiting("bulk_conversion", {"why": why})
    why = handled.put_aside(ctx, chapter, chapter["content_digest"])
    if why:
        return workflow.Dropped(why)
    if not chapter["content_digest"]:
        raise ValueError(f"chapter {chapter['id']} has no content digest: discovery sets it before junk")
    key = store.series_key(series["folder"])
    row = current_scan(ctx.conn, chapter)
    if row is None:
        return scan(ctx, chapter, series, key)
    return decide(ctx, chapter, key, row)


def current_scan(conn, chapter):
    """The chapter's junk_scan row when it is current (same series, same content digest), else None."""
    return conn.execute(
        "SELECT * FROM junk_scan WHERE chapter_id = ? AND series_id = ? AND content_digest = ?",
        (chapter["id"], chapter["series_id"], chapter["content_digest"]),
    ).fetchone()


# ---------------------------------------------------------------- pages on local disk


def _scratch(ctx, chapter):
    return config.abs_path(ctx.cfg["paths"]["scratch"]) / str(chapter["id"]) / "junk"


def extract(ctx, chapter, work):
    """The download's pages written to `work` in archive order: (paths, names, sizes, measurements)."""
    cbz = Path(chapter["download_path"])
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


def _layout(ctx, series, content_paths):
    return strip.ensure_series_layout(ctx.conn, series["id"], [content_paths])


def _aspects(ctx, series, sizes):
    """The series' page aspect candidates: stored, else computed from this chapter and stored."""
    row = ctx.conn.execute("SELECT page_aspect FROM series_conversion WHERE series_id = ?", (series["id"],)).fetchone()
    if row["page_aspect"] is not None:
        return json.loads(row["page_aspect"])
    aspects = strip.page_aspect([sizes])
    if aspects is not None:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE series_conversion SET page_aspect = coalesce(page_aspect, ?) WHERE series_id = ?",
                (json.dumps(aspects), series["id"]),
            )
    return aspects or []


# ---------------------------------------------------------------- scan


def scan(ctx, chapter, series, key):
    work = _scratch(ctx, chapter)
    try:
        paths, names, sizes, measured = extract(ctx, chapter, work)
        content = strip._content_indexes(names)
        layout = _layout(ctx, series, [paths[i] for i in content])
        if layout == "strip":
            sc, units = _scan_strip(ctx, key, paths, sizes, content)
        else:
            uniform = [m["junk_signature"] is None for m in measured]
            sc = page_units.scan(paths, names, sizes, uniform, _aspects(ctx, series, sizes))
            units = _page_units(sc)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    at = workflow.now()
    seen = candidates.seen_rows(key, chapter["identity"], units, at)

    def record(conn):
        conn.execute(
            """INSERT OR REPLACE INTO junk_scan (chapter_id, series_id, content_digest, scan, scanned_at)
            VALUES (?, ?, ?, ?, ?)""",
            (chapter["id"], chapter["series_id"], chapter["content_digest"], json.dumps(sc), at),
        )
        candidates.write_seen(conn, seen)

    return workflow.Ready(record=record)


def _scan_strip(ctx, key, paths, sizes, content):
    cpaths = [paths[i] for i in content]
    rows = [strip.page_rows(strip._open(p)) for p in cpaths]
    st = strip.make_strip(cpaths, [sizes[i] for i in content], rows)
    decisions = store.Decisions(ctx.conn, key)
    device = ctx.cfg["junk"]["ocr_device"]
    sc = strip_units.scan(
        st,
        lambda im: ocr.text(im, device),
        lambda sig: decisions.decide("block", sig) is None,
    )
    sc["content"] = content
    return sc, [("block", b["sig"]) for b in sc["blocks"]]


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


def decide(ctx, chapter, key, row):
    sc = json.loads(row["scan"])
    decisions = store.Decisions(ctx.conn, key)

    def decide_unit(kind, text):
        return decisions.decide(kind, store.parse_key(kind, text))

    edit = {"drop_pages": [], "bands": [], "join": [], "split": False, "strip_removed": []}
    if sc["layout"] == "strip":
        got = strip_units.decide(sc, decide_unit)
        if got["kept_problem"]:
            return workflow.NeedsHuman(got["kept_problem"])
        edit["strip_removed"] = got["removed"]
    else:
        got = page_units.decide(sc, decide_unit)
        edit.update(got["edit"])
    words = {w: ctx.cfg["junk"][w] for w in WORDS}
    picks = candidates.select(ctx.conn, key, chapter["identity"], got["undecided"], words)
    if not picks:

        def done(conn):
            values = (chapter["id"], json.dumps(edit))
            conn.execute("INSERT OR REPLACE INTO chapter_edit (chapter_id, edit) VALUES (?, ?)", values)
            conn.execute("DELETE FROM junk_scan WHERE chapter_id = ?", (chapter["id"],))

        return workflow.Done(record=done)
    limit = ctx.cfg["junk"]["review_limit"]
    rows = [candidates.find(ctx.conn, key, p["kind"], p["key"]) for p in picks]
    held = [r for r in rows if r is not None and (r["verdict"] == "unsure" or candidates.stuck(r, limit))]
    if held:

        def hold(conn):  # the chapter keeps the candidates it waits on open, so junk decide can name them
            ids = sorted(r["id"] for r in rows if r is not None)
            conn.execute("UPDATE junk_scan SET candidates = ? WHERE chapter_id = ?", (json.dumps(ids), chapter["id"]))

        return workflow.NeedsHuman(held_reason(held, limit), record=hold)
    new = [p for p, r in zip(picks, rows, strict=True) if r is None]
    if new:
        write_images(ctx, chapter, key, sc, new)
    at = workflow.now()

    def wait(conn):
        ids = candidates.join(conn, key, picks, at)
        conn.execute("UPDATE junk_scan SET candidates = ? WHERE chapter_id = ?", (json.dumps(ids), chapter["id"]))

    return workflow.Waiting("junk_decision", {"candidates": len(picks)}, record=wait)


def held_reason(rows, limit):
    parts = [
        f"candidate {r['id']} ({r['kind']}): "
        + ("Claude was unsure" if r["verdict"] == "unsure" else f"undecided after {limit} reviews")
        for r in rows
    ]
    return "junk " + "; ".join(parts) + " -> junk decide ID junk|keep, then retry"


def junk_decision_over(ctx, chapter):
    """The junk_decision wait (SPEC "Junk decisions"): over (True) when the chapter's scan is stale (it rescans) or
    every candidate it waits on is covered by a decision; NeedsHuman when one is unsure or stuck; else False."""
    row = ctx.conn.execute("SELECT * FROM junk_scan WHERE chapter_id = ?", (chapter["id"],)).fetchone()
    if row is None or current_scan(ctx.conn, chapter) is None:
        return True
    limit = ctx.cfg["junk"]["review_limit"]
    waiting, held = False, []
    decisions = store.by_series(ctx.conn)
    for cid in json.loads(row["candidates"]):
        c = ctx.conn.execute("SELECT * FROM junk_candidate WHERE id = ?", (cid,)).fetchone()
        if c is None:
            continue  # cleaned up: it was closed
        decided = decisions(c["series_key"]).decide(c["kind"], store.parse_key(c["kind"], c["key"]))
        if decided is not None:
            continue
        if c["verdict"] == "unsure" or candidates.stuck(c, limit):
            held.append(c)
        else:
            waiting = True
    if held:
        return workflow.NeedsHuman(held_reason(held, limit))
    return not waiting


def bulk_conversion_over(ctx, chapter):
    """The bulk_conversion wait ends once the series has a baseline or its record parses."""
    series = ctx.conn.execute("SELECT * FROM series WHERE id = ?", (chapter["series_id"],)).fetchone()
    return not baseline.needed(series) or baseline.record_problem(series) is None


# ---------------------------------------------------------------- candidate images


def write_images(ctx, chapter, key, sc, picks):
    """Write the images of new candidates (SPEC "Junk decisions", candidates): the download's pages are extracted
    again to the scratch folder, which is removed afterwards."""
    work = _scratch(ctx, chapter)
    try:
        paths, names, sizes, _ = extract(ctx, chapter, work)
        for p in picks:
            unit_im, context_im = (
                _strip_images(sc, paths, sizes, p) if sc["layout"] == "strip" else _page_images(paths, p)
            )
            info = {
                "series": key,
                "kind": p["kind"],
                "reason": p["reason"],
                "text": p["unit"].get("text", ""),
                "chapters": candidates.chapters_showing(ctx.conn, key, seen_kind(p["kind"]), p["key"]),
            }
            name = candidates.folder_name(key, p["kind"], p["key"])
            candidates.write_images(ctx.cfg, name, unit_im, context_im, info)
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
