"""Junk units of a long-strip chapter (SPEC "Junk decisions": units, decisions).

Ported from the bulk converter's optimizer/strip_junk.py (edge blocks, match_blocks, the tail pass, the join rule, the
kept check) and the strip steps of its planner (plan_scan.strip_chapter, plan_review.tail_review / finish_strip),
reshaped to 1 chapter:

- `scan(strip, ocr_texts)` decodes once and returns a JSON-able scan holding everything `decide` reads: the gaps and
  spans, every edge block with its hard-join pieces and their remainders (signature, OCR text when given), the
  measurements of every content block of the tail region and every remainder (the tail pieces are cut from them),
  and the signatures of the middle blocks (the watermark rule).
- `decide(scan, decide_unit, known, cuts, render)` applies decisions: what is removed (edge blocks, pieces,
  remainders, chains' card and cut ranges, tail pieces, join-rule blank rows), what is left undecided (edge blocks
  with their probes, tail pieces), the keys of the decisions that acted as watermarks (the series remembers them:
  `known`) and the chains that should have matched and did not. It decodes nothing unless a chain or a cut decision
  is tried (`render`).

Chains, cut decisions and card holes are junk.anchored's; decide applies them in the old order (plan_review
tail_review / finish_strip): with them it reads the strip's pixels through `render`. The rest of card (W5, SPEC "Junk
decisions": units; strip_junk.remnant_gaps / rest_members): decide returns the chapter's remnant gaps, rest_members
their members, 1 bundle per chapter (the old one grouped chapters' matching sets into 1 item). The watermark rule
reads this chapter and the watermark keys the series' earlier chapters showed (the old one read every chapter of the
series at once).
Constants are the bulk converter's, frozen: the stored decisions were made with them.
"""

import bisect
import functools
import itertools
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Literal, NamedTuple, NotRequired, TypedDict

import numpy as np
from PIL import Image

from manga_pipeline import db, images, strip
from manga_pipeline.convert import plan
from manga_pipeline.junk import anchored as an
from manga_pipeline.junk import ocr, store

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
    every tail block and remainder, and the signature texts of its middle blocks. The junk stage stores it with its
    content pages and their logos.
    """

    layout: Literal["strip"]
    width: int
    height: int
    gaps: list[list[int]]
    spans: list[list[int]]
    blocks: list[EdgeBlock]
    measured: dict[str, PieceMeasure]
    middle: list[str]


class Matched[R](TypedDict):
    """An edge block, piece or remainder a junk decision removed (match_blocks): rows, edge, deciding row, "match", sig.

    sig: the removed unit's signature text.
    """

    start: int
    end: int
    where: str
    decision: R
    why: str
    sig: str


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
    """A range a block or probe decision removed: rows, edge, the decision's id, why ("match"), its key, the unit's sig.

    chain and added: the decision's chain text (None: none) and decided_at (junk.anchored's unit pool).
    """

    start: int
    end: int
    where: str
    decision: int
    why: str
    key: str
    sig: str
    chain: str | None
    added: str


class Drop(TypedDict):
    """A range a tail rule or the join rule removes: rows, edge, the deciding piece decision's id and the piece's sig.

    decision and sig are None for a range a rule removed (blank, wedge, join).
    """

    start: int
    end: int
    where: str
    decision: int | None
    why: str
    sig: str | None


type Range = Removed | Drop


class RestPiece(TypedDict):
    """A tail piece the tail pass reviews or decides by a piece decision: rows, signature text, verdict (None: none)."""

    start: int
    end: int
    sig: str
    verdict: str | None


class RestGap(TypedDict):
    """A remnant gap (W5): its pieces in strip order, and whether it holds a card hole's kept rows."""

    pieces: list[RestPiece]
    hole: bool


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

    The final removed ranges (after the join rule), the undecided units, why the removal is refused (None: it is not),
    the keys of the decisions that removed a watermark, the texts of the chains that should have matched and did
    not (each holds the chapter for a person), the strip's seeds as it leaves it (seeds_left: where junk cut
    anchors a proposal), and its remnant gaps (remnant_gaps: rest_members' input).
    """

    removed: list[plan.RemovedRange]
    undecided: list[PieceUnit | BlockUnit]
    kept_problem: str | None
    watermarks: list[str]
    misses: list[str]
    seeds: list[an.Seed]
    rest: list[RestGap]


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


type _Want = Callable[[Piece, Image.Image], None]  # want(unit, image): a final unit dict that OCR may read


def _hard_join_pieces(im: Image.Image, s: int, e: int, where: str) -> Iterator[tuple[_Part, _Part]]:
    """Return (piece, rest) per hard join of the edge block `im`.

    im: strip rows [s, e), at the chapter's `where` edge. Walked from the chapter edge inward (strip.edge_pieces): the
    piece and the remainder of the block it leaves.
    """
    for k, piece in strip.edge_pieces(im, where, MAX_SEAM):
        rest = im.crop((0, k, im.width, im.height) if where == "head" else (0, 0, im.width, k))
        (a, b), (x, y) = ((s, s + k), (s + k, e)) if where == "head" else ((s + k, e), (s, s + k))
        yield _Part(a, b, piece), _Part(x, y, rest)


def _unit(s: int, e: int, im: Image.Image) -> Piece:
    """Return the unit of rows [s, e) with image im: its rows and signature (OCR text is added later)."""
    return {"start": s, "end": e, "sig": images.sig_to_str(images.signature(im))}


def _join(piece: _Part, rest: _Part, want: _Want) -> Join:
    """Return a hard join's entry, given to want(entry, image) unless uniform (a uniform piece is never read).

    The piece's unit, "uniform", and "rest", its remainder's {"start", "end", "sig"} (None when uniform).
    """
    uniform = images.is_uniform(piece.im)
    r: Rest | None = None
    if not images.is_uniform(rest.im):
        r = {"start": rest.start, "end": rest.end, "sig": images.sig_to_str(images.signature(rest.im))}
    entry: Join = {**_unit(piece.start, piece.end, piece.im), "uniform": uniform, "rest": r}
    if not uniform:
        want(entry, piece.im)
    return entry


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
    want: _Want,
) -> _Edges:
    """Return the edge blocks among a strip's content blocks [(start, end)].

    render(start, end) -> image, want(unit, image): each final unit dict (edge block or non-uniform join piece) with
    its image. A block under strip.MIN_EDGE_BLOCK_PX or uniform is no unit (a tail one
    is still measured).
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
            joins.append(_join(piece, rest, want))
            if where == "tail":
                measured[f"{rest.start}:{rest.end}"] = measure_piece(rest.im)
        block: EdgeBlock = {**_unit(s, e, im), "where": where, "joins": joins}
        want(block, im)
        edge.append(block)
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
    ocr_texts: Callable[[Sequence[Image.Image]], list[str]] | None = None,
    undecided: Callable[[images.Signature], bool] = lambda _sig: True,
) -> StripScan:
    """Scan a Strip (strip.make_strip over the chapter's non-cover pages).

    ocr_texts(images) -> their texts in order, called once with the image of every edge block and non-uniform
    hard-join piece whose signature `undecided(sig)` (no stored decision) says so; None: no OCR. Its ocr.OcrError
    fails the scan naming the unit's rows. Returns a JSON-able dict.
    """
    gaps = strip.find_gaps(st)
    spans = strip.spans(st)
    blocks_all = strip.content_blocks(st, gaps)
    render = functools.partial(strip.render_rows, st.pages, st.width, cache={})
    to_read: list[tuple[Piece, Image.Image]] = []

    def want(u: Piece, im: Image.Image) -> None:
        if ocr_texts is not None and undecided(images.sig_from_str(u["sig"])):
            to_read.append((u, im))

    edge = _edge_blocks(blocks_all, st.height, render, want)
    if ocr_texts is not None:
        try:
            read = ocr_texts([im for _, im in to_read])
        except ocr.OcrError as e:
            u = to_read[e.index][0]
            msg = f"OCR of strip rows {u['start']}-{u['end']} failed: {type(e.cause).__name__}: {e.cause}"
            raise RuntimeError(msg) from e
        for (u, _), t in zip(to_read, read, strict=True):
            u["text"] = t
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


def match_blocks[R](
    blocks: Iterable[EdgeBlock], approve: Callable[[str, str], R | None]
) -> tuple[list[Matched[R]], list[LeftBlock]]:
    """Apply the stored decisions to the edge blocks; return (removed, left).

    removed [{"start", "end", "where", "decision", "why": "match", "sig"}]; left: the edge blocks no junk decision
    removed, each with its non-uniform pieces as "probes". approve(kind, sig text) -> the deciding junk row or None.
    A block matching whole is removed; else its hard-join pieces are tried from the chapter edge inward and the
    first match is removed, then its remainder (removed when it matches, else left as a block without probes, marked
    "rest": 1 cut per block); else the block is left.
    """
    removed: list[Matched[R]]
    left: list[LeftBlock]
    removed, left = [], []

    def gone(u: Piece | Rest, row: R, where: str) -> None:
        removed.append(
            {"start": u["start"], "end": u["end"], "where": where, "decision": row, "why": "match", "sig": u["sig"]}
        )

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


def watermarks(
    removed: Iterable[Removed],
    middle: Iterable[str],
    known: Iterable[images.Signature] = (),
    chained: Iterable[int] = (),
) -> set[int]:
    """Return the decision ids of this chapter's tail bands that are watermarks, not credits.

    Removed as >= 2 tail bands (each block or probe match 1; chained: the decision of each chain match that removed a
    credit cut's tail band, 1 per match), or, for a block or probe decision, whose key matches a middle block of the
    chapter (an unsignable one matches nothing) or one of `known`, the keys (parsed) the series' earlier chapters
    showed to be watermarks; a cut decision's key is never compared. removed: [{"where", "decision" (id), "key"}].
    """
    bands = [r for r in removed if r["where"] == "tail"]
    counts = Counter([r["decision"] for r in bands] + list(chained))
    marks = {d for d, n in counts.items() if n >= MARK_MIN_BANDS}
    sigs = [s for s in map(images.sig_from_str, middle) if not images.unsignable(s)] + list(known)
    for r in bands:
        if r["decision"] not in marks and any(images.matches(images.sig_from_str(r["key"]), s) for s in sigs):
            marks.add(r["decision"])
    return marks


def tail_pieces(layout: an.Layout, taken: Iterable[strip.Rows], start: int) -> list[TailPiece]:
    """Return the tail pieces from the credit start.

    The rows from `start` to the strip end outside `taken` (the removed ranges and the card holes' kept rows), split at
    every gap, [{"start", "end", "gap"}] in order.
    """
    out: list[TailPiece] = []
    for s, e in an.uncovered(start, layout.height, taken):
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


def soft(layout: an.Layout, r: Range) -> bool:
    """Return whether r is a W1 drop of a gap piece (its rows within 1 gap): the join rule counts its rows as blank."""
    return r["why"] == "blank" and any(s <= r["start"] and r["end"] <= e for s, e in layout.gaps)


def join_rule(layout: an.Layout, removed: Iterable[Range], gutter: int) -> list[Drop]:
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
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2], an.merged(merged[-1][3] + hard))
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
        c = an.merged(
            ([before] if before else [])
            + [(x[1], y[0]) for x, y in itertools.pairwise(g) if y[0] > x[1]]
            + [x for m in g for x in an.uncovered(m[0], m[1], hard)]
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
            parts = [(x, y)] if keep is None else an.uncovered(x, y, [(x + keep[0] - off, x + keep[1] - off)])
            out.extend(
                {"start": s, "end": t, "where": where, "decision": None, "why": "join", "sig": None}
                for s, t in parts
                if t > s
            )
            off += y - x
    return sorted(out, key=lambda r: r["start"])


def settle(layout: an.Layout, removed: Sequence[Range], gutter_px: int) -> list[Range]:
    """Return the removed set after the join rule: the removed ranges but the soft ones, plus join_rule's, sorted."""
    return sorted(
        [r for r in removed if not soft(layout, r)] + join_rule(layout, removed, gutter_px), key=lambda r: r["start"]
    )


def gutter(width: int) -> int:
    """1 gutter in px at strip width (gap_min_rows is given at 1000 px width)."""
    return max(1, round(strip.RESTITCH["gap_min_rows"] * width / 1000))


def kept_problem(layout: an.Layout, removed: Iterable[strip.RowRange]) -> str | None:
    """Return why cutting `removed` out is refused, or None.

    Refused when only blank rows are kept, or under MIN_PAGE from a strip at least that long.
    """
    kept = an.uncovered(0, layout.height, [(r["start"], r["end"]) for r in removed])
    length = sum(e - s for s, e in kept)
    blank = all(layout.blank(s, e) for s, e in kept)
    if (length < MIN_PAGE <= layout.height) or blank:
        return (
            f"junk removal would leave {length} px of the {layout.height} px strip"
            f"{' (only blank rows)' if blank else ''} (minimum {MIN_PAGE})"
        )
    return None


def _measured_pieces(
    layout: an.Layout,
    taken: Iterable[strip.Rows],
    start: int,
    measured: Mapping[str, PieceMeasure],
    render: an.Render | None,
) -> list[ClassedPiece]:
    """Return the tail pieces from `start` (tail_pieces), each with its blank flag and measurements.

    Each with "blank" (a gap, or no span row) and, when not blank, the measurements of its rows: the scan's (measured:
    the scan's "measured"), else rendered and measured (a chain, a cut or a hole cut the piece inside a block); a piece
    the scan did not measure with no render is an error.
    """
    pieces: list[ClassedPiece] = []
    for p in tail_pieces(layout, taken, start):
        if p["gap"] or layout.blank(p["start"], p["end"]):
            pieces.append({**p, "blank": True})
            continue
        m = measured.get(f"{p['start']}:{p['end']}")
        if m is None:
            if render is None:
                msg = f"tail piece {p['start']}-{p['end']} was not measured by the scan"
                raise ValueError(msg)
            m = measure_piece(render(p["start"], p["end"]))
        match m:  # a piece takes its measure's shape: signed, or uniform or unsignable
            case {"sig": None}:
                pieces.append({**p, "blank": False, **m})
            case _:
                pieces.append({**p, "blank": False, **m})
    return pieces


class _Tail(NamedTuple):
    """The tail pass: its pieces, what it drops (W1, W3, piece decisions) and the pieces left for review.

    signed: every piece it reviewed or decided by a piece decision, with that decision's verdict (W5's pieces).
    """

    pieces: list[ClassedPiece]
    drops: list[Drop]
    review: list[PieceUnit]
    signed: list[RestPiece]


def _tail_pass(
    pieces: list[ClassedPiece], bands: Sequence[strip.Rows], others: Sequence[strip.Rows], decide_unit: store.DecideUnit
) -> _Tail:
    """Class and decide the tail pieces (from the first credit band).

    bands, others: the rows of the tail bands that are no watermark, and of every other removed range and card hole.
    The pieces are classed (tail_classes) and decided: blank and wedge pieces drop, a review piece drops on a junk piece
    decision and is left for review when none covers it.
    """
    classes = tail_classes(pieces, bands, others)
    drops: list[Drop]
    review: list[PieceUnit]
    signed: list[RestPiece]
    drops, review, signed = [], [], []
    for i, c in sorted(classes.items(), key=lambda item: item[0]):
        if isinstance(c, str):
            p = pieces[i]
            drops.append(
                {"start": p["start"], "end": p["end"], "where": "tail", "decision": None, "why": c, "sig": None}
            )
            continue
        p = c
        row = decide_unit("piece", p["sig"])
        verdict = None if row is None else str(row["verdict"])
        signed.append({"start": p["start"], "end": p["end"], "sig": p["sig"], "verdict": verdict})
        if row is None:
            review.append({"kind": "piece", "start": p["start"], "end": p["end"], "where": "tail", "sig": p["sig"]})
        elif row["verdict"] == "junk":
            drops.append(
                {
                    "start": p["start"],
                    "end": p["end"],
                    "where": "tail",
                    "decision": row["id"],
                    "why": "piece",
                    "sig": p["sig"],
                }
            )
    return _Tail(pieces, drops, review, signed)


def remnant_gaps(
    pieces: Iterable[RestPiece], bounds: Iterable[strip.Rows], holes: Iterable[strip.Rows]
) -> list[RestGap]:
    """Return a chapter's remnant gaps (W5; the bulk converter's strip_junk.remnant_gaps).

    The runs between 2 consecutive bounds, or after the last, that hold a piece or a card hole's kept rows, in strip
    order. pieces: the tail pieces the tail pass reviews or decides, in strip order; bounds: the ranges steps 1-3
    removed (the tail pass's own drops never bound: they lie inside a gap); holes: card holes' kept rows. A piece's
    or a hole's gap is the count of bound ends at or before its start.
    """
    ends = sorted(e for _, e in bounds)
    gaps: dict[int, RestGap] = {}
    for p in pieces:
        gaps.setdefault(bisect.bisect_right(ends, p["start"]), {"pieces": [], "hole": False})["pieces"].append(p)
    for s, _ in holes:
        gaps.setdefault(bisect.bisect_right(ends, s), {"pieces": [], "hole": False})["hole"] = True
    return [gaps[k] for k in sorted(gaps)]


def rest_members(gaps: Iterable[RestGap], judged: Callable[[str], bool]) -> list[RestPiece]:
    """Return the members of a chapter's rest-of-card bundle (W5; the bulk converter's strip_junk.rest_members).

    The pieces of every gap that is not protected, in strip order: a gap is protected when it holds a card hole's kept
    rows, or a piece judged kept (judged(sig text): an active matching piece keep with no bundle mark) beside a piece
    that is not (a gap whose every piece is judged kept has nothing to protect).
    """
    out: list[RestPiece] = []
    for g in gaps:
        kept = [judged(p["sig"]) for p in g["pieces"]]
        if not g["hole"] and (all(kept) or not any(kept)):
            out += g["pieces"]
    return sorted(out, key=lambda p: p["start"])


def _undecided_blocks(
    left: Iterable[LeftBlock], owned: Sequence[strip.Rows], decide_unit: store.DecideUnit
) -> list[BlockUnit]:
    """Return the edge blocks match_blocks left that nothing removed touches and no block decision covers.

    No removed range or tail piece (`owned`, [(start, end)]) touches them and no block decision covers them; each with
    the probes no probe decision covers (a probe decided keep is no longer offered).
    """
    out: list[BlockUnit] = []
    for b in left:
        if an.hits(b["start"], b["end"], owned):
            continue
        if decide_unit("block", b["sig"]) is None:
            probes = [p for p in b["probes"] if decide_unit("probe", p["sig"]) is None]
            out.append({**b, "kind": "block", "probes": probes})
    return out


def _entry(r: Range) -> plan.RemovedRange:
    """Return a final range as strip_removed holds it: rows and why, and for a decision's range its id and the sig."""
    out: plan.RemovedRange = {"start": r["start"], "end": r["end"], "why": r["why"]}
    if r["decision"] is not None:
        out["decision"] = r["decision"]
        if r["sig"] is not None:
            out["hash"] = r["sig"]
    return out


def _removed(found: Iterable[Matched[db.Row]]) -> list[Removed]:
    """Return step 1's ranges with their deciding row's id, key, chain and decided_at."""
    return [
        {
            "start": r["start"],
            "end": r["end"],
            "where": r["where"],
            "decision": r["decision"]["id"],
            "why": r["why"],
            "key": r["decision"]["key"],
            "sig": r["sig"],
            "chain": r["decision"]["chain"],
            "added": r["decision"]["decided_at"],
        }
        for r in found
    ]


def _drop(a: an.Added) -> Drop:
    """Return a range a chain added (card or cut) as the tail rules and the join rule read it: no unit signature."""
    return {
        "start": a["start"],
        "end": a["end"],
        "where": a["where"],
        "decision": a["decision"],
        "why": a["why"],
        "sig": None,
    }


def _no_match() -> an.Anchored:
    return {"added": [], "matched": [], "matches": [], "holes": [], "misses": []}


def _anchor(
    layout: an.Layout, removed: list[Removed], cuts: Sequence[an.Entry], render: an.Render | None
) -> tuple[an.View | None, an.Anchored]:
    """Apply steps 2-3: the chains of the removed units at their edges and the cut decisions at every seed.

    Returns the strip as the matcher reads it (None: nothing to try, no pixels read) and what the matcher added. A
    chain or cut decision to try with no render is an error.
    """
    pool = [*cuts, *an.unit_pool(removed)]
    if not pool:
        return None, _no_match()
    if render is None:
        msg = "a chained or cut junk decision is tried, but decide was given no render of the strip"
        raise ValueError(msg)
    view = an.View(layout, an.span_sigs(render), an.extender(layout, render), EDGE_PX)
    return view, an.anchored_ranges(view, removed, an.seeds(layout, removed, EDGE_PX), pool)


def _tail_bands(
    removed: Sequence[Removed], got: an.Anchored, middle: Iterable[str], known: Iterable[images.Signature]
) -> tuple[set[int], list[strip.Rows]]:
    """Step 4's tail bands: (the watermark decision ids, the rows of the bands that are no watermark, sorted).

    A tail band is a tail range a block or probe decision removed, or a credit cut decision's; a card range or a cut
    without credit is none. A chain match that removed a credit band counts 1 band (watermarks' `chained`).
    """
    cut_bands = [a for a in got["added"] if a["where"] == "tail" and a["why"] == "cut" and a["credit"]]
    rows = {(a["start"], a["end"]) for a in cut_bands}
    chained = [m["decision"] for m in got["matches"] if rows & set(m["ranges"])]
    marks = watermarks(removed, middle, known, chained)
    bands = [(r["start"], r["end"]) for r in removed if r["where"] == "tail" and r["decision"] not in marks]
    bands += [(a["start"], a["end"]) for a in cut_bands if a["decision"] not in marks]
    return marks, sorted(bands)


def _late_cuts(
    view: an.View | None, taken: Sequence[an.Ranged], drops: Sequence[Drop], cuts: Sequence[an.Entry]
) -> an.Anchored:
    """Step 5: the cut decisions again, seeded only by the tail pass's drops (their edges in an edge region)."""
    if view is None or not drops or not cuts:
        return _no_match()
    start = [s for s in an.seeds(view.layout, drops, EDGE_PX) if s["kind"] == "removed"]
    return an.anchored_ranges(view, taken, start, cuts)


def seeds_left(layout: an.Layout, ranges: Iterable[Range], matches: Iterable[an.Match]) -> list[an.Seed]:
    """Return the seeds a decide leaves (the bulk converter's junk_cut._seed_list).

    an.seeds over the removed ranges before the join rule (steps 1-5), then each match's far edge as a chained seed of
    its side; a position seeded twice is listed twice. A chained seed takes the region of the seed its match was made
    at (the old code computed the far edge's own); nothing reads the region.
    """
    out = an.seeds(layout, ranges, EDGE_PX)
    for m in matches:
        far = m["span"][1] if m["side"] == "below" else m["span"][0]
        out.append({"side": m["side"], "pos": far, "region": m["region"], "kind": "chained", "units": []})
    return out


def miss_text(m: an.Miss) -> str:
    """Return the text of a chain that should have matched and did not (SPEC "Junk decisions": chains and cuts)."""
    what = "card" if m["unit"] is not None else "cut"
    if m["why"] == "cap" and m["segment"] is not None:
        a, b = m["segment"]
        return (
            f"junk decision {m['decision']}'s {what} grows past its segment at rows {a}-{b}: fix or undo the decision"
        )
    if m["unit"] is None:
        msg = f"junk decision {m['decision']}: a {m['why']} miss names no unit"
        raise ValueError(msg)
    a, b = m["unit"]
    return (
        f"junk decision {m['decision']}'s card ({m['side']}) does not match beside rows {a}-{b}: "
        "fix or undo the decision"
    )


def decide(
    sc: StripScan,
    decide_unit: store.DecideUnit,
    known: Iterable[images.Signature] = (),
    cuts: Iterable[an.CutRow] = (),
    render: an.Render | None = None,
) -> StripDecision:
    """Apply decisions to a strip scan, in the old order (SPEC "Junk decisions": chains and cuts).

    decide_unit(kind, sig text) -> the deciding row (dict-like with "id", "verdict", "key", "chain", "decided_at") or
    None; known: the series' watermark keys (parsed); cuts: the series' active cut decisions, oldest first; render(a,
    b): the image of strip rows [a, b), called only when a chain or a cut decision is tried (None: none may be).
    Returns {"removed": final ranges (after the join rule), "undecided": [{"kind", "start", "end", "where", "sig",
    "text"?, "probes"?}], "kept_problem": text or None, "watermarks": the keys of the block or probe decisions that
    removed a watermark here, "misses": the texts of the chains that should have matched and did not, "seeds", "rest":
    the remnant gaps (W5) of the pieces the tail pass reviewed or decided, bounded by the ranges steps 1-3 removed}.
    """
    layout = an.Layout(sc["height"], sc["width"], sc["spans"], sc["gaps"])

    def approve(kind: str, sig: str) -> db.Row | None:
        row = decide_unit(kind, sig)
        return row if row is not None and row["verdict"] == "junk" else None

    found, left = match_blocks(sc["blocks"], approve)  # 1
    removed = _removed(found)
    cut_entries = an.cut_pool(cuts)
    view, got = _anchor(layout, removed, cut_entries, render)  # 2-3
    ranged: list[Range] = sorted([*removed, *map(_drop, got["added"])], key=lambda r: r["start"])
    marks, bands = _tail_bands(removed, got, sc["middle"], known)  # 4
    holes = an.card_holes(layout.height, ranged, got["holes"], EDGE_PX)
    taken = [(r["start"], r["end"]) for r in ranged]
    # only a chain or a cut makes a piece edge the scan never measured: without one, a missing measure fails fast
    measure = render if view is not None else None
    pieces = _measured_pieces(layout, taken + holes, bands[0][0], sc["measured"], measure) if bands else []
    tail = _tail_pass(pieces, bands, [r for r in taken if r not in bands] + holes, decide_unit)
    late = _late_cuts(view, [*ranged, *tail.drops], tail.drops, cut_entries)  # 5
    late_ranges = [_drop(a) for a in late["added"]]
    late_rows = [(r["start"], r["end"]) for r in late_ranges]
    review = [p for p in tail.review if an.uncovered(p["start"], p["end"], late_rows)]  # wholly cut: withdrawn
    signed = [p for p in tail.signed if an.uncovered(p["start"], p["end"], late_rows)]  # nor a W5 piece
    everything = sorted([*ranged, *tail.drops, *late_ranges], key=lambda r: r["start"])
    owned = (
        [(r["start"], r["end"]) for r in everything]
        + [(p["start"], p["end"]) for p in pieces]
        + got["matched"]
        + late["matched"]
    )
    undecided = review + _undecided_blocks(left, owned, decide_unit)  # 6
    final = settle(layout, everything, gutter(layout.width))  # 7
    return {
        "removed": [_entry(r) for r in final],
        "undecided": sorted(undecided, key=lambda u: u["start"]),
        "kept_problem": kept_problem(layout, final) if final else None,
        "watermarks": sorted({r["key"] for r in removed if r["decision"] in marks}),
        "misses": sorted({miss_text(m) for m in got["misses"] + late["misses"]}),
        "seeds": seeds_left(layout, everything, got["matches"] + late["matches"]),
        "rest": remnant_gaps(signed, taken, holes),
    }
