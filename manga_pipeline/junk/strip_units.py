"""Junk units of a long-strip chapter (SPEC "Junk decisions": units, decisions).

Ported from the bulk converter's optimizer/strip_junk.py (edge blocks, match_blocks, the tail pass, the join rule, the
kept check) and the strip steps of its planner (plan_scan.strip_chapter, plan_review.tail_review / finish_strip),
reshaped to 1 chapter:

- `scan(strip, ocr_text)` decodes once and returns a JSON-able scan holding everything `decide` reads: the gaps and
  spans, every edge block with its hard-join pieces and their remainders (signature, OCR text when given), the
  measurements of every content block of the tail region and every remainder (the tail pieces are cut from them),
  and the signatures of the middle blocks (the watermark rule).
- `decide(scan, decide_unit, known)` applies decisions with no decode: what is removed (edge blocks, pieces,
  remainders, tail pieces, join-rule blank rows), what is left undecided (edge blocks with their probes, tail
  pieces), and the keys of the decisions that acted as watermarks (the series remembers them: `known`).

Not ported (SPEC "Junk decisions", D5.1): chains, cut rows and card holes; the rest-of-card bundles (each tail piece
is its own unit). The watermark rule reads this chapter and the watermark keys the series' earlier chapters showed
(the old one read every chapter of the series at once).
Constants are the bulk converter's, frozen: the stored decisions were made with them.
"""

import functools
import itertools
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Literal, NamedTuple, NotRequired, TypedDict

import numpy as np
from PIL import Image

from manga_pipeline import db, images, strip
from manga_pipeline.convert import plan
from manga_pipeline.junk import logo_units, store

EDGE_PX = 6000  # edge region: the first and last rows of a strip whose blocks are junk units (old junk_edge_px)
MAX_SEAM = strip.SLICE_REPAIR["max_seam_score"]  # hard-join threshold (slice_repair.max_seam_score)
BLANK_STD = 3  # W1: a tail piece with luma std below this ...
BLANK_INK = 40  # ... and no pixel further than this from its median is blank (strip_tail.blank_std / blank_ink)
WEDGE_PX = 130  # W3: a piece (with gutters) shorter than this between 2 credit bands is dropped (strip_tail.wedge_px)
MIN_PAGE = strip.RESTITCH["min_page"]  # kept check: a strip at least this long is never cut below it
MARK_MIN_BANDS = 2  # tail bands sharing 1 decision that make it a mark


class Piece(TypedDict):
    """A strip unit: its rows [start, end) in strip px, its signature text, and its OCR text when it was read."""

    start: int
    end: int
    sig: str
    text: NotRequired[str]


class Rest(TypedDict):
    """The remainder a hard join leaves of its edge block: its rows [start, end) and its signature text."""

    start: int
    end: int
    sig: str


class Join(Piece):
    """A hard join of an edge block: its edge piece's unit, whether that is uniform, its remainder (None: uniform)."""

    uniform: bool
    rest: Rest | None


class EdgeBlock(Piece):
    """An edge block's unit at the chapter's `where` edge ("head" or "tail"), with its hard joins from the edge in."""

    where: str
    joins: list[Join]


class Measure(TypedDict):
    """What the tail rules read of a gap-free run but its signature: luma std, ink, uniform, unsignable."""

    std: float
    ink: int
    uniform: bool
    unsignable: bool


class SignedMeasure(Measure):
    """A gap-free run's measure with its signature text: the run is neither uniform nor unsignable."""

    sig: str


class UnsignedMeasure(Measure):
    """A gap-free run's measure without a signature: the run is uniform or unsignable (a W1 blank)."""

    sig: None


type PieceMeasure = SignedMeasure | UnsignedMeasure  # measure_piece's, told apart by the signature


class StripScan(TypedDict):
    """A strip chapter's scan (scan), JSON-able: everything decide reads.

    The strip's width and height, its gaps and spans ([start, end] each), its edge blocks, {"start:end": measure} of
    every tail block and remainder, and the signature texts of its middle blocks. content (the content pages' download
    indexes) and marks (their logos) are added to it by its caller.
    """

    layout: Literal["strip"]
    width: int
    height: int
    gaps: list[list[int]]
    spans: list[list[int]]
    blocks: list[EdgeBlock]
    measured: dict[str, PieceMeasure]
    middle: list[str]
    content: NotRequired[list[int]]
    marks: NotRequired[list[logo_units.StripLogo]]


class Matched[R](TypedDict):
    """An edge block, piece or remainder a junk decision removed (match_blocks): rows, edge, deciding row, "match"."""

    start: int
    end: int
    where: str
    decision: R
    why: str


class LeftBlock(Piece):
    """An edge block no junk decision removed: its edge, its non-uniform pieces as probes, rest for a remainder."""

    where: str
    probes: list[Piece]
    rest: NotRequired[bool]


class BlockUnit(LeftBlock):
    """An undecided edge block, with the probes no decision covers."""

    kind: Literal["block"]


class PieceUnit(TypedDict):
    """An undecided tail piece: its rows [start, end), its edge ("tail") and its signature text."""

    kind: Literal["piece"]
    start: int
    end: int
    where: str
    sig: str


class Removed(TypedDict):
    """A range a junk decision removed: its rows, edge, the decision's id, why ("match") and the decision's key."""

    start: int
    end: int
    where: str
    decision: int
    why: str
    key: str


class Drop(TypedDict):
    """A range a tail rule or the join rule removes: its rows, edge, the deciding piece decision's id (None: a rule)."""

    start: int
    end: int
    where: str
    decision: int | None
    why: str


type Range = Removed | Drop


class TailPiece(TypedDict):
    """A tail piece (tail_pieces): kept rows [start, end) from the credit start, and whether they are a gap."""

    start: int
    end: int
    gap: bool


class BlankPiece(TailPiece):
    """A tail piece that is a gap or holds no span row."""

    blank: Literal[True]


class SignedPiece(TailPiece, SignedMeasure):
    """A tail piece holding span rows, with the scan's measurements of its rows and their signature."""

    blank: Literal[False]


class UnsignedPiece(TailPiece, UnsignedMeasure):
    """A tail piece holding span rows that are uniform or unsignable, with the scan's measurements of them."""

    blank: Literal[False]


type ClassedPiece = BlankPiece | UnsignedPiece | SignedPiece


class StripDecision(TypedDict):
    """What decisions make of a strip scan (decide).

    The final removed ranges (after the join rule), the undecided units, why the removal is refused (None: it is not)
    and the keys of the decisions that removed a watermark.
    """

    removed: list[plan.RemovedRange]
    undecided: list[PieceUnit | BlockUnit]
    kept_problem: str | None
    watermarks: list[str]


def signable(im: Image.Image) -> str | None:
    """Return the signature text of an image, or None when it is uniform or unsignable."""
    if images.is_uniform(im):
        return None
    sig = images.signature(im)
    return None if images.unsignable(sig) else images.sig_to_str(sig)


def measure_piece(im: Image.Image) -> PieceMeasure:
    """Measure what the tail rules read of a gap-free run.

    Luma std, ink (largest luma distance from its median), uniform, unsignable, signature text (None when uniform or
    unsignable).
    """
    g = np.asarray(im.convert("L"), np.int16)
    uniform = images.is_uniform(im)
    sig = None if uniform else images.signature(im)
    unsignable = sig is not None and images.unsignable(sig)
    std, ink = float(g.std()), int(np.abs(g - np.median(g)).max())
    if sig is None or unsignable:
        return {"std": std, "ink": ink, "uniform": uniform, "unsignable": unsignable, "sig": None}
    return {"std": std, "ink": ink, "uniform": uniform, "unsignable": unsignable, "sig": images.sig_to_str(sig)}


# ---------------------------------------------------------------- the scan, which decodes


class _Part(NamedTuple):
    """Strip rows [start, end) of an edge block and their image."""

    start: int
    end: int
    im: Image.Image


def _hard_join_pieces(im: Image.Image, s: int, e: int, where: str) -> Iterator[tuple[_Part, _Part]]:
    """Return (piece, rest) per hard join of the edge block `im`.

    im: strip rows [s, e), at the chapter's `where` edge. Walked from the chapter edge inward (strip.edge_pieces): the
    piece and the remainder of the block it leaves.
    """
    for k, piece in strip.edge_pieces(im, where, MAX_SEAM):
        rest = im.crop((0, k, im.width, im.height) if where == "head" else (0, 0, im.width, k))
        (a, b), (x, y) = ((s, s + k), (s + k, e)) if where == "head" else ((s + k, e), (s, s + k))
        yield _Part(a, b, piece), _Part(x, y, rest)


def _join(piece: _Part, rest: _Part, unit: Callable[[int, int, Image.Image], Piece]) -> Join:
    """Return a hard join's entry.

    The piece's unit (a uniform one's signature only, never OCR), "uniform", and "rest", its remainder's {"start",
    "end", "sig"} (None when uniform).
    """
    uniform = images.is_uniform(piece.im)
    join: Piece = (
        {"start": piece.start, "end": piece.end, "sig": images.sig_to_str(images.signature(piece.im))}
        if uniform
        else unit(piece.start, piece.end, piece.im)
    )
    r: Rest | None = None
    if not images.is_uniform(rest.im):
        r = {"start": rest.start, "end": rest.end, "sig": images.sig_to_str(images.signature(rest.im))}
    return {**join, "uniform": uniform, "rest": r}


class _Edges(NamedTuple):
    """The edge blocks of a strip (each with its hard joins) and the measures of tail blocks and remainders.

    {"start:end": measure_piece} of every tail block and every remainder of one.
    """

    blocks: list[EdgeBlock]
    measured: dict[str, PieceMeasure]


def _edge_blocks(
    blocks: Iterable[strip.Rows],
    height: int,
    render: Callable[[int, int], Image.Image],
    unit: Callable[[int, int, Image.Image], Piece],
) -> _Edges:
    """Return the edge blocks among a strip's content blocks [(start, end)].

    render(start, end) -> image, unit(start, end, image) -> a unit. A block under strip.MIN_EDGE_BLOCK_PX or uniform is
    no unit (a tail one is still measured).
    """
    edge: list[EdgeBlock]
    measured: dict[str, PieceMeasure]
    edge, measured = [], {}
    for s, e in blocks:
        where = strip.edge_region(s, e, height, EDGE_PX)
        if where is None:
            continue
        im = render(s, e)
        if where == "tail":
            measured[f"{s}:{e}"] = measure_piece(im)
        if e - s < strip.MIN_EDGE_BLOCK_PX or images.is_uniform(im):
            continue
        joins: list[Join] = []
        for piece, rest in _hard_join_pieces(im, s, e, where):
            joins.append(_join(piece, rest, unit))
            if where == "tail":
                measured[f"{rest.start}:{rest.end}"] = measure_piece(rest.im)
        edge.append({**unit(s, e, im), "where": where, "joins": joins})
    return _Edges(edge, measured)


def _middle_sigs(blocks: Iterable[strip.Rows], height: int, render: Callable[[int, int], Image.Image]) -> list[str]:
    """Return the signature texts of the non-uniform middle blocks that could match a tail band.

    Middle blocks lie wholly outside both edge regions; one could match a tail band when at least
    strip.MIN_EDGE_BLOCK_PX tall and at most what an edge region allows.
    """
    tallest = EDGE_PX / (1 - images.ASPECT_TOLERANCE) ** 2  # a taller block can match no tail band
    middle: list[str] = []
    for s, e in blocks:
        if e - s < strip.MIN_EDGE_BLOCK_PX or e - s > tallest or e <= EDGE_PX or s >= height - EDGE_PX:
            continue
        im = render(s, e)
        if not images.is_uniform(im):
            middle.append(images.sig_to_str(images.signature(im)))
    return middle


def scan(
    st: strip.Strip,
    ocr_text: Callable[[Image.Image], str] | None = None,
    undecided: Callable[[images.Signature], bool] = lambda _sig: True,
) -> StripScan:
    """Scan a Strip (strip.make_strip over the chapter's non-cover pages).

    ocr_text(image) -> text, called for every edge block and non-uniform hard-join piece whose signature
    `undecided(sig)` (no stored decision) says so; None: no OCR. Returns a JSON-able dict.
    """
    gaps = strip.find_gaps(st)
    spans = strip.spans(st)
    blocks_all = strip.content_blocks(st, gaps)
    render = functools.partial(strip.render_rows, st.pages, st.width, cache={})

    def unit(s: int, e: int, im: Image.Image) -> Piece:
        sig = images.signature(im)
        u: Piece = {"start": s, "end": e, "sig": images.sig_to_str(sig)}
        if ocr_text is not None and undecided(sig):
            u["text"] = ocr_text(im)
        return u

    edge = _edge_blocks(blocks_all, st.height, render, unit)
    return {
        "layout": "strip",
        "width": st.width,
        "height": st.height,
        "gaps": [list(g) for g in gaps],
        "spans": [list(s) for s in spans],
        "blocks": edge.blocks,
        "measured": edge.measured,
        "middle": _middle_sigs(blocks_all, st.height, render),
    }


# ---------------------------------------------------------------- decide (no decode)


class Layout:
    """The rows of a scanned strip: height, width, spans and gaps (sorted)."""

    def __init__(self, sc: StripScan) -> None:
        """Read the layout from scan sc: its height, width, spans and gaps, sorted."""
        self.height, self.width = sc["height"], sc["width"]
        self.spans = sorted(tuple(s) for s in sc["spans"])
        self.gaps = sorted(tuple(g) for g in sc["gaps"])

    def runs(self) -> list[strip.Rows]:
        """[(start, end)] of the walkable runs between and around the spans (0-row ones included)."""
        out: list[strip.Rows]
        out, pos = [], 0
        for s, e in self.spans:
            out.append((pos, s))
            pos = e
        return [*out, (pos, self.height)]

    def blank(self, a: int, b: int) -> bool:
        """Return whether rows [a, b) hold no span row."""
        return not any(s < b and a < e for s, e in self.spans)


def uncovered(a: int, b: int, taken: Iterable[strip.Rows]) -> list[strip.Rows]:
    """Return the parts of [a, b) outside every (start, end) in taken, in order."""
    out: list[strip.Rows]
    out, pos = [], a
    for s, e in sorted(taken):
        if e <= pos or s >= b:
            continue
        if s > pos:
            out.append((pos, s))
        pos = max(pos, e)
    if pos < b:
        out.append((pos, b))
    return out


def _hits(a: int, b: int, taken: Iterable[strip.Rows]) -> bool:
    return any(s < b and a < e for s, e in taken)


def match_blocks[R](
    blocks: Iterable[EdgeBlock], approve: Callable[[str, str], R | None]
) -> tuple[list[Matched[R]], list[LeftBlock]]:
    """Apply the stored decisions to the edge blocks; return (removed, left).

    removed [{"start", "end", "where", "decision", "why": "match"}]; left: the edge blocks no junk decision removed,
    each with its non-uniform pieces as "probes". approve(kind, sig text) -> the deciding junk row or None. A block
    matching whole is removed; else its hard-join pieces are tried from the chapter edge inward and the first match is
    removed, then its remainder (removed when it matches, else left as a block without probes, marked "rest": 1 cut per
    block); else the block is left.
    """
    removed: list[Matched[R]]
    left: list[LeftBlock]
    removed, left = [], []

    def gone(u: strip.RowRange, row: R, where: str) -> None:
        removed.append({"start": u["start"], "end": u["end"], "where": where, "decision": row, "why": "match"})

    for b in blocks:
        row = approve("block", b["sig"])
        if row is not None:
            gone(b, row, b["where"])
            continue
        split = next(((j, h) for j in b["joins"] for h in [approve("probe", j["sig"])] if h is not None), None)
        if split is None:
            probes = [_piece(j) for j in b["joins"] if not j["uniform"]]
            left.append({**_piece(b), "where": b["where"], "probes": probes})
            continue
        j, row = split
        gone(j, row, b["where"])
        r = j["rest"]
        if r is None:
            continue
        row = approve("block", r["sig"])
        if row is not None:
            gone(r, row, b["where"])
        else:
            left.append({**r, "where": b["where"], "probes": [], "rest": True})
    return removed, left


def _piece(u: Piece) -> Piece:
    """Return a unit's own keys alone: its rows, its signature text and its OCR text when it was read."""
    out: Piece = {"start": u["start"], "end": u["end"], "sig": u["sig"]}
    if "text" in u:
        out["text"] = u["text"]
    return out


def watermarks(removed: Iterable[Removed], middle: Iterable[str], known: Iterable[images.Signature] = ()) -> set[int]:
    """Return the decision ids of this chapter's removed tail bands that are watermarks, not credits.

    Removed as >= 2 tail bands, or whose key matches a middle block of the chapter (an unsignable one matches nothing)
    or one of `known`, the keys (parsed) the series' earlier chapters showed to be watermarks. removed: [{"where",
    "decision" (id), "key"}].
    """
    bands = [r for r in removed if r["where"] == "tail"]
    marks = {d for d, n in Counter(r["decision"] for r in bands).items() if n >= MARK_MIN_BANDS}
    sigs = [s for s in map(images.sig_from_str, middle) if not images.unsignable(s)] + list(known)
    for r in bands:
        if r["decision"] not in marks and any(images.matches(images.sig_from_str(r["key"]), s) for s in sigs):
            marks.add(r["decision"])
    return marks


def tail_pieces(layout: Layout, removed: Iterable[strip.RowRange], start: int) -> list[TailPiece]:
    """Return the tail pieces from the credit start.

    The kept rows from `start` to the strip end split at every gap, [{"start", "end", "gap"}] in order.
    """
    out: list[TailPiece] = []
    for s, e in uncovered(start, layout.height, [(r["start"], r["end"]) for r in removed]):
        pos = s
        for gs, ge in layout.gaps:
            x, y = max(gs, s), min(ge, e)
            if x >= y:
                continue
            if x > pos:
                out.append({"start": pos, "end": x, "gap": False})
            out.append({"start": x, "end": y, "gap": True})
            pos = y
        if pos < e:
            out.append({"start": pos, "end": e, "gap": False})
    return out


def inked(p: ClassedPiece) -> SignedPiece | None:
    """Return a tail piece that is no W1 blank, else None (W1 blank).

    W1 blank: a gap, all blank rows (BlankPiece), uniform or unsignable (an UnsignedPiece, as measure_piece writes it;
    a signed one's flags are read too), or flat (luma std < BLANK_STD and ink <= BLANK_INK).
    """
    match p:
        case {"gap": False, "sig": str()} if not (
            p["uniform"] or p["unsignable"] or (p["std"] < BLANK_STD and p["ink"] <= BLANK_INK)
        ):
            return p
        case _:
            return None


def tail_classes(
    pieces: Sequence[ClassedPiece], bands: Iterable[strip.Rows], others: Iterable[strip.Rows]
) -> dict[int, Literal["blank", "wedge"] | SignedPiece]:
    """Class each tail piece: {index: "blank" | "wedge" | the piece itself, for review}.

    W1 blank (inked); W3 wedge (a non-blank piece whose height plus its adjacent gap pieces is under WEDGE_PX and whose
    nearest non-blank neighbours on both sides are credit bands); review (the rest, a measured and signed piece:
    decided by piece decisions, else a unit). bands, others: [(start, end)] credit bands, every other removed range.
    """
    signed = {i: q for i, p in enumerate(pieces) if (q := inked(p)) is not None}
    out: dict[int, Literal["blank", "wedge"] | SignedPiece] = {
        i: "blank" for i in range(len(pieces)) if i not in signed
    }
    seq = sorted(
        [(s, e, "band") for s, e in bands]
        + [(s, e, "other") for s, e in others]
        + [(p["start"], p["end"], "blank" if i in out else "piece") for i, p in enumerate(pieces)]
    )

    def nearest(k: int, step: int) -> str | None:
        k += step
        while 0 <= k < len(seq) and seq[k][2] == "blank":
            k += step
        return seq[k][2] if 0 <= k < len(seq) else None

    for i, p in signed.items():
        k = seq.index((p["start"], p["end"], "piece"))
        gutters = sum(
            q["end"] - q["start"] for q in pieces if q["gap"] and (q["end"] == p["start"] or q["start"] == p["end"])
        )
        short = p["end"] - p["start"] + gutters < WEDGE_PX
        out[i] = "wedge" if short and nearest(k, -1) == "band" and nearest(k, 1) == "band" else p
    return out


def _merge(rows: Iterable[strip.Rows]) -> list[strip.Rows]:
    out: list[strip.Rows] = []
    for s, e in sorted(rows):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def soft(layout: Layout, r: Range) -> bool:
    """Return whether r is a W1 drop of a gap piece (its rows within 1 gap): the join rule counts its rows as blank."""
    return r["why"] == "blank" and any(s <= r["start"] and r["end"] <= e for s, e in layout.gaps)


def join_rule(layout: Layout, removed: Iterable[Range], gutter: int) -> list[Drop]:
    """Return the ranges of blank rows the join rule removes beside a chapter's removals.

    The blank rows a chapter's removals leave adjacent: at each join (removed ranges separated only by blank rows) the
    walkable runs touching it on both sides, the blank rows between its ranges and the rows of its soft ranges form 1
    run C: at a strip end C goes whole; C >= gutter: C - gutter goes, gutter kept in its middle; C < gutter: C goes
    whole. Returns the ranges it removes ({"why": "join"}), sorted.
    """
    rs = sorted((r["start"], r["end"], r["where"], soft(layout, r)) for r in removed)
    merged: list[tuple[int, int, str, list[strip.Rows]]] = []  # (start, end, where, rows a non-soft range removes)
    for s, e, w, f in rs:
        hard: list[strip.Rows] = [] if f else [(s, e)]
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2], _merge(merged[-1][3] + hard))
        else:
            merged.append((s, e, w, hard))
    groups: list[list[tuple[int, int, str, list[strip.Rows]]]] = []
    for m in merged:
        if groups and layout.blank(groups[-1][-1][1], m[0]):
            groups[-1].append(m)
        else:
            groups.append([m])
    out: list[Drop]
    runs, out = layout.runs(), []
    for g in groups:
        start, end, where = g[0][0], g[-1][1], g[0][2]
        before = next(((rs_, start) for rs_, re_ in runs if rs_ < start <= re_), None)
        after = next(((end, re_) for rs_, re_ in runs if rs_ <= end < re_), None)
        hard = [x for m in g for x in m[3]]
        c = _merge(
            ([before] if before else [])
            + [(x[1], y[0]) for x, y in itertools.pairwise(g) if y[0] > x[1]]
            + [x for m in g for x in uncovered(m[0], m[1], hard)]
            + ([after] if after else [])
        )
        total = sum(y - x for x, y in c)
        if not total:
            continue
        at_end = (
            start == 0 or end == layout.height or (before and before[0] == 0) or (after and after[1] == layout.height)
        )
        keep = None
        if not at_end and total >= gutter:
            k0 = (total - gutter) // 2
            keep = (k0, k0 + gutter)
        off = 0
        for x, y in c:
            parts = [(x, y)] if keep is None else uncovered(x, y, [(x + keep[0] - off, x + keep[1] - off)])
            out.extend(
                {"start": s, "end": t, "where": where, "decision": None, "why": "join"} for s, t in parts if t > s
            )
            off += y - x
    return sorted(out, key=lambda r: r["start"])


def settle(layout: Layout, removed: Sequence[Range], gutter_px: int) -> list[Range]:
    """Return the removed set after the join rule: the removed ranges but the soft ones, plus join_rule's, sorted."""
    return sorted(
        [r for r in removed if not soft(layout, r)] + join_rule(layout, removed, gutter_px), key=lambda r: r["start"]
    )


def gutter(width: int) -> int:
    """1 gutter in px at strip width (gap_min_rows is given at 1000 px width)."""
    return max(1, round(strip.RESTITCH["gap_min_rows"] * width / 1000))


def kept_problem(layout: Layout, removed: Iterable[strip.RowRange]) -> str | None:
    """Return why cutting `removed` out is refused, or None.

    Refused when only blank rows are kept, or under MIN_PAGE from a strip at least that long.
    """
    kept = uncovered(0, layout.height, [(r["start"], r["end"]) for r in removed])
    length = sum(e - s for s, e in kept)
    blank = all(layout.blank(s, e) for s, e in kept)
    if (length < MIN_PAGE <= layout.height) or blank:
        return (
            f"junk removal would leave {length} px of the {layout.height} px strip"
            f"{' (only blank rows)' if blank else ''} (minimum {MIN_PAGE})"
        )
    return None


def _measured_pieces(
    layout: Layout, removed: Iterable[strip.RowRange], start: int, measured: Mapping[str, PieceMeasure]
) -> list[ClassedPiece]:
    """Return the tail pieces from `start` (tail_pieces), each with its blank flag and measurements.

    Each with "blank" (a gap, or no span row) and, when not blank, the scan's measurements of its rows (measured: the
    scan's "measured"); a piece the scan did not measure is an error.
    """
    pieces: list[ClassedPiece] = []
    for p in tail_pieces(layout, removed, start):
        if p["gap"] or layout.blank(p["start"], p["end"]):
            pieces.append({**p, "blank": True})
            continue
        m = measured.get(f"{p['start']}:{p['end']}")
        if m is None:
            msg = f"tail piece {p['start']}-{p['end']} was not measured by the scan"
            raise ValueError(msg)
        match m:  # a piece takes its measure's shape: signed, or uniform or unsignable
            case {"sig": None}:
                pieces.append({**p, "blank": False, **m})
            case _:
                pieces.append({**p, "blank": False, **m})
    return pieces


class _Tail(NamedTuple):
    """The tail pass: its pieces, what it drops (W1, W3, piece decisions) and the pieces left for review."""

    pieces: list[ClassedPiece]
    drops: list[Drop]
    review: list[PieceUnit]


def _tail_pass(
    layout: Layout,
    removed: list[Removed],
    bands: list[Removed],
    measured: Mapping[str, PieceMeasure],
    decide_unit: store.DecideUnit,
) -> _Tail:
    """Class and decide the tail pieces from the first credit band.

    `bands`: the removed tail bands that are no watermark. The pieces are classed (tail_classes) and decided: blank and
    wedge pieces drop, a review piece drops on a junk piece decision and is left for review when none covers it. No
    bands: no tail pass.
    """
    if not bands:
        return _Tail([], [], [])
    pieces = _measured_pieces(layout, removed, min(r["start"] for r in bands), measured)
    classes = tail_classes(
        pieces, [(r["start"], r["end"]) for r in bands], [(r["start"], r["end"]) for r in removed if r not in bands]
    )
    drops: list[Drop]
    review: list[PieceUnit]
    drops, review = [], []
    for i, c in sorted(classes.items(), key=lambda item: item[0]):
        if isinstance(c, str):
            p = pieces[i]
            drops.append({"start": p["start"], "end": p["end"], "where": "tail", "decision": None, "why": c})
            continue
        p = c
        row = decide_unit("piece", p["sig"])
        if row is None:
            review.append({"kind": "piece", "start": p["start"], "end": p["end"], "where": "tail", "sig": p["sig"]})
        elif row["verdict"] == "junk":
            drops.append({"start": p["start"], "end": p["end"], "where": "tail", "decision": row["id"], "why": "piece"})
    return _Tail(pieces, drops, review)


def _undecided_blocks(
    left: Iterable[LeftBlock], owned: Sequence[strip.Rows], decide_unit: store.DecideUnit
) -> list[BlockUnit]:
    """Return the edge blocks match_blocks left that nothing removed touches and no block decision covers.

    No removed range or tail piece (`owned`, [(start, end)]) touches them and no block decision covers them; each with
    the probes no probe decision covers (a probe decided keep is no longer offered).
    """
    out: list[BlockUnit] = []
    for b in left:
        if _hits(b["start"], b["end"], owned):
            continue
        if decide_unit("block", b["sig"]) is None:
            probes = [p for p in b["probes"] if decide_unit("probe", p["sig"]) is None]
            out.append({**b, "kind": "block", "probes": probes})
    return out


def decide(sc: StripScan, decide_unit: store.DecideUnit, known: Iterable[images.Signature] = ()) -> StripDecision:
    """Apply decisions to a strip scan.

    decide_unit(kind, sig text) -> the deciding row (dict-like with "id", "verdict", "key") or None; known: the series'
    watermark keys (parsed). Returns {"removed": final ranges (after the join rule), "undecided": [{"kind", "start",
    "end", "where", "sig", "text"?, "probes"?}], "kept_problem": text or None, "watermarks": the keys of the decisions
    that removed a watermark here}.
    """
    layout = Layout(sc)

    def approve(kind: str, sig: str) -> db.Row | None:
        row = decide_unit(kind, sig)
        return row if row is not None and row["verdict"] == "junk" else None

    found, left = match_blocks(sc["blocks"], approve)
    removed: list[Removed] = [
        {
            "start": r["start"],
            "end": r["end"],
            "where": r["where"],
            "decision": r["decision"]["id"],
            "why": r["why"],
            "key": r["decision"]["key"],
        }
        for r in found
    ]
    marks = watermarks(removed, sc["middle"], known)
    bands = [r for r in removed if r["where"] == "tail" and r["decision"] not in marks]
    tail = _tail_pass(layout, removed, bands, sc["measured"], decide_unit)
    everything = sorted(removed + tail.drops, key=lambda r: r["start"])
    owned = [(r["start"], r["end"]) for r in everything] + [(p["start"], p["end"]) for p in tail.pieces]
    undecided = tail.review + _undecided_blocks(left, owned, decide_unit)
    final = settle(layout, everything, gutter(layout.width))
    return {
        "removed": [{"start": r["start"], "end": r["end"], "why": r["why"]} for r in final],
        "undecided": sorted(undecided, key=lambda u: u["start"]),
        "kept_problem": kept_problem(layout, final) if final else None,
        "watermarks": sorted({r["key"] for r in removed if r["decision"] in marks}),
    }
