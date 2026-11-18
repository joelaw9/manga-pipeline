"""Junk units of a page-format chapter (SPEC "Junk decisions": units, decisions, the chapter edit).

Ported from the bulk converter's optimizer/page_junk.py (credit bands, ink box, end-page size and text rules),
junk.is_white, and the page-format steps of its planner (plan_scan.page_chapter / white_edges, plan_review.page_review /
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
from collections.abc import Callable, Container, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NamedTuple, NotRequired, ReadOnly, TypedDict

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from manga_pipeline import db, images, strip
from manga_pipeline.convert import plan
from manga_pipeline.junk import frames, logo_units, store


class BandSettings(strip.SliceRepair, strip.Restitch):
    """The band and page-review rules' settings, read only: strip's slice-repair and restitch ones, then their own."""

    band_min_px: ReadOnly[int]
    band_seam_min_px: ReadOnly[int]
    band_search: ReadOnly[float]
    page_review_width_tolerance: ReadOnly[float]
    page_review_aspect_margin: ReadOnly[float]


BAND: BandSettings = {
    **strip.SLICE_REPAIR,
    **strip.RESTITCH,
    "band_min_px": 16,  # a band (and what it leaves of the page) is at least this tall
    "band_seam_min_px": 100,  # a band cut on a seam (no gutter) is at least this tall
    "band_search": 0.12,  # the cut is looked for within this share of the page height around the naive cut
    "page_review_width_tolerance": 0.05,  # size rule: a width this far off the modal width is another size
    "page_review_aspect_margin": 0.01,  # size rule: the middle pages' aspect range widened by this
}
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
CREDIT_MIN_PAGES = 2  # the last page and 1 other to compare it with
BOTH_BANDS = 2  # a page's top and bottom band


class Band(TypedDict):
    """A credit band on a page (find_band), in source px.

    Its edge ("top" | "bottom"), the row it is cut at, its height, how it is cut ("gap" | "seam"), the signature text
    of its ink box, and its box [x0, y0, x1, y1].
    """

    where: str
    cut: int
    height: int
    how: str
    sig: str
    box: list[int]


class TextFeatures(TypedDict):
    """A page's share of plain, mid-tone and coloured pixels (text_features)."""

    plain: float
    mid: float
    chroma: float


class EdgeUnit(TypedDict):
    """An edge page's unit (scan).

    Its page (download index), whole-page signature text (None when uniform), frame key text (None when uniform or the
    frame has no content), and its text features (tail pages only).
    """

    page: int
    sig: str | None
    frame: str | None
    text: NotRequired[TextFeatures]


class PageScan(TypedDict):
    """A page-format chapter's scan (scan), JSON-able; page indexes are download indexes, text in its keys.

    The content pages, every page's size [w, h], the edge pages' units, the white pages, {page: its bands}, {box key:
    packed edge rows} of the content pages whole and as each band crop leaves them, the series' page aspects, and the
    content pages' logos.
    """

    layout: Literal["pages"]
    split: Literal[False]
    content: list[int]
    sizes: list[list[int]]
    edges: list[EdgeUnit]
    white: list[int]
    bands: dict[str, list[Band]]
    rows: dict[str, list[str]]
    aspects: list[float]
    marks: list[logo_units.Logo]


class SplitScan(TypedDict):
    """A stitched page-format chapter's scan: only that it is split (no logos)."""

    layout: Literal["pages"]
    split: Literal[True]
    marks: list[logo_units.Logo]


class PageUnit(TypedDict):
    """An undecided edge page: its kind ("page" by its signature, "frame" by its frame), key text, page, reasons."""

    kind: Literal["page", "frame"]
    key: str
    page: int
    reasons: list[str]


class BandUnit(TypedDict):
    """An undecided band: its key (signature text), page, edge, box [x0, y0, x1, y1] and reasons."""

    kind: Literal["band"]
    key: str
    page: int
    where: str
    box: list[int]
    reasons: list[str]


class PageDecision(TypedDict):
    """What decisions make of a page-format scan (decide): the chapter edit's page-layout keys, the undecided units."""

    edit: plan.PageLayoutEdit
    undecided: list[PageUnit | BandUnit]


# ---------------------------------------------------------------- page measurements


def is_white(im: Image.Image, white_min: float = END_PAGES["white_min"]) -> bool:
    """Return whether im is a white page: uniform, and every channel's minimum at least white_min."""
    lo_hi = im.convert("RGB").getextrema()
    if len(lo_hi) != images.RGB_CHANNELS:  # Pillow types a 1-band image's (min, max) in the same union
        msg = f"getextrema of an RGB image gave {lo_hi!r}, not 1 (min, max) per channel"
        raise TypeError(msg)
    return images.is_uniform(im) and min(lo for lo, _ in lo_hi) >= white_min


def ink_box(im: Image.Image) -> images.Box | None:
    """Box of the band's ink (pixels more than INK_DELTA luma from the median of its border pixels), or None."""
    g = np.asarray(im.convert("L"), np.int16)
    border = np.concatenate([g[0], g[-1], g[:, 0], g[:, -1]])
    ink = np.abs(g - np.median(border)) > INK_DELTA
    ys, xs = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    if not len(ys):
        return None
    return int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1


def _sharpest_row(g: Image.Image, y: float, step: float) -> int:
    """Full-resolution row within +-1.5 analysis rows of y where the image changes most."""
    y0, y1 = max(1, int(y - 1.5 * step)), min(g.height - 1, int(y + 1.5 * step) + 1)
    rows = np.asarray(g.crop((0, y0 - 1, g.width, y1)), np.float32)
    return y0 + int(np.abs(np.diff(rows, axis=0)).mean(axis=1).argmax())


class _Cut(NamedTuple):
    """Where a band is cut off its page (source px), how ("gap" | "seam"), and for a gap cut the gutter's rows."""

    at: int
    how: str
    run: tuple[int, int] | None


def _gutters(uniform: NDArray[np.bool_], step: float) -> Iterator[tuple[float, float]]:
    """(start, end) in source px of each run of uniform analysis rows (step source px each), in order."""
    y, n = 0, len(uniform)
    while y < n:
        if not uniform[y]:
            y += 1
            continue
        e = y
        while e < n and uniform[e]:
            e += 1
        yield y * step, e * step
        y = e


def _nearest_gutter(
    gutters: Iterable[tuple[float, float]], naive: float, reach: float, h: int
) -> tuple[float, tuple[int, int]] | None:
    """Return (the point nearest the naive cut, the gutter's rows rounded) of the gutters overlapping naive +- reach.

    None when there is none. A page's own top or bottom margin (a gutter at row 0 or the page end h) is no seam.
    """
    lo, hi = naive - reach, naive + reach
    best: tuple[float, tuple[int, int]] | None = None
    for ys, ye in gutters:
        if ye >= lo and ys <= hi and ys > 0 and ye < h:
            c = min(max(naive, ys), ye)
            if best is None or abs(c - naive) < abs(best[0] - naive):
                best = (c, (round(ys), round(ye)))
    return best


def _nearest_seam(a: NDArray[np.float32], step: float, naive: float, reach: float, max_seam_score: float) -> int | None:
    """Return the analysis row within naive +- reach nearest the naive cut whose seam_score is high enough.

    Its discontinuity (seam_score) is at least max_seam_score, the higher score on a tie; None when there is none.
    """
    lo, hi = naive - reach, naive + reach
    best: tuple[tuple[float, float], int] | None = None
    for k in range(max(strip.SEAM_ROWS, int(lo / step)), min(len(a) - strip.SEAM_ROWS, int(hi / step) + 1) + 1):
        score = strip.seam_score(a[k - strip.SEAM_ROWS : k], a[k : k + strip.SEAM_ROWS])
        if score >= max_seam_score and (best is None or (abs(k * step - naive), -score) < best[0]):
            best = ((abs(k * step - naive), -score), k)
    return None if best is None else best[1]


def _band_cut(im: Image.Image, page_h: float, where: str, cfg: BandSettings) -> _Cut | None:
    """Return where to cut a band at `where` off a page whose own part is page_h tall.

    The gutter point nearest the naive cut within band_search of page_h (moved to the gutter edge's sharpest row when it
    is not the naive cut itself), else the seam nearest it at full resolution; None when neither (never cut through
    art).
    """
    w, h = im.size
    aw = cfg["analysis_width"]
    n = max(2 * strip.SEAM_ROWS + 2, round(h * aw / w))
    step = h / n
    g = im.convert("L")
    a = np.asarray(g.resize((aw, n)), np.float32)
    naive = h - page_h if where == "top" else page_h
    reach = cfg["band_search"] * page_h
    gap = _nearest_gutter(_gutters(strip.blank_rows(a, cfg), step), naive, reach, h)
    if gap is not None:
        cut, run = gap
        if cut != naive:  # on the gutter's edge
            at_end = cut < naive
            cut = _sharpest_row(g, cut, step)
            run = (run[0], cut) if at_end else (cut, run[1])
        return _Cut(round(cut), "gap", run)
    k = _nearest_seam(a, step, naive, reach, cfg["max_seam_score"])
    if k is None:
        return None
    return _Cut(round(_sharpest_row(g, k * step, step)), "seam", None)


def _band(im: Image.Image, where: str, cut: _Cut, cfg: BandSettings) -> Band | None:
    """Return the band `cut` makes at `where`: {"where", "cut", "height", "how", "sig" (text), "box"}, or None.

    None when it is no band (it or what it leaves under band_min_px, a seam band under band_seam_min_px, its content
    without the gutter rows uniform or without ink). Its signature is of its ink box.
    """
    w, h = im.size
    box = (0, 0, w, cut.at) if where == "top" else (0, cut.at, w, h)
    height = box[3] - box[1]
    if height < cfg["band_min_px"] or h - height < cfg["band_min_px"]:
        return None
    if cut.how == "seam" and height < cfg["band_seam_min_px"]:
        return None
    run = cut.run
    content = im.crop(box if run is None else (0, 0, w, run[0]) if where == "top" else (0, run[1], w, h))
    if content.height < cfg["band_min_px"] or images.is_uniform(content):
        return None
    ink = ink_box(content)
    if ink is None:
        return None
    sig = images.sig_to_str(images.signature(content.crop(ink)))
    return {"where": where, "cut": cut.at, "height": height, "how": cut.how, "sig": sig, "box": list(box)}


def find_band(im: Image.Image, aspect: float, where: str, cfg: BandSettings = BAND) -> Band | None:
    """Find a credit band stitched onto the top or bottom of a page taller than the series shape.

    It is cut at the gutter nearest the naive cut within band_search of the page height, else at the strongest seam
    (seam_score >= max_seam_score) nearest it; none -> None (never cut through art). The band is at least band_min_px
    and not uniform; one under band_seam_min_px counts only when cut on a gutter. Its signature is of its ink box,
    without the gutter rows. Returns {"where", "cut", "height", "how": "gap" | "seam", "sig" (text), "box"} or None
    (source px).
    """
    w, h = im.size
    page_h = w * aspect
    if h <= page_h * (1 + cfg["band_tolerance"]):
        return None
    cut = _band_cut(im, page_h, where, cfg)
    return None if cut is None else _band(im, where, cut, cfg)


def page_bands(im: Image.Image, aspect: float, cfg: BandSettings = BAND) -> list[Band]:
    """find_band at both edges of a page."""
    return [b for b in (find_band(im, aspect, wh, cfg) for wh in ("top", "bottom")) if b]


def text_features(im: Image.Image) -> TextFeatures:
    """Return a page's share of plain, mid-tone and coloured pixels at TEXT_WIDTH: {"plain", "mid", "chroma"}."""
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


def _pack_edges(edges: strip.EdgeRows) -> list[str]:
    top, bottom = edges
    return [base64.b64encode(np.asarray(x, np.uint8).tobytes()).decode() for x in (top, bottom)]


def _unpack_edges(packed: Sequence[str]) -> strip.EdgeRows:
    top, bottom = (
        np.frombuffer(base64.b64decode(x), np.uint8).reshape(strip.SEAM_ROWS, -1).astype(np.float32) for x in packed
    )
    return top, bottom


# ---------------------------------------------------------------- size and text rules (no decode)


def page_candidates(
    sizes: Sequence[strip.Size],
    aspects: Sequence[float] | None,
    banded: Container[int],
    joined: Container[int],
    cfg: BandSettings = BAND,
) -> list[tuple[int, str]]:
    """Return the edge pages (of the content pages, `sizes` in order) whose size no middle page of the chapter has.

    Another width ("width") or an aspect outside the middle pages' range ("tall" / "short"). Never: band pages, joined
    pages, spreads. Returns [(content index, why)].
    """
    if not sizes:
        return []
    shape = strip.series_shape(aspects)
    width = Counter(w for w, _ in sizes).most_common(1)[0][0]
    wtol, margin = cfg["page_review_width_tolerance"], cfg["page_review_aspect_margin"]
    spread_tolerance = END_PAGES["spread_tolerance"]
    edge = strip.edge_indexes(len(sizes))
    mid = [
        i
        for i in range(len(sizes))
        if i not in edge and i not in banded and i not in joined and abs(sizes[i][0] / width - 1) <= wtol
    ]
    mid_a = [sizes[i][1] / sizes[i][0] for i in mid] or [shape]
    lo, hi = min(mid_a) * (1 - margin), max(mid_a) * (1 + margin)
    out: list[tuple[int, str]] = []
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


def end_text_page(
    sizes: Sequence[strip.Size], texts: Mapping[int, TextFeatures], cfg: Mapping[str, float] = END_PAGES
) -> int | None:
    """Return the content index of the last page when it is a credit page, else None.

    A credit page: of the modal size, mostly plain, little mid-tone, some colour, in a gray chapter (every other tail
    page under gray_chroma); else None. texts: {content index: features}.
    """
    n = len(sizes)
    if n < CREDIT_MIN_PAGES:
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


def _box_key(index: int, box: Sequence[int] | None) -> str:
    return f"{index}" if box is None else f"{index}:{','.join(map(str, box))}"


class _OnePage:
    """Download pages decoded as RGB, holding only the last one asked for (the scan reads 1 page at a time)."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self.paths = paths
        self.opened: dict[int, Image.Image] = {}

    def __call__(self, i: int) -> Image.Image:
        if i not in self.opened:
            self.opened.clear()
            self.opened[i] = images.open_flat(self.paths[i])[0].convert("RGB")
        return self.opened[i]


def _edge_unit(i: int, im: Image.Image, *, uniform: bool, tail: bool) -> EdgeUnit:
    """Return an edge page's unit.

    {"page", "sig" (whole page; None when uniform), "frame" (None when uniform or the frame has no content), "text"
    (text_features; tail pages only)}.
    """
    f = frames.frame(im)
    unit: EdgeUnit = {
        "page": i,
        "sig": None if uniform else images.sig_to_str(images.signature(im)),
        "frame": frames.key_text(f) if not uniform and frames.has_content(f) else None,
    }
    if tail:
        unit["text"] = text_features(im)
    return unit


def _band_pages(
    names: Sequence[str],
    content: Sequence[int],
    csizes: Sequence[strip.Size],
    shape: float,
    page: Callable[[int], Image.Image],
) -> dict[str, list[Band]]:
    """Return {download index (text): page_bands} of the content pages with a band (strip.band_pages).

    csizes: the content pages' sizes; page(i) decodes.
    """
    bands: dict[str, list[Band]] = {}
    for k in strip.band_pages([names[i] for i in content], csizes, shape):
        i = content[k]
        found = page_bands(page(i), shape)
        if found:
            bands[str(i)] = found
    return bands


def _page_rows(i: int, im: Image.Image, found: Sequence[Band], size: strip.Size) -> dict[str, list[str]]:
    """Return {_box_key: packed edge rows} of a content page, whole and as each crop of its bands `found` leaves it.

    Slice repair runs on the pages decisions keep, cropped where cropped.
    """
    rows = {_box_key(i, None): _pack_edges(strip.edge_rows(im))}
    for box in _crop_boxes(found, size):
        rows[_box_key(i, box)] = _pack_edges(strip.edge_rows(im.crop(images.as_box(box))))
    return rows


def scan(
    paths: Sequence[Path],
    names: Sequence[str],
    sizes: Sequence[strip.Size],
    uniform: Sequence[bool],
    aspects: Sequence[float] | None,
) -> PageScan | SplitScan:
    """Scan a page-format chapter.

    paths, names, sizes: every page of the download in archive order; uniform: per page, True when it has no junk
    signature (images.measure_pages); aspects: the series' page aspect candidates (strip.page_aspect). Returns a
    JSON-able dict; page indexes are download indexes.
    """
    content = strip.content_indexes(names)
    csizes = [sizes[i] for i in content]
    if strip.is_stitched(csizes, aspects):
        return {"layout": "pages", "split": True, "marks": []}
    shape = strip.series_shape(aspects)
    page = _OnePage(paths)
    tail_k = range(max(0, len(content) - strip.END_TAIL), len(content))
    edges = [
        _edge_unit(content[k], page(content[k]), uniform=uniform[content[k]], tail=k in tail_k)
        for k in strip.edge_indexes(len(content))
    ]
    white = [i for i in content if uniform[i] and is_white(page(i))]
    bands = _band_pages(names, content, csizes, shape, page)
    rows: dict[str, list[str]]
    logos: list[logo_units.Logo]
    rows, logos = {}, []
    for i in content:
        rows |= _page_rows(i, page(i), bands.get(str(i), []), sizes[i])
        logo = logo_units.measure(i, page(i))
        if logo is not None:
            logos.append(logo)
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


def _crop(found: Iterable[Band], size: strip.Size) -> list[int] | None:
    """Return the kept box of a page once bands `found` are cut off, or None when it would keep under band_min_px."""
    w, h = size
    top = max((b["cut"] for b in found if b["where"] == "top"), default=0)
    bottom = min((b["cut"] for b in found if b["where"] == "bottom"), default=h)
    if bottom - top < BAND["band_min_px"]:
        return None
    return [0, top, w, bottom]


def _crop_boxes(found: Sequence[Band], size: strip.Size) -> list[list[int]]:
    """Every box a page can be cropped to by a non-empty subset of its bands."""
    out = [_crop([b], size) for b in found]
    if len(found) == BOTH_BANDS:
        out.append(_crop(found, size))
    return [b for b in out if b is not None]


# ---------------------------------------------------------------- decide (no decode)


def page_decision(unit: EdgeUnit, decide_unit: store.DecideUnit) -> db.Row | None:
    """Return the row deciding an edge page from its whole-page signature and its frame, or None.

    The signature by page decisions and the frame by frame decisions: the first in lookup order (store.in_lookup_order),
    the page's on a tie; None when neither decides.
    """
    page = decide_unit("page", unit["sig"]) if unit["sig"] else None
    frame = decide_unit("frame", unit["frame"]) if unit["frame"] else None
    hits = [r for r in (page, frame) if r is not None]
    return store.in_lookup_order(hits)[0] if hits else None


class _EdgePass(NamedTuple):
    """What decisions make of the edge pages.

    The pages dropped as junk, the pages a frame decision covers, and {page: (kind, key text)} of the signed ones no
    decision covers: by their frame when it has one, else by their signature.
    """

    drops: set[int]
    framed: set[int]
    undecided: dict[int, tuple[Literal["page", "frame"], str]]


def _edge_pass(edges: Iterable[EdgeUnit], decide_unit: store.DecideUnit) -> _EdgePass:
    """Apply the page and frame decisions to the edge pages (page_decision)."""
    drops: set[int]
    framed: set[int]
    undecided: dict[int, tuple[Literal["page", "frame"], str]]
    drops, framed, undecided = set(), set(), {}
    for u in edges:
        row = page_decision(u, decide_unit)
        if u["frame"] and decide_unit("frame", u["frame"]) is not None:
            framed.add(u["page"])
        if row is None:
            if u["sig"] is not None:
                undecided[u["page"]] = ("frame", u["frame"]) if u["frame"] else ("page", u["sig"])
        elif row["verdict"] == "junk":
            drops.add(u["page"])
    return _EdgePass(drops, framed, undecided)


def _white_ends(rest: Sequence[int], white: Container[int]) -> set[int]:
    """Return the white pages at either end of the pages `rest` (in order), unless every page is white."""
    lead = next((n for n, i in enumerate(rest) if i not in white), len(rest))
    if lead == len(rest):
        return set()
    trail = next(n for n in range(len(rest) - 1, -1, -1) if rest[n] not in white)
    return set(rest[:lead]) | set(rest[trail + 1 :])


class _BandPass(NamedTuple):
    """What band decisions make of the band pages.

    {page: kept box} of the cropped ones, the undecided band units, the pages with a signable band, and the pages whose
    junk bands would leave almost nothing (kept whole).
    """

    crops: dict[int, list[int]]
    units: list[BandUnit]
    banded: set[int]
    too_much: set[int]


def _band_unit(i: int, b: Band) -> BandUnit:
    return {
        "kind": "band",
        "key": b["sig"],
        "page": i,
        "where": b["where"],
        "box": b["box"],
        "reasons": ["band"],  # D5.16: an undecided band is a candidate at 1 chapter
    }


def _band_pass(
    bands: Mapping[str, Sequence[Band]],
    sizes: Sequence[strip.Size],
    skip: Container[int],
    decide_unit: store.DecideUnit,
) -> _BandPass:
    """Apply the band decisions to the band pages (scan "bands") but those in `skip` (dropped or framed).

    An unsignable band is no band (no decision could be written on it): the page is judged as a page.
    """
    crops: dict[int, list[int]]
    units: list[BandUnit]
    banded: set[int]
    too_much: set[int]
    crops, units, banded, too_much = {}, [], set(), set()
    for key, listed in bands.items():
        i = int(key)
        if i in skip:
            continue
        found = [b for b in listed if not store.unsignable("band", b["sig"])]
        if not found:
            continue
        banded.add(i)
        hits: list[Band] = []
        for b in found:
            row = decide_unit("band", b["sig"])
            if row is None:
                units.append(_band_unit(i, b))
            elif row["verdict"] == "junk":
                hits.append(b)
        box = _crop(hits, sizes[i]) if hits else None
        if box is not None:
            crops[i] = box
        elif hits:
            too_much.add(i)  # the bands would leave almost nothing: the page stays whole and is a candidate
    return _BandPass(crops, units, banded, too_much)


def _joins(
    keep: Sequence[int], sizes: Sequence[strip.Size], crops: Mapping[int, list[int]], rows: Mapping[str, Sequence[str]]
) -> list[list[int]]:
    """Run slice repair over the kept pages (in order), cropped where cropped: [[page, sliver]] of the joins it makes.

    rows: the scan's packed edge rows.
    """
    ksizes = [(crops[i][2] - crops[i][0], crops[i][3] - crops[i][1]) if i in crops else sizes[i] for i in keep]
    edges = {
        j: _unpack_edges(rows[_box_key(keep[j], crops.get(keep[j]))])
        for k in strip.slice_pairs(ksizes)
        for j in (k - 1, k)
    }
    return [[keep[j["page"]], keep[j["sliver"]]] for j in strip.find_slices(ksizes, edges) if j["join"]]


def _reasons(sc: PageScan, banded: set[int], joined: set[int]) -> dict[int, list[str]]:
    """Return the per-chapter candidate reasons of a scan's edge pages (SPEC "Junk decisions", candidates).

    {page: [reason]}: size, else text (end_text_page; never a banded or joined page).
    """
    content = sc["content"]
    position = {i: k for k, i in enumerate(content)}
    csizes = [(w, h) for w, h in (sc["sizes"][i] for i in content)]
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
    return reasons


def decide(sc: PageScan | SplitScan, decide_unit: store.DecideUnit) -> PageDecision:
    """Apply decisions to a page-format scan.

    decide_unit(kind, key text) -> the deciding row or None. Returns {"edit": the page-layout keys of the chapter edit,
    "undecided": [{"kind", "key", "page", "where"?, "reasons"}]}.
    """
    if sc["split"] is True:
        return {"edit": {"split": True, "drop_pages": [], "bands": [], "join": []}, "undecided": []}
    content, sizes = sc["content"], [(w, h) for w, h in sc["sizes"]]
    edge = _edge_pass(sc["edges"], decide_unit)
    drops = edge.drops | _white_ends([i for i in content if i not in edge.drops], set(sc["white"]))
    band = _band_pass(sc["bands"], sizes, drops | edge.framed, decide_unit)
    joins = _joins([i for i in content if i not in drops], sizes, band.crops, sc["rows"])
    joined = {i for pair in joins for i in pair}
    reasons = _reasons(sc, band.banded, joined)
    # "band" for an edge page with an undecided band, or whose junk bands would leave too little
    band_pages = {b["page"] for b in band.units} | band.too_much
    for i in sorted(band_pages & {u["page"] for u in sc["edges"]}):
        reasons.setdefault(i, []).append("band")
    undecided: list[PageUnit] = []
    for i, (kind, key) in sorted(edge.undecided.items()):
        if i in drops:
            continue
        undecided.append({"kind": kind, "key": key, "page": i, "reasons": reasons.get(i, [])})
    return {
        "edit": {
            "split": False,
            "drop_pages": sorted(drops),
            "bands": [{"page": i, "box": band.crops[i]} for i in sorted(band.crops)],
            "join": joins,
        },
        "undecided": undecided + band.units,
    }
