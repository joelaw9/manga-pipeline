"""Junk units of a page-format chapter (SPEC "Junk decisions": units, decisions, the chapter edit). Ported from the
bulk converter's optimizer/page_junk.py (credit bands, ink box, end-page size and text rules), junk.is_white, and the
page-format steps of its planner (plan_scan.page_chapter / white_edges, plan_review.page_review /
page_items_review / uncovered_bands), reshaped to 1 chapter:

- `scan(...)` decodes once: per edge page its whole-page signature, frame, white flag and (last pages) text
  features; per tall page its credit bands; the edge rows of the pages slice repair may join; per content page its
  logo (logo_units.measure); or, for a stitched chapter, only that it is split.
- `decide(sc, decide_unit)` applies decisions with no decode and returns the page-layout edit and the units left
  undecided, each with the per-chapter reasons it is a candidate (size, text, band).

Not ported: the cross-series frame index (frame recurrence is counted like any unit's, SPEC "Junk decisions").
Constants are the bulk converter's, frozen: the stored decisions were made with them.
"""

import base64
from collections import Counter
from collections.abc import Mapping
from types import MappingProxyType

import numpy as np
from PIL import Image

from manga_pipeline import images, strip
from manga_pipeline.junk import frames, logo_units, store

BAND = MappingProxyType(
    {
        **strip.SLICE_REPAIR,
        **strip.RESTITCH,
        "band_min_px": 16,  # a band (and what it leaves of the page) is at least this tall
        "band_seam_min_px": 100,  # a band cut on a seam (no gutter) is at least this tall
        "band_search": 0.12,  # the cut is looked for within this share of the page height around the naive cut
        "page_review_width_tolerance": 0.05,  # size rule: a width this far off the modal width is another size
        "page_review_aspect_margin": 0.01,  # size rule: the middle pages' aspect range widened by this
    }
)
END_PAGES = MappingProxyType(
    {
        "same_size": 0.05,
        "text_plain": 0.60,
        "text_mid": 0.11,
        "text_chroma": 0.005,
        "gray_chroma": 0.003,
        "white_min": 245,  # a white page: uniform, and its darkest channel minimum at least this
        "spread_tolerance": 0.10,
    }
)
SPREAD_WIDTH = (1.8, 2.2)  # size rule: a page this many modal widths wide is a spread ...
WIDE_SHORT = (1.3, 0.85)  # ... as is one wider than 1.3x modal and shorter than 0.85x the middle pages' lowest aspect
INK_DELTA = 40  # band signature: a pixel this far (luma) from the band's median border tone is ink
TEXT_WIDTH = 400  # text features: the page is measured at this width (box filter) ...
TEXT_BORDER = 4  # ... its background is the median luma of this many px around its edge
TEXT_PLAIN = 12  # ... plain: luma at most this far from the background
TEXT_MID = 20  # ... mid-tone: luma more than this far from it, within TEXT_MID_LUMA, not chroma
TEXT_MID_LUMA = (40, 215)
TEXT_CHROMA = 40  # ... chroma: channel spread (max - min) above this


# ---------------------------------------------------------------- page measurements


def is_white(im, white_min=END_PAGES["white_min"]):
    """A white page: uniform, and every channel's minimum at least white_min."""
    lo_hi = im.convert("RGB").getextrema()
    return images.is_uniform(im) and min(lo for lo, _ in lo_hi) >= white_min


def ink_box(im):
    """Box of the band's ink (pixels more than INK_DELTA luma from the median of its border pixels), or None."""
    g = np.asarray(im.convert("L"), np.int16)
    border = np.concatenate([g[0], g[-1], g[:, 0], g[:, -1]])
    ink = np.abs(g - np.median(border)) > INK_DELTA
    ys, xs = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    if not len(ys):
        return None
    return int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1


def _sharpest_row(g, y, step):
    """Full-resolution row within +-1.5 analysis rows of y where the image changes most."""
    y0, y1 = max(1, int(y - 1.5 * step)), min(g.height - 1, int(y + 1.5 * step) + 1)
    rows = np.asarray(g.crop((0, y0 - 1, g.width, y1)), np.float32)
    return y0 + int(np.abs(np.diff(rows, axis=0)).mean(axis=1).argmax())


def find_band(im, aspect, where, cfg: Mapping[str, float] = BAND):
    """A credit band stitched onto the top or bottom of a page taller than the series shape: cut at the gutter nearest
    the naive cut within band_search of the page height, else at the strongest seam (seam_score >= max_seam_score)
    nearest it; none -> None (never cut through art). The band is at least band_min_px and not uniform; one under
    band_seam_min_px counts only when cut on a gutter. Its signature is of its ink box, without the gutter rows.
    Returns {"where", "cut", "height", "how": "gap" | "seam", "sig" (text), "box"} or None (source px)."""
    w, h = im.size
    page_h = w * aspect
    if h <= page_h * (1 + cfg["band_tolerance"]):
        return None
    aw = cfg["analysis_width"]
    n = max(2 * strip.SEAM_ROWS + 2, round(h * aw / w))
    step = h / n
    g = im.convert("L")
    a = np.asarray(g.resize((aw, n)), np.float32)
    uniform = strip.blank_rows(a, cfg)
    naive = h - page_h if where == "top" else page_h
    lo, hi = naive - cfg["band_search"] * page_h, naive + cfg["band_search"] * page_h
    cut, how, run, y = None, None, None, 0
    while y < n:  # gutters overlapping the window: their point nearest the naive cut
        if not uniform[y]:
            y += 1
            continue
        e = y
        while e < n and uniform[e]:
            e += 1
        ys, ye = y * step, e * step
        if ye >= lo and ys <= hi and ys > 0 and ye < h:  # a page's own top/bottom margin is no seam
            c = min(max(naive, ys), ye)
            if cut is None or abs(c - naive) < abs(cut - naive):
                cut, how, run = c, "gap", (round(ys), round(ye))
        y = e
    if how == "gap" and run is not None and cut != naive:  # on the gutter's edge (run is set with "gap")
        at_end = cut < naive
        cut = _sharpest_row(g, cut, step)
        run = (run[0], cut) if at_end else (cut, run[1])
    if cut is None:  # no gutter: the discontinuity nearest the naive cut
        best = None
        for k in range(max(strip.SEAM_ROWS, int(lo / step)), min(n - strip.SEAM_ROWS, int(hi / step) + 1) + 1):
            score = strip.seam_score(a[k - strip.SEAM_ROWS : k], a[k : k + strip.SEAM_ROWS])
            if score >= cfg["max_seam_score"] and (best is None or (abs(k * step - naive), -score) < best[0]):
                best = ((abs(k * step - naive), -score), k)
        if best is None:
            return None
        cut, how = _sharpest_row(g, best[1] * step, step), "seam"
    cut = round(cut)
    box = (0, 0, w, cut) if where == "top" else (0, cut, w, h)
    height = box[3] - box[1]
    if height < cfg["band_min_px"] or h - height < cfg["band_min_px"]:
        return None
    if how == "seam" and height < cfg["band_seam_min_px"]:
        return None
    content = im.crop(box if run is None else (0, 0, w, run[0]) if where == "top" else (0, run[1], w, h))
    if content.height < cfg["band_min_px"] or images.is_uniform(content):
        return None
    ink = ink_box(content)
    if ink is None:
        return None
    sig = images.sig_to_str(images.signature(content.crop(ink)))
    return {"where": where, "cut": cut, "height": height, "how": how, "sig": sig, "box": list(box)}


def page_bands(im, aspect, cfg=BAND):
    """find_band at both edges of a page."""
    return [b for b in (find_band(im, aspect, wh, cfg) for wh in ("top", "bottom")) if b]


def text_features(im):
    """A page's share of plain, mid-tone and coloured pixels at TEXT_WIDTH: {"plain", "mid", "chroma"}."""
    im = im.convert("RGB")
    im = im.resize((TEXT_WIDTH, max(1, round(im.height * TEXT_WIDTH / im.width))), Image.Resampling.BOX)
    a = np.asarray(im, np.int16)
    lum = a.mean(axis=2)
    b = TEXT_BORDER
    bg = np.median(np.concatenate([lum[:b].ravel(), lum[-b:].ravel(), lum[:, :b].ravel(), lum[:, -b:].ravel()]))
    d = np.abs(lum - bg)
    chroma = (a.max(axis=2) - a.min(axis=2)) > TEXT_CHROMA
    mid = (d > TEXT_MID) & (lum >= TEXT_MID_LUMA[0]) & (lum <= TEXT_MID_LUMA[1]) & ~chroma
    return {"plain": float((d <= TEXT_PLAIN).mean()), "mid": float(mid.mean()), "chroma": float(chroma.mean())}


def _pack_edges(edges):
    top, bottom = edges
    return [base64.b64encode(np.asarray(x, np.uint8).tobytes()).decode() for x in (top, bottom)]


def _unpack_edges(packed):
    return tuple(
        np.frombuffer(base64.b64decode(x), np.uint8).reshape(strip.SEAM_ROWS, -1).astype(np.float32) for x in packed
    )


# ---------------------------------------------------------------- size and text rules (no decode)


def page_candidates(sizes, aspects, banded, joined, cfg=BAND, spread_tolerance=END_PAGES["spread_tolerance"]):
    """Edge pages (of the content pages, `sizes` in order) whose size no middle page of the chapter has: another width
    ("width") or an aspect outside the middle pages' range ("tall" / "short"). Never: band pages, joined pages,
    spreads. Returns [(content index, why)]."""
    if not sizes:
        return []
    shape = strip.series_shape(aspects)
    width = Counter(w for w, _ in sizes).most_common(1)[0][0]
    wtol, margin = cfg["page_review_width_tolerance"], cfg["page_review_aspect_margin"]
    edge = strip.edge_indexes(len(sizes))
    mid = [
        i
        for i in range(len(sizes))
        if i not in edge and i not in banded and i not in joined and abs(sizes[i][0] / width - 1) <= wtol
    ]
    mid_a = [sizes[i][1] / sizes[i][0] for i in mid] or [shape]
    lo, hi = min(mid_a) * (1 - margin), max(mid_a) * (1 + margin)
    out = []
    for i in edge:
        if i in banded or i in joined:
            continue
        w, h = sizes[i]
        a, wr = h / w, w / width
        spread = SPREAD_WIDTH[0] <= wr <= SPREAD_WIDTH[1] or (wr > WIDE_SHORT[0] and a < WIDE_SHORT[1] * lo)
        if spread and min(mid_a) * (1 - spread_tolerance) <= 2 * a <= max(mid_a) * (1 + spread_tolerance):
            continue
        if abs(wr - 1) > wtol:
            out.append((i, "width"))
        elif not lo <= a <= hi:
            out.append((i, "tall" if a > hi else "short"))
    return out


def end_text_page(sizes, texts, cfg=END_PAGES):
    """Content index of the last page when it is a credit page of the modal size, mostly plain, little mid-tone, some
    colour, in a gray chapter (every other tail page under gray_chroma); else None. texts: {content index: features}."""
    n = len(sizes)
    if n < 2:
        return None
    last = n - 1
    (mw, mh), (w, h) = Counter(sizes).most_common(1)[0][0], sizes[last]
    f, others = texts[last], [texts[i] for i in range(max(0, n - strip.END_TAIL), last)]
    same = abs(w / mw - 1) <= cfg["same_size"] and abs((h / w) / (mh / mw) - 1) <= cfg["same_size"]
    if (
        same
        and f["plain"] >= cfg["text_plain"]
        and f["mid"] <= cfg["text_mid"]
        and f["chroma"] >= cfg["text_chroma"]
        and all(o["chroma"] < cfg["gray_chroma"] for o in others)
    ):
        return last
    return None


# ---------------------------------------------------------------- the scan, which decodes


def _box_key(index, box):
    return f"{index}" if box is None else f"{index}:{','.join(map(str, box))}"


def scan(paths, names, sizes, uniform, aspects):
    """The scan of a page-format chapter. paths, names, sizes: every page of the download in archive order;
    uniform: per page, True when it has no junk signature (images.measure_pages); aspects: the series' page aspect
    candidates (strip.page_aspect). Returns a JSON-able dict; page indexes are download indexes."""
    content = strip._content_indexes(names)
    csizes = [sizes[i] for i in content]
    if strip.is_stitched(csizes, aspects):
        return {"layout": "pages", "split": True, "marks": []}
    shape = strip.series_shape(aspects)
    opened = {}

    def page(i):
        if i not in opened:
            opened.clear()
            opened[i] = images.open_flat(paths[i])[0].convert("RGB")
        return opened[i]

    edge_k = strip.edge_indexes(len(content))
    tail_k = range(max(0, len(content) - strip.END_TAIL), len(content))
    edges = []
    for k in edge_k:
        i = content[k]
        im = page(i)
        f = frames.frame(im)
        unit = {
            "page": i,
            "sig": None if uniform[i] else images.sig_to_str(images.signature(im)),
            "frame": frames.key_text(f) if not uniform[i] and frames.has_content(f) else None,
        }
        if k in tail_k:
            unit["text"] = text_features(im)
        edges.append(unit)
    white = [i for i in content if uniform[i] and is_white(page(i))]
    bands = {}
    for k in strip.band_pages([names[i] for i in content], csizes, shape):
        i = content[k]
        found = page_bands(page(i), shape)
        if found:
            bands[str(i)] = found
    # edge rows of every content page, and of every band page as each of its crops leaves it: slice repair runs on
    # the pages decisions keep, cropped where cropped
    rows, logos = {}, []
    for i in content:
        rows[_box_key(i, None)] = _pack_edges(strip.edge_rows(page(i)))
        logo = logo_units.measure(i, page(i))
        if logo is not None:
            logos.append(logo)
        for box in _crop_boxes(bands.get(str(i), []), sizes[i]):
            rows[_box_key(i, box)] = _pack_edges(strip.edge_rows(page(i).crop(tuple(box))))
    return {
        "layout": "pages",
        "split": False,
        "content": content,
        "sizes": [list(s) for s in sizes],
        "edges": edges,
        "white": white,
        "bands": bands,
        "rows": rows,
        "aspects": list(aspects or []),
        "marks": logos,
    }


def _crop(found, size):
    """The kept box of a page once bands `found` are cut off, or None when it would keep under band_min_px."""
    w, h = size
    top = max((b["cut"] for b in found if b["where"] == "top"), default=0)
    bottom = min((b["cut"] for b in found if b["where"] == "bottom"), default=h)
    if bottom - top < BAND["band_min_px"]:
        return None
    return [0, top, w, bottom]


def _crop_boxes(found, size):
    """Every box a page can be cropped to by a non-empty subset of its bands."""
    out = [_crop([b], size) for b in found]
    if len(found) == 2:
        out.append(_crop(found, size))
    return [b for b in out if b is not None]


# ---------------------------------------------------------------- decide (no decode)


def page_decision(unit, decide_unit):
    """The row deciding an edge page from its whole-page signature (page decisions) and its frame (frame decisions):
    the first in lookup order (store.in_lookup_order), the page's on a tie; None when neither decides."""
    page = decide_unit("page", unit["sig"]) if unit["sig"] else None
    frame = decide_unit("frame", unit["frame"]) if unit["frame"] else None
    hits = [r for r in (page, frame) if r is not None]
    return store.in_lookup_order(hits)[0] if hits else None


def decide(sc, decide_unit):
    """Apply decisions to a page-format scan. decide_unit(kind, key text) -> the deciding row or None. Returns
    {"edit": the page-layout keys of the chapter edit, "undecided": [{"kind", "key", "page", "where"?, "reasons"}]}."""
    if sc["split"]:
        return {"edit": {"split": True, "drop_pages": [], "bands": [], "join": []}, "undecided": []}
    content, sizes = sc["content"], [tuple(s) for s in sc["sizes"]]
    drops, framed, undecided_pages = set(), set(), {}
    for u in sc["edges"]:
        row = page_decision(u, decide_unit)
        if u["frame"] and decide_unit("frame", u["frame"]) is not None:
            framed.add(u["page"])
        if row is None:
            if u["sig"] is not None:
                undecided_pages[u["page"]] = u
        elif row["verdict"] == "junk":
            drops.add(u["page"])
    rest = [i for i in content if i not in drops]
    white = set(sc["white"])
    lead = next((n for n, i in enumerate(rest) if i not in white), len(rest))
    if lead < len(rest):
        trail = next(n for n in range(len(rest) - 1, -1, -1) if rest[n] not in white)
        drops |= set(rest[:lead]) | set(rest[trail + 1 :])
    crops, band_units, banded, too_much = {}, [], set(), set()
    for key, listed in sc["bands"].items():
        i = int(key)
        if i in drops or i in framed:
            continue
        # An unsignable band is no band (no decision could be written on it): the page is judged as a page.
        found = [b for b in listed if not store.unsignable("band", b["sig"])]
        if not found:
            continue
        banded.add(i)
        hits = []
        for b in found:
            row = decide_unit("band", b["sig"])
            if row is None:
                band_units.append(
                    {
                        "kind": "band",
                        "key": b["sig"],
                        "page": i,
                        "where": b["where"],
                        "box": b["box"],
                        "reasons": ["band"],  # D5.16: an undecided band is a candidate at 1 chapter
                    }
                )
            elif row["verdict"] == "junk":
                hits.append(b)
        box = _crop(hits, sizes[i]) if hits else None
        if box is not None:
            crops[i] = box
        elif hits:
            too_much.add(i)  # the bands would leave almost nothing: the page stays whole and is a candidate
    keep = [i for i in content if i not in drops]
    ksizes = [(crops[i][2] - crops[i][0], crops[i][3] - crops[i][1]) if i in crops else sizes[i] for i in keep]
    edges = {
        j: _unpack_edges(sc["rows"][_box_key(keep[j], crops.get(keep[j]))])
        for k in strip.slice_pairs(ksizes)
        for j in (k - 1, k)
    }
    joins = [[keep[j["page"]], keep[j["sliver"]]] for j in strip.find_slices(ksizes, edges) if j["join"]]
    joined = {i for pair in joins for i in pair}
    # per-chapter candidate reasons of the edge pages (SPEC "Junk decisions", candidates)
    position = {i: k for k, i in enumerate(content)}
    csizes = [sizes[i] for i in content]
    reasons = {
        content[k]: [why]
        for k, why in page_candidates(
            csizes, sc["aspects"], {position[i] for i in banded}, {position[i] for i in joined if i in position}
        )
    }
    texts = {position[u["page"]]: u["text"] for u in sc["edges"] if "text" in u}
    if len(texts) == min(strip.END_TAIL, len(content)) and content:
        last = end_text_page(csizes, texts)
        if last is not None and content[last] not in banded | joined and content[last] not in reasons:
            reasons[content[last]] = ["text"]
    edge_pages = {u["page"] for u in sc["edges"]}
    for i in sorted({b["page"] for b in band_units} | too_much):
        if i in edge_pages:
            reasons.setdefault(i, []).append("band")
    undecided = []
    for i, u in sorted(undecided_pages.items()):
        if i in drops:
            continue
        kind, key = ("frame", u["frame"]) if u["frame"] else ("page", u["sig"])
        undecided.append({"kind": kind, "key": key, "page": i, "reasons": reasons.get(i, [])})
    undecided += band_units
    return {
        "edit": {
            "split": False,
            "drop_pages": sorted(drops),
            "bands": [{"page": i, "box": crops[i]} for i in sorted(crops)],
            "join": joins,
        },
        "undecided": undecided,
    }
