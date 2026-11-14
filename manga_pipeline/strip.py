"""Strip model, gaps, cuts, layout, slices (contracts section 1/5): restitch long-strip chapters and repair sliced
pages in page-format chapters. Copied from the bulk converter's optimizer/restitch.py; reads no config: its settings
are the frozen RESTITCH and SLICE_REPAIR values (the bulk converter's config.yaml.example), and every function that
took a config section takes it as an optional argument defaulting to them.

Everything is planned with the whole chapter known first (all page sizes, every gap, every junk
block); only then are output pages rendered, one at a time, from the source rows they need.

Planning is decode-free here: page sizes come from the plan's inventory, and the per-page measurements (page_rows: a
long-strip page's uniform-row profile; edge_rows: a slice candidate's edge rows) come from the plan's one decode per
page. Execution renders the planned output pages (render).

Layout (classify_layout): `layout:` in the series overrides, else page geometry alone; Kavita's genres only flag a
disagreement.
"""

import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image

from manga_pipeline import archive, db, images

# The bulk converter's config.yaml.example `restitch:` and `slice_repair:` values (frozen: plans and tests depend on
# them). A caller passes its own mapping to override them.
RESTITCH = MappingProxyType(
    {
        "min_page": 1500,  # px at target width; limits trigger re-cutting
        "max_page": 5000,  # no gap in range -> cut through art at exactly this height
        "analysis_width": 400,  # row-profile width for gap detection
        "gap_min_rows": 12,  # near-uniform run (at 1000 px width) that counts as a panel gap
        "gap_row_std": 3,
        "gap_row_range": 20,
    }
)
SLICE_REPAIR = MappingProxyType(
    {
        "max_sliver_ratio": 0.50,  # short page <= this fraction of the page before it (Female Blacksmith: 1250+457)
        "max_seam_score": 3.5,  # seam discontinuity / in-page row change; slices 0.9-3.0, real boundaries 5.9+
        "min_edge_texture": 8,  # row std on both sides of the cut: blank margins don't count as continuous art
        "pattern_min_joins": 3,  # same page+sliver heights confirmed this often -> join other pairs of those heights
        "stitched_ratio": 1.8,  # chapter page shape >= 1.8x the series' page shape -> stitched pages, split them
        "fallback_page_aspect": 1.42,  # page shape when a series has no unstitched pages to learn from
        "snap_window": 0.06,  # stitched image within +-6% of a whole number of pages; else flagged irregular
        "band_tolerance": 0.01,  # credit bands: page taller than the series shape by more than this is band-scanned
    }
)
CONFIG = MappingProxyType({"restitch": RESTITCH, "slice_repair": SLICE_REPAIR})  # geometry_share's sections

# ComicInfo/Kavita genres that mark a long-strip series, matched case-insensitively
LONG_STRIP_GENRES = frozenset(g.lower() for g in ("Long Strip", "Web Comic", "Webtoon", "Webtoons", "Longstrip"))
ORIGIN_LAYOUT = {"manga": "pages", "manhwa": "strip", "manhua": "strip"}  # Kavita origin genre -> claimed layout
STRIP_SHARE = 0.75  # continuous page joins at or above this share: long strip (page manga <= 0.71 measured,
# webtoons >= 0.83; The Moon That Rises In The Day, 0.74, has an override)
SAMPLE_CHAPTERS = 3  # spread-out chapters sampled for the geometry check ...
SAMPLE_PAGES = 25  # ... first pages of each
MIN_EDGE_BLOCK_PX = 40  # content blocks shorter than this at a chapter's ends are never junk candidates
SPAN_BREAK_PX = 2  # a walkable run at least this tall (strip px) separates two spans (spans)
STEM_WIDTH = 3  # rendered page names: 001.png, 002.png, ...


# ---------------------------------------------------------------- shared measurements


def _open(path, mode="RGB"):
    """Decode fully (8-bit, sRGB, see images.open_image) and close the file; pages are read many times. A page with
    transparency is composited onto white (images.open_flat, as the plan's page scan reads it), the canvas rendered
    pages are built on (a plain convert would show the colour stored under transparent pixels, often black)."""
    return images.open_flat(path)[0].convert(mode)


def edge_rows(im, width=400, rows=6):
    """(top rows, bottom rows) of a page at `width`, luma: what seam_score compares across a join."""
    g = im.convert("L")
    g = g.resize((width, max(rows + 2, round(g.height * width / g.width))))
    a = np.asarray(g, np.float32)
    return a[:rows], a[-rows:]


def seam_score(bottom_rows, top_rows):
    """Discontinuity across a join vs ordinary row-to-row change beside it (1 = as smooth as in-page)."""
    inside = np.mean(
        [np.abs(bottom_rows[i + 1] - bottom_rows[i]).mean() for i in range(len(bottom_rows) - 1)]
        + [np.abs(top_rows[i + 1] - top_rows[i]).mean() for i in range(len(top_rows) - 1)]
    )
    return float(np.abs(top_rows[0] - bottom_rows[-1]).mean() / max(inside, 1.0))


def geometry_share(chapter_pages, cfg=CONFIG):
    """Median share of continuous page joins (webtoon slices) over the sampled chapters' first SAMPLE_PAGES pages:
    both edge rows blank (restitch.gap_row_std) or a seam as smooth as a slice (slice_repair.max_seam_score).
    cfg: {"restitch", "slice_repair"} sections."""
    gap_std, max_seam = cfg["restitch"]["gap_row_std"], cfg["slice_repair"]["max_seam_score"]
    shares = []
    for pages in chapter_pages:
        edges = [edge_rows(_open(p, "L")) for p in pages[:SAMPLE_PAGES]]
        joins = [
            (bot[-1].std() < gap_std and top[0].std() < gap_std) or seam_score(bot, top) < max_seam
            for (_, bot), (top, _) in zip(edges, edges[1:], strict=False)
        ]
        if joins:
            shares.append(sum(joins) / len(joins))
    return statistics.median(shares) if shares else 0


def override_layout(override):
    """`layout:` from a series overrides mapping: "strip", "pages" or None."""
    v = override.get("layout")
    v = str(v).strip().lower() if v is not None else None
    if v not in (None, "strip", "pages"):
        raise ValueError(f"overrides layout must be 'strip' or 'pages', not {v!r}")
    return v


def _claims(kavita_genres):
    """[(source, claim, value)] of Kavita's layout claims (metadata check): a long-strip genre claims strip; exactly
    one origin genre claims pages (Manga) or strip (Manhwa / Manhua)."""
    titles = [g.strip() for g in kavita_genres or ()]
    out = []
    strip = [g for g in titles if g.lower() in LONG_STRIP_GENRES]
    if strip:
        out.append(("Kavita genre", "strip", ", ".join(strip)))
    origin = [g for g in titles if g.lower() in ORIGIN_LAYOUT]
    if len(origin) == 1:
        out.append(("Kavita origin", ORIGIN_LAYOUT[origin[0].lower()], origin[0]))
    return out


def classify_layout(chapter_pages, override=None, kavita_genres=None, cfg=CONFIG):
    """Series layout: (layout, why, flags). An override `layout:` decides; else the geometry check alone: continuous
    share >= STRIP_SHARE -> "strip", else "pages". flags: one `layout-metadata` flag per Kavita claim (kavita_genres:
    Kavita's genre titles) that disagrees with the geometry's verdict, the override included. chapter_pages: the
    sampled chapters' page paths (required). cfg: {"restitch", "slice_repair"} sections."""
    o = override_layout(override or {})
    share = geometry_share(chapter_pages, cfg)
    verdict = "strip" if share >= STRIP_SHARE else "pages"
    flags = [
        f"layout-metadata: {source} says {claim} ({value}) but continuous joins {share:.0%}: check the Kavita "
        "series match and the scraper's source"
        for source, claim, value in _claims(kavita_genres)
        if claim != verdict
    ]
    if o:
        return o, f"override layout: {o} (continuous joins {share:.0%})", flags
    return verdict, f"continuous joins {share:.0%}", flags


# ---------------------------------------------------------------- long strips


def strip_layout(sizes, width=None):
    """(width, [{"w", "h", "top", "th"}], height) of pages laid one below the other at the target width (the chapter's
    most common page width unless given): th = page height at that width, top = its offset. Sizes only, no decode."""
    width = width or Counter(w for w, _ in sizes).most_common(1)[0][0]
    pages, top = [], 0
    for w, h in sizes:
        th = round(h * width / w)
        pages.append({"w": w, "h": h, "top": top, "th": th})
        top += th
    return width, pages, top


def blank_rows(a, cfg=RESTITCH):
    """The one gap-row classifier: per row of a grayscale array at analysis width, True for a blank row (std <
    gap_row_std and range < gap_row_range, any background colour). page_rows."""
    return (a.std(axis=1) < cfg["gap_row_std"]) & (np.ptp(a, axis=1) < cfg["gap_row_range"])


def page_rows(im, cfg=RESTITCH):
    """A page's blank-row profile for gap detection: per analysis row (page scaled to cfg analysis_width), True where
    the row is blank (blank_rows). Computed once per page by the plan scan."""
    w, h = im.size
    n = max(1, round(h * cfg["analysis_width"] / w))
    return blank_rows(np.asarray(im.convert("L").resize((cfg["analysis_width"], n)), np.float32), cfg)


@dataclass
class Strip:
    pages: list  # [{"path", "w", "h", "top", "th"}] th = height at target width, top = offset
    width: int  # target width (chapter's most common page width)
    height: int  # total strip height at target width
    uniform: np.ndarray  # per analysis row: near-uniform (gap) row
    row_px: float  # average target px per analysis row (only for sizing thresholds)
    row_y: np.ndarray  # target-px start of each analysis row, plus the strip end (per-page exact)


def make_strip(paths, sizes, rows):
    """A chapter's Strip from its page sizes and each page's page_rows profile (no decode)."""
    width, pages, height = strip_layout(sizes)
    starts = []
    for p, path, r in zip(pages, paths, rows, strict=True):
        p["path"] = str(path)
        # map rows within their own page: per-page rounding would drift if mapped with one global ratio
        starts.append(p["top"] + np.arange(len(r)) * p["th"] / len(r))
    uniform = np.concatenate(rows) if rows else np.zeros(0, bool)
    row_y = np.append(np.concatenate(starts) if starts else np.zeros(0), height)
    return Strip(pages, width, height, uniform, height / max(len(uniform), 1), row_y)


def _blank_runs(strip):
    """[(first, end)] analysis-row indexes of every maximal run of blank rows (Strip.uniform)."""
    runs, run = [], 0
    for i, u in enumerate(np.append(strip.uniform, False)):
        if u:
            run += 1
            continue
        if run:
            runs.append((i - run, i))
        run = 0
    return runs


def _px(strip, i, j):
    return int(round(strip.row_y[i])), int(round(strip.row_y[j]))


def walkable_runs(strip):
    """[(start, end)] in target px of every maximal run of blank rows, any length."""
    return [_px(strip, i, j) for i, j in _blank_runs(strip)]


def find_gaps(strip, cfg=RESTITCH):
    """[(start, end)] in target px of the walkable runs at least gap_min_rows (at 1000 px width) tall."""
    need = max(1, round(cfg["gap_min_rows"] * strip.width / 1000 / strip.row_px))
    return [_px(strip, i, j) for i, j in _blank_runs(strip) if j - i >= need]


def spans(strip):
    """[(start, end)] in target px of the strip's spans: maximal runs of rows between walkable runs of at least
    SPAN_BREAK_PX, starting and ending on a non-blank row. Lines glued with no blank row (or a walkable run shorter
    than SPAN_BREAK_PX) between them are one span (I Stole the Female Lead's First Love c0006: the D&C logo, (c) line
    and bunny watermark). An all-blank strip has none."""
    out, pos = [], 0
    for s, e in walkable_runs(strip) + [(strip.height, strip.height)]:
        if s > pos:
            if out and pos - out[-1][1] < SPAN_BREAK_PX:
                out[-1] = (out[-1][0], s)
            else:
                out.append((pos, s))
        pos = e
    return out


def edge_region(start, end, height, edge_px):
    """'head' for a unit wholly in the first edge_px rows of a strip of `height`, 'tail' wholly in the last, None
    otherwise; one in both (a strip shorter than 2 x edge_px) takes the half its middle lies in, a tie 'head'."""
    head, tail = end <= edge_px, start >= height - edge_px
    if head and tail:
        return "head" if start + end <= height else "tail"
    return "head" if head else "tail" if tail else None


def content_blocks(strip, gaps):
    """Content between gaps, [(start, end)] in target px."""
    blocks, pos = [], 0
    for s, e in gaps:
        if s > pos:
            blocks.append((pos, s))
        pos = e
    if pos < strip.height:
        blocks.append((pos, strip.height))
    return blocks


def render_rows(pages, width, start, end, cache=None):
    """Image of strip rows [start, end) at target width, built from only the source pages it covers.
    pages: [{"path", "top", "th"}]."""
    cache = {} if cache is None else cache
    out = Image.new("RGB", (width, end - start), "white")
    for p in pages:
        lo, hi = max(start, p["top"]), min(end, p["top"] + p["th"])
        if lo >= hi:
            continue
        if p["path"] not in cache:
            if len(cache) >= 3:
                cache.pop(next(iter(cache)))
            im = _open(p["path"])
            cache[p["path"]] = im if im.width == width else im.resize((width, p["th"]), Image.LANCZOS)
        out.paste(cache[p["path"]].crop((0, lo - p["top"], width, hi - p["top"])), (0, lo - start))
    return out


def plan_cuts(length, gaps, cfg=RESTITCH):
    """Choose page boundaries over a strip of `length` px with gap candidates [(center, strength)].
    Every page within [min_page, max_page]; prefer lengths near the middle and stronger gaps; where no
    gap fits, cut through the art at exactly max_page — unless that would leave a remainder under min_page,
    in which case that gapless run is split evenly. Returns (cuts, forced) — cuts include 0 and length."""
    lo, hi = cfg["min_page"], cfg["max_page"]
    if length <= hi:
        return [0, length], []
    mid, span = (lo + hi) / 2, hi - lo
    strength = {c: s for c, s in gaps}
    pts = sorted({0, length} | {c for c, _ in gaps if 0 < c < length})
    forced = set()

    def usable(q):  # a point closer than min_page to the end is a dead end: the page after it would be short
        return q == length or length - q >= lo

    changed = True
    while changed:  # make sure every point can reach a usable next point within the limits
        changed = False
        for p in list(pts):
            if p == length or length - p <= hi:
                continue
            if any(p + lo <= q <= p + hi and usable(q) for q in pts):
                continue
            nxt = min(q for q in pts if q > p + hi and usable(q))  # end of the gapless run starting at p
            run = nxt - p
            n = -(-run // hi)  # pieces needed at most max_page each
            if run - (n - 1) * hi < lo:  # max cuts would strand a sub-minimum remainder
                new = [p + round(i * run / n) for i in range(1, n)]
            else:
                new = [p + i * hi for i in range(1, n)]
            pts += new
            forced.update(new)
            changed = True
        pts = sorted(set(pts))
    max_s = max(strength.values(), default=1)
    best = {0: (0.0, None)}
    for q in pts[1:]:
        for p in pts:
            if p >= q or p not in best:
                continue
            seg = q - p
            if not lo <= seg <= hi:
                continue
            cost = best[p][0] + ((seg - mid) / span) ** 2
            cost += 5.0 if q in forced else 0.0
            cost -= 0.3 * strength.get(q, 0) / max_s
            if q not in best or cost < best[q][0]:
                best[q] = (cost, p)
    cuts, q = [], length
    while q is not None:
        cuts.append(q)
        q = best[q][1]
    cuts = cuts[::-1]
    return cuts, sorted(c for c in cuts if c in forced)


def cut_strip(height, gaps, removed, cfg=RESTITCH):
    """{"kept", "cuts", "forced", "length"} of a strip of `height` px with the removed ranges ([{"start", "end"}])
    taken out and the rest cut into pages on its gaps (plan_cuts). Also run again once a tail pass drops more
    pieces."""
    kept, pos = [], 0
    for r in sorted(removed, key=lambda r: r["start"]):
        if r["start"] > pos:
            kept.append((pos, r["start"]))
        pos = max(pos, r["end"])  # overlapping / nested ranges: never step back
    if pos < height:
        kept.append((pos, height))
    to_kept = _kept_mapper(kept)
    gap_pts = [(to_kept((s + e) // 2), e - s) for s, e in gaps]
    gap_pts = [(c, w) for c, w in gap_pts if c is not None]
    length = sum(e - s for s, e in kept)
    cuts, forced = plan_cuts(length, gap_pts, cfg)
    return {"kept": kept, "cuts": cuts, "forced": forced, "length": length}


def hard_joins(im, max_seam):
    """Rows of an image's hard joins, in order: seam_score (SEAM_ROWS rows each side) at least max_seam and the highest
    within SEAM_ROWS rows (one row per seam), at least MIN_EDGE_BLOCK_PX from either end. Scanned at the image's own
    rows, not an analysis width: that resize smears a 1-row seam over 2 analysis rows (Bring The Love ch87: the banner
    join scores 3.04 there, 10.26 at strip width)."""
    a = np.asarray(im.convert("L"), np.float32)
    lo, hi = MIN_EDGE_BLOCK_PX, len(a) - MIN_EDGE_BLOCK_PX
    if hi < lo:
        return []
    # seam_score for every row at once (it was 72% of a strip chapter's analysis row by row): d[i] = row i -> i+1
    d = np.abs(np.diff(a, axis=0)).mean(axis=1)
    k = np.arange(lo, hi + 1)
    win = sliding_window_view(d, SEAM_ROWS - 1)
    inside = (win[k - SEAM_ROWS].sum(axis=1) + win[k].sum(axis=1)) / (2 * (SEAM_ROWS - 1))
    s = d[k - 1] / np.maximum(inside, 1.0)
    peak = sliding_window_view(np.pad(s, SEAM_ROWS), 2 * SEAM_ROWS + 1).max(axis=1)
    return [int(j) for j in k[(s >= max_seam) & (s >= peak)]]


def edge_pieces(im, where, max_seam):
    """[(row, piece)] of an edge block's chapter-edge pieces, one per hard join (hard_joins) walked from the chapter
    edge inward: rows [0, row) of a head block, [row, end) of a tail block."""
    joins = hard_joins(im, max_seam)
    return [
        (k, im.crop((0, 0, im.width, k) if where == "head" else (0, k, im.width, im.height)))
        for k in (joins if where == "head" else joins[::-1])
    ]


def _kept_mapper(kept):
    def to_kept(y):
        off = 0
        for s, e in kept:
            if s <= y < e:
                return off + (y - s)
            off += e - s
        return None

    return to_kept


def kept_ranges(kept, a, b):
    """Strip ranges [(s, e)] covered by kept coordinates [a, b) (kept: the strip minus removed blocks)."""
    parts, off = [], 0
    for s, e in kept:
        lo, hi = max(a, off), min(b, off + e - s)
        if lo < hi:
            parts.append((s + lo - off, s + hi - off))
        off += e - s
    return parts


def composited(o):
    """Is a planned output page (a render spec) composited onto white by render? Strip rows and cropped or joined parts
    are; a single uncropped part passes through with its mode (alpha kept)."""
    return "rows" in o or not (len(o["parts"]) == 1 and o["parts"][0][1] is None)


def render(outputs, x_dir, out_dir, strip=None):
    """Write the planned output pages as PNG (lossless intermediates for the AVIF encoder), in order. outputs:
    [{"name": stem, "parts": [[page name, box or None], ...]}] (pages stacked top to bottom, each cropped to its box) or
    [{"name": stem, "rows": [a, b]}] (strip rows in kept coordinates; strip: {"width", "pages": [{"name", "top", "th"}],
    "kept"}). A single uncropped part keeps its mode (a cover passes through). Returns the PNG paths."""
    x_dir, out_dir = Path(x_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = [dict(p, path=str(x_dir / p["name"])) for p in strip["pages"]] if strip else None
    written, cache = [], {}
    for o in outputs:
        if "rows" in o:
            a, b = o["rows"]
            im, y = Image.new("RGB", (strip["width"], b - a), "white"), 0
            for s, e in kept_ranges(strip["kept"], a, b):
                im.paste(render_rows(pages, strip["width"], s, e, cache), (0, y))
                y += e - s
        elif not composited(o):
            im = images.open_image(x_dir / o["parts"][0][0])[0]
        else:
            parts = [_open(x_dir / n) if box is None else _open(x_dir / n).crop(tuple(box)) for n, box in o["parts"]]
            im, y = Image.new("RGB", (parts[0].width, sum(p.height for p in parts)), "white"), 0
            for p in parts:
                im.paste(p, (0, y))
                y += p.height
        path = out_dir / f"{o['name']}.png"
        images.save_png(im, path)
        written.append(path)
    return written


def output_name(n):
    """Stem of the n-th rendered page (1-based): 001, 002, ..."""
    return f"{n:0{STEM_WIDTH}d}"


# ---------------------------------------------------------------- page-format: stitched pages


def page_aspect(chapter_pages, stitched_ratio=SLICE_REPAIR["stitched_ratio"]):
    """Candidate series page shapes (h/w), most common first, from ordinary page-format pages; None when
    there are none. Several candidates because sliced chapters add a false one (Little Lord: 1500/1125
    top halves outnumber the true 1600/1125 pages). Pages: (w, h) tuples (the plan inventory)."""
    aspects = Counter()
    for pages in chapter_pages:
        for d in pages:
            if d and d[0] and 0.9 <= d[1] / d[0] < stitched_ratio:  # page-like shapes only, no slivers
                aspects[round(d[1] / d[0], 3)] += 1
    common = [a for a, n in aspects.most_common(3) if n >= 3]
    return common or None


def _best_page_height(heights, width, aspects, cfg):
    """Page height under which the stitched images split into whole pages best: the series' candidate shapes,
    or, when the series has none, heights implied by the images (smallest image = 1, 2 or 3 pages)."""
    smallest = min(heights)
    # image-derived heights can explain any single image, so only use them when the series shape is unknown
    options = [width * a for a in aspects] if aspects else [smallest / k for k in (1, 2, 3)]
    options = [h for h in options if 0.9 * width <= h <= cfg["stitched_ratio"] * width] or [
        width * cfg["fallback_page_aspect"]
    ]
    pref = width * (aspects[0] if aspects else cfg["fallback_page_aspect"])

    def misfit(h):
        return sum(abs(x / h - max(1, round(x / h))) for x in heights)

    return min(options, key=lambda h: (round(misfit(h), 2), abs(h - pref)))


def is_stitched(pages, aspects, cfg=SLICE_REPAIR):
    """Page-format chapter delivered as tall strips of several pages. Pages: (w, h) tuples (the plan inventory)."""
    ratios = [h / w for w, h in (d for d in pages if d and d[0])]
    if not ratios:
        return False
    base = max(aspects) if aspects else cfg["fallback_page_aspect"]
    return statistics.median(ratios) >= cfg["stitched_ratio"] * base


def plan_pages(names, sizes, aspects, cfg=SLICE_REPAIR):
    """Cut a stitched page-format chapter back into pages of the series' shape (sizes only, no decode).
    Stitchers join whole pages, so each source image holds a whole number of pages: an image of height H
    is split into k = round(H / page_h) equal pages. (Snapping to white rows is wrong here: panel gutters
    inside a page are white too.) Images not within snap_window of a whole number of pages are reported
    as irregular for review and still split evenly. Returns {"width", "pages": [{"name", "w", "h", "top", "th"}],
    "height", "cuts", "irregular", "page_h"}."""
    width, pages, height = strip_layout(sizes)
    page_h = _best_page_height([p["th"] for p in pages], width, aspects, cfg)
    cuts, irregular = [0], []
    for n, p in zip(names, pages, strict=True):
        p["name"] = n
        k = max(1, round(p["th"] / page_h))
        if abs(p["th"] - k * page_h) > cfg["snap_window"] * page_h:
            irregular.append({"page": n, "height": p["th"], "pages": p["th"] / page_h})
        cuts += [p["top"] + round(i * p["th"] / k) for i in range(1, k)] + [p["top"] + p["th"]]
    return {
        "width": width,
        "pages": pages,
        "height": height,
        "cuts": cuts,
        "irregular": irregular,
        "page_h": round(page_h),
    }


# ---------------------------------------------------------------- page-format: sliced pages


def slice_pairs(sizes, cfg=SLICE_REPAIR):
    """Indexes i where page i could be a sliver cut off the bottom of page i - 1 (same width, at most max_sliver_ratio
    of its height): find_slices needs the edge rows of pages i - 1 and i."""
    return [
        i
        for i in range(1, len(sizes))
        if sizes[i][0] == sizes[i - 1][0] and sizes[i][1] <= cfg["max_sliver_ratio"] * sizes[i - 1][1]
    ]


def find_slices(sizes, edges, cfg=SLICE_REPAIR):
    """Pairs (i, i+1) where page i+1 is a sliver cut off the bottom of page i: same width, short,
    continuous seam, and actual art on both sides of the cut (blank margins don't count).
    sizes: [(w, h)]; edges: {index: edge_rows(page)} for both pages of every slice_pairs pair."""
    joins = []
    for i in slice_pairs(sizes, cfg):
        (_, h0), (_, h1) = sizes[i - 1], sizes[i]
        bot, top = edges[i - 1][1], edges[i][0]
        textured = bot[-1].std() >= cfg["min_edge_texture"] and top[0].std() >= cfg["min_edge_texture"]
        score = seam_score(bot, top)
        ok = bool(textured and score <= cfg["max_seam_score"])
        joins.append(
            {
                "page": i - 1,
                "sliver": i,
                "heights": [h0, h1],
                "seam": round(score, 2),
                "join": ok,
                "textured": bool(textured),
                "why": "seam" if ok else None,
            }
        )
    # A source that slices pages does it at fixed heights (e.g. every 1,600 px page -> 1,500 + 100): once
    # the seam test confirms a height pair 3+ times, the same pair elsewhere is a slice even where the cut
    # falls on blank margin or a hard panel edge.
    confirmed = Counter(tuple(j["heights"]) for j in joins if j["join"])
    for j in joins:
        if not j["join"] and confirmed[tuple(j["heights"])] >= cfg["pattern_min_joins"]:
            j["join"], j["why"] = True, "pattern"
    return joins


# ---------------------------------------------------------------- page-format: credit bands

# page-format chapter edges: first / last content pages (whole-page junk, page review, frames)
END_HEAD, END_TAIL = 2, 3
SEAM_ROWS = 6  # analysis rows each side of a candidate seam (as edge_rows)


def series_shape(aspects, cfg=SLICE_REPAIR):
    """The page shape (h/w) bands are measured against: page_aspect's most common candidate, else the fallback.
    Not max(): banded pages themselves can be a candidate (My Noble Family ch7-10: 1125x2019, h/w 1.795)."""
    return aspects[0] if aspects else cfg["fallback_page_aspect"]


def _content_indexes(pages):
    """Indexes of non-cover pages (cover-named pages are never reshaped or junk-checked)."""
    return [i for i, p in enumerate(pages) if not archive.is_cover_name(Path(str(p)).name)]


def band_pages(pages, sizes, shape, cfg=SLICE_REPAIR):
    """Indexes of the pages a credit band can sit on, the pages the band scan decodes: non-cover pages taller than the
    series shape (h > w * shape * (1 + band_tolerance)), anywhere in the chapter (Akuyaku Kizoku ch12/13: a Reset Scans
    ad on page 2). Sizes only; a page becomes a band page only when the scan finds a band on it."""
    return [i for i in _content_indexes(pages) if sizes[i][1] > sizes[i][0] * shape * (1 + cfg["band_tolerance"])]


# ---------------------------------------------------------------- page-format: per-page credit review


def edge_indexes(n):
    """Indexes of the first END_HEAD and last END_TAIL of n content pages."""
    return sorted(set(range(min(END_HEAD, n))) | set(range(max(0, n - END_TAIL), n)))


# ---------------------------------------------------------------- the series' layout


def ensure_series_layout(conn, series_id, chapter_pages):
    """The series' layout ("strip" or "pages", contracts section 5): the stored one (from the bulk converter's record
    at the baseline, or computed earlier), else classify_layout over chapter_pages (the sampled chapters' extracted
    page paths), stored with origin "computed". The caller holds no transaction."""
    row = conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()
    if row is not None:
        return row[0]
    layout, _, _ = classify_layout(chapter_pages)
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, %s, 'computed')
            ON CONFLICT (series_id) DO NOTHING""",
            (series_id, layout),
        )
        return conn.execute("SELECT layout FROM series_conversion WHERE series_id = %s", (series_id,)).fetchone()[0]
