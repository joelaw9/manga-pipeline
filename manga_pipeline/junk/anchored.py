"""Chains and cut decisions on a long strip (SPEC "Junk decisions": chains and cuts): the matcher.

Ported from the bulk converter's optimizer/strip_junk.py: `Layout` with `next_span`, `neighbours`, `region` and
`region_bounds` (:165-227), `seeds` (:230-257), the walk (`_h_ok`, `exact_rows`, `_place`), `try_version`, `nominal`,
`_gap_ok`, `_off_half`, `anchored_ranges` with `_at_seed` and `_extended` (:260-554), `overlap_extend` with `_dilate`
and `_reached` (:660-727), `card_holes` (:766-780) and `signable` (:90-95); the pools from `cut_entries` /
`unit_entries` (:434-464); `tiles_match` from optimizer/junk.py (`tile_distance` :121-127, `Library.tiles_match`
:379-384); `span_sigs` from plan_scan's `span_sig` (:385-388), rendered at decide instead of cached with the scan.

Not ported: `skip` segments and `none` exemptions (skip and none are refused (no command makes them): junk.chains)
and global rows (none are made). Pure but for `render`, which returns an image of strip rows [a, b).
Constants are the bulk converter's, frozen: the stored chains were learned with them.
"""

import bisect
import functools
from collections.abc import Callable, Iterable, Sequence
from typing import Literal, NamedTuple, Protocol, ReadOnly, TypedDict

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import NDArray
from PIL import Image

from manga_pipeline import images, strip
from manga_pipeline.junk import chains

type Side = Literal["above", "below"]
type Region = Literal["head", "tail"]
type SeedKind = Literal["removed", "strip", "chained", "gap"]
type State = Literal["match", "covered", "signature", "clamped", "uniform", "off", "gap"]
type Render = Callable[[int, int], Image.Image]
type SpanSig = Callable[[int, int], str | None]
type Extend = Callable[[strip.Rows, Sequence[strip.Rows], Sequence[strip.Rows]], strip.Rows | None]

SEED_RANK: dict[SeedKind, int] = {"removed": 0, "strip": 1, "chained": 2, "gap": 3}  # a position seeded twice keeps
# its first kind here
HEIGHT_PX = 2.0  # a segment's height matches a span within this many px ...
HEIGHT_SHARE = 0.03  # ... or this share of the segment's height, whichever is more
INK_FLOOR = 12  # overlap extension: least luma distance from the background that is ink ...
INK_MAD = 4  # ... or this many MADs of the background, whichever is more
INK_CHROMA = 40  # chroma spread distance from the background that is ink
INK_WINDOW = 33  # px: column window of the local background (median)
INK_DILATE = 3  # px: the ink mask is dilated by this (glyphs of one logo join)
BG_ROWS = 32  # px: rows of each adjacent walkable run the background is taken from (nearest first)
LUMA = (0.299, 0.587, 0.114)  # RGB weights of luma


class Ranged(TypedDict):
    """A removed range: strip rows [start, end)."""

    start: ReadOnly[int]
    end: ReadOnly[int]


class SeedRange(Ranged):
    """A removed range as `seeds` reads it: why it was removed and the deciding decision's id (None: a rule)."""

    why: ReadOnly[str]
    decision: ReadOnly[int | None]


class UnitRange(Ranged):
    """A unit step 1 removed: its edge, its decision's id, its chain text (None: none) and decided_at (ISO text)."""

    where: ReadOnly[str]
    decision: ReadOnly[int]
    chain: ReadOnly[str | None]
    added: ReadOnly[str]


class CutRow(Protocol):
    """A cut decision's row (a `db.Row`): `id`, `chain`, `decided_at`, `credit` are read."""

    def __getitem__(self, key: str, /) -> object:
        """Return a column's value."""
        ...


class Entry(TypedDict):
    """1 version the matcher may apply: its decision, decided_at, side, version and, for a unit's chain, its unit."""

    decision: int
    added: str
    why: Literal["card", "cut"]
    side: Side
    version: chains.Version
    unit: strip.Rows | None  # the removed unit a unit's chain acts on (its edges only); None: a cut decision
    where: str | None  # the unit's edge (card); None: a cut decision (the seed's region)
    credit: bool


class Seed(TypedDict):
    """Where a chain may start: the side it extends to from pos, the edge region, the kind and the units it edges."""

    side: Side
    pos: int
    region: Region
    kind: SeedKind
    units: list[strip.Rows]


class Part(NamedTuple):
    """1 placed segment: its kind, strip rows [start, end) and the segment."""

    kind: Literal["cut", "keep", "edge"]
    start: int
    end: int
    seg: chains.Segment


class Added(TypedDict):
    """A range a match removes: rows, edge, the decision's id, why (card or cut) and whether it is a credit cut."""

    start: int
    end: int
    where: str
    decision: int
    why: Literal["card", "cut"]
    credit: bool


class Match(TypedDict):
    """1 match: its decision, matched span (anchor to far edge), the ranges it added, side, anchor, unit and region.

    region: its seed's edge region, which the chained seed at its far edge takes.
    """

    decision: int
    span: strip.Rows
    ranges: list[strip.Rows]
    side: Side
    anchor: int
    unit: strip.Rows | None
    region: Region


class Hole(TypedDict):
    """A match holding a keep segment (a card): its decision, matched span, its seed's region and its own ranges."""

    decision: int
    span: strip.Rows
    region: Region
    own: list[strip.Rows]


class Miss(TypedDict):
    """A chain that should apply and did not: why, the unit (a unit's chain) and the capped segment (cap)."""

    decision: int
    side: Side
    why: Literal["signature", "clamped", "cap"]
    unit: strip.Rows | None
    segment: strip.Rows | None


class Anchored(TypedDict):
    """What the matcher adds: ranges (sorted), the span of every match, the matches, the card holes, the misses."""

    added: list[Added]
    matched: list[strip.Rows]
    matches: list[Match]
    holes: list[Hole]
    misses: list[Miss]


# ---------------------------------------------------------------- rows


def uncovered(a: int, b: int, taken: Iterable[strip.Rows]) -> list[strip.Rows]:
    """Return the parts of [a, b) outside every (start, end) in taken, in order."""
    out: list[strip.Rows] = []
    pos = a
    for s, e in sorted(taken):
        if e <= pos or s >= b:
            continue
        if s > pos:
            out.append((pos, s))
        pos = max(pos, e)
    if pos < b:
        out.append((pos, b))
    return out


def hits(a: int, b: int, taken: Iterable[strip.Rows]) -> bool:
    """Return whether rows [a, b) overlap any (start, end) in taken."""
    return any(s < b and a < e for s, e in taken)


def merged(rows: Iterable[strip.Rows]) -> list[strip.Rows]:
    """Return rows merged where they overlap or touch, sorted."""
    out: list[strip.Rows] = []
    for s, e in sorted(rows):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _rows(r: Sequence[int]) -> strip.Rows:
    s, e = r
    return int(s), int(e)


class Layout:
    """A strip as the matcher walks it: height, width (px), spans (strip.spans) and gaps (walkable runs >= 1 gutter).

    The walkable runs are the rows between and around the spans (0-row ones included).
    """

    def __init__(self, height: int, width: int, spans: Sequence[Sequence[int]], gaps: Sequence[Sequence[int]]) -> None:
        """Hold the strip's height, width, spans and gaps, each sorted."""
        self.height, self.width = height, width
        self.spans = sorted(_rows(s) for s in spans)
        self.gaps = sorted(_rows(g) for g in gaps)
        self._starts = [s for s, _ in self.spans]
        self._ends = [e for _, e in self.spans]

    def runs(self) -> list[strip.Rows]:
        """[(start, end)] of the walkable runs between and around the spans, in order (0-row ones included)."""
        out: list[strip.Rows] = []
        pos = 0
        for s, e in self.spans:
            out.append((pos, s))
            pos = e
        return [*out, (pos, self.height)]

    def blank(self, a: int, b: int) -> bool:
        """Return whether rows [a, b) hold no span row."""
        i = bisect.bisect_right(self._ends, a)
        return i == len(self.spans) or self.spans[i][0] >= b

    def next_span(self, pos: int, side: Side) -> strip.Rows | None:
        """Return the span a segment takes from pos walking `side` (below: down, above: up), or None past the end.

        The next span after the walkable run at pos; a span pos lies inside is taken from pos on.
        """
        if side == "below":
            i = bisect.bisect_right(self._ends, pos)
            if i == len(self.spans):
                return None
            s, e = self.spans[i]
            return max(s, pos), e
        i = bisect.bisect_left(self._starts, pos) - 1
        if i < 0:
            return None
        s, e = self.spans[i]
        return s, min(e, pos)

    def neighbours(self, a: int, b: int) -> tuple[strip.Rows | None, strip.Rows | None]:
        """Return (the span above [a, b) or None, the span below or None): the nearest spans wholly outside it."""
        i = bisect.bisect_right(self._ends, a) - 1
        j = bisect.bisect_left(self._starts, b)
        return (self.spans[i] if i >= 0 else None), (self.spans[j] if j < len(self.spans) else None)

    def region(self, pos: int, edge_px: int) -> Region | None:
        """Return head / tail for a position in the first / last edge_px rows, else None.

        A short strip's position in both regions takes the half it lies in.
        """
        head, tail = pos <= edge_px, pos >= self.height - edge_px
        if head and tail:
            return "head" if 2 * pos < self.height else "tail"
        return "head" if head else "tail" if tail else None


class View(NamedTuple):
    """The strip as the matcher reads it: its layout, span signatures, overlap extension and edge region size.

    span_sig(a, b): the signature text of rows [a, b) (span_sigs); extend: the overlap extension (extender).
    """

    layout: Layout
    span_sig: SpanSig
    extend: Extend
    edge_px: int


def region_bounds(height: int, region: str, edge_px: int) -> strip.Rows:
    """Return the (start, end) rows of an edge region; ValueError for a region other than head or tail.

    Head = the first edge_px rows, tail = the last; in a strip shorter than 2 x edge_px each takes its half.
    """
    if region not in ("head", "tail"):
        msg = f"edge region {region!r}: head or tail"
        raise ValueError(msg)
    half = (height + 1) // 2 if height < 2 * edge_px else None
    if region == "head":
        return 0, min(edge_px, height) if half is None else half
    return max(0, height - edge_px) if half is None else half, height


# ---------------------------------------------------------------- signatures


def signable(im: Image.Image) -> str | None:
    """Return the signature text of an image, or None when it is uniform or unsignable."""
    if images.is_uniform(im):
        return None
    sig = images.signature(im)
    return None if images.unsignable(sig) else images.sig_to_str(sig)


def tiles_match(tiles: str, sig: str | None) -> bool:
    """Return whether a segment's tiles match a span's signature: same tile count, every tile within the threshold.

    No aspect test (the segment's height is tested instead); a span with no signature (uniform, unsignable) never
    matches.
    """
    if sig is None:
        return False
    a, b = images.sig_from_str(f"{tiles}|1.0")[0], images.sig_from_str(sig)[0]
    return len(a) == len(b) and all((x ^ y).bit_count() <= images.PHASH_THRESHOLD for x, y in zip(a, b, strict=True))


def span_sigs(render: Render) -> SpanSig:
    """Return span_sig(a, b): the signature text of strip rows [a, b) (signable), each rendered once."""

    @functools.cache
    def span_sig(a: int, b: int) -> str | None:
        return signable(render(a, b))

    return span_sig


# ---------------------------------------------------------------- pools


def cut_pool(cuts: Iterable[CutRow]) -> list[Entry]:
    """Return the entries of a series' cut decisions: every version of every row, tried at every seed."""
    out: list[Entry] = []
    for row in cuts:
        decision, stored, added, credit = row["id"], row["chain"], row["decided_at"], row["credit"]
        if not (type(decision) is int and isinstance(stored, str) and isinstance(added, str)):
            msg = f"cut decision {decision!r}: id, chain or decided_at missing or of the wrong type"
            raise ValueError(msg)
        if type(credit) is not bool:
            msg = f"cut decision {decision}: credit {credit!r} is not a bool"
            raise ValueError(msg)
        for side, v in chains.versions(chains.parse(stored)):
            entry: Entry = {
                "decision": decision,
                "added": added,
                "why": "cut",
                "side": side,
                "version": v,
                "unit": None,
                "where": None,
                "credit": credit,
            }
            out.append(entry)
    return out


def unit_pool(removed: Iterable[UnitRange]) -> list[Entry]:
    """Return the entries of the chains of the units step 1 removed: each tried at its own unit's edges only."""
    out: list[Entry] = []
    for r in removed:
        stored = r["chain"]
        if stored is None:
            continue
        for side, v in chains.versions(chains.parse(stored)):
            entry: Entry = {
                "decision": r["decision"],
                "added": r["added"],
                "why": "card",
                "side": side,
                "version": v,
                "unit": (r["start"], r["end"]),
                "where": r["where"],
                "credit": False,
            }
            out.append(entry)
    return out


# ---------------------------------------------------------------- seeds and the walk


def seeds(layout: Layout, removed: Iterable[SeedRange], edge_px: int) -> list[Seed]:
    """Return the seeds of a strip, by position.

    side: the direction a chain extends from pos; kind: strip (strip start: below, strip end: above), removed (a
    removed range's end: below, its start: above, in an edge region), gap (a gap's end: below, its start: above, in an
    edge region; never a mid-strip gap). No seed extends beyond a strip end. units: the removed units whose edge it is
    (ranges a decision removed as `match`: their chains are tried there only). A position seeded twice keeps the first
    kind of SEED_RANK (the gap rule holds only at a pure gap seed).
    """
    out: dict[tuple[Side, int], Seed] = {}

    def add(side: Side, pos: int, kind: SeedKind, region: Region | None, unit: strip.Rows | None = None) -> None:
        if region is None or (side == "below" and pos >= layout.height) or (side == "above" and pos <= 0):
            return
        s = out.setdefault((side, pos), {"side": side, "pos": pos, "region": region, "kind": kind, "units": []})
        if SEED_RANK[kind] < SEED_RANK[s["kind"]]:
            s["kind"] = kind
        if unit is not None and unit not in s["units"]:
            s["units"].append(unit)

    add("below", 0, "strip", "head")
    add("above", layout.height, "strip", "tail")
    for r in removed:
        unit = (r["start"], r["end"]) if r["why"] == "match" and r["decision"] is not None else None
        add("below", r["end"], "removed", layout.region(r["end"], edge_px), unit)
        add("above", r["start"], "removed", layout.region(r["start"], edge_px), unit)
    for s, e in layout.gaps:
        add("below", e, "gap", layout.region(e, edge_px))
        add("above", s, "gap", layout.region(s, edge_px))
    return sorted(out.values(), key=lambda s: (s["pos"], s["side"]))


def _exact(seg: chains.Segment) -> bool:
    return seg["kind"] != "edge" and seg.get("exact", False)


def _h_ok(n: int, seg: chains.Span, width: int) -> bool:
    want = seg["h"] * width
    return abs(n - want) <= max(HEIGHT_PX, HEIGHT_SHARE * want)


def exact_rows(sp: strip.Rows, seg: chains.Span, side: Side, width: int) -> strip.Rows | None:
    """Return the rows of an exact segment in the span sp the walk reaches, or None.

    Exactly round(h x W) rows from the span's near end (below: its start, above: its end); None unless its far edge
    lies inside the span.
    """
    n = round(seg["h"] * width)
    if n >= sp[1] - sp[0]:
        return None
    return (sp[0], sp[0] + n) if side == "below" else (sp[1] - n, sp[1])


def _place(
    layout: Layout, pos: int, side: Side, segs: Sequence[chains.Segment]
) -> list[Part] | Literal["clamped", "mismatch"]:
    """Return the placement of segs walked from pos, or why there is none.

    A cut / keep segment takes the next span when its height is h x W within max(2 px, 3 %), an exact one exactly
    round(h x W) rows of it from its near end (exact_rows); an edge segment every row to the strip end. clamped: the
    walk reached the strip end; mismatch: a span's height differed.
    """
    parts: list[Part] = []
    for seg in segs:
        if seg["kind"] == "edge":
            x, y = (pos, layout.height) if side == "below" else (0, pos)
            if y <= x:
                return "clamped"
            return [*parts, Part("edge", x, y, seg)]
        sp = layout.next_span(pos, side)
        if sp is None:
            return "clamped"
        if seg.get("exact", False):
            rows = exact_rows(sp, seg, side, layout.width)
        else:
            rows = sp if _h_ok(sp[1] - sp[0], seg, layout.width) else None
        if rows is None:
            return "mismatch"
        parts.append(Part(seg["kind"], rows[0], rows[1], seg))
        pos = rows[1] if side == "below" else rows[0]
    return parts


def _tiles_ok(part: Part, span_sig: SpanSig) -> bool:
    seg = part.seg
    return seg["kind"] == "edge" or tiles_match(seg["tiles"], span_sig(part.start, part.end))


def try_version(
    view: View, version: chains.Version, side: Side, anchor: int, taken: Sequence[strip.Rows]
) -> tuple[State, list[Part] | None]:
    """Return 1 version tried at an anchor: (state, parts).

    match (with its parts) when its cut / keep spans match their tiles and it takes a row not removed yet; covered when
    they match and its cut / edge rows are all removed already; else signature (a span differed), clamped (the walk
    reached the strip end with art left there) or uniform (it reached the strip end over blank or removed rows only).
    ValueError for an exact segment not followed by the non-exact rest of its span (junk.chains refuses it).
    """
    segs = version["segments"]
    for s, nxt in zip(segs, [*segs[1:], None], strict=True):
        if _exact(s) and (nxt is None or nxt["kind"] == "edge" or _exact(nxt)):
            msg = f"version {version!r}: an exact segment not followed by the non-exact rest of its span"
            raise ValueError(msg)
    layout = view.layout
    placed = _place(layout, anchor, side, segs)
    if isinstance(placed, list):
        if not all(_tiles_ok(p, view.span_sig) for p in placed):
            return "signature", None
        taking = [(p.start, p.end) for p in placed if p.kind != "keep"]
        return ("match", placed) if any(uncovered(x, y, taken) for x, y in taking) else ("covered", None)
    if placed == "mismatch":
        return "signature", None
    a, b = (anchor, layout.height) if side == "below" else (0, anchor)
    art = any(uncovered(max(s, a), min(e, b), taken) for s, e in layout.spans if s < b and e > a)
    return ("clamped" if art else "uniform"), None


def nominal(version: chains.Version, side: Side, anchor: int, layout: Layout) -> float:
    """Return a version's nominal length in px: heights (x W); an edge segment adds its anchor's distance to an end."""
    n = 0.0
    for s in version["segments"]:
        if s["kind"] == "edge":
            n += layout.height - anchor if side == "below" else anchor
        else:
            n += s["h"] * layout.width
    return n


def _gap_ok(version: chains.Version, width: int) -> bool:
    """Return the gap rule: at least 1 keep segment, or at least MIN_EDGE_BLOCK_PX of cut segments."""
    segs = version["segments"]
    return (
        any(s["kind"] == "keep" for s in segs)
        or sum(s["h"] * width for s in segs if s["kind"] == "cut") >= strip.MIN_EDGE_BLOCK_PX
    )


def _off_half(version: chains.Version, side: Side, anchor: int, height: int) -> bool:
    """Return whether an edge version is off its half: below needs anchor >= H / 2, above anchor <= H / 2."""
    if not any(s["kind"] == "edge" for s in version["segments"]):
        return False
    return 2 * anchor < height if side == "below" else 2 * anchor > height


# ---------------------------------------------------------------- the matcher


def anchored_ranges(view: View, removed: Iterable[Ranged], start: Iterable[Seed], pool: Sequence[Entry]) -> Anchored:
    """Return the ranges chains add to a strip at its seeds, repeated until no seed adds one.

    removed: the ranges removed so far; start: the seeds to begin with (seeds; step 5 passes those of the tail drops
    only); pool: the applicable versions. At each seed every applicable version (side, unit, the gap rule at a gap
    seed, an edge version's half) is tried longest nominal length first, then by decided_at, then decision id
    (try_version); the first match wins there: each cut segment is extended by `extend` ((start, end), blocked, taken
    -> (start, end), or None when the extension reaches a keep segment), at most by the segment's own height in rows
    neither removed nor in the match's other cut / edge segments (past either, the version fails there); its cut and
    edge rows (merged) not removed yet are added; its far edge is a chained seed (same side and region) and every added
    range's edges in an edge region are removed seeds.
    """
    layout = view.layout
    taken = [(r["start"], r["end"]) for r in removed]
    out: list[Added] = []
    holes: list[Hole] = []
    misses: list[Miss] = []
    matches: list[Match] = []
    done: set[tuple[Side, int]] = set()
    queue: list[Seed] = list(start)
    while queue:
        queue.sort(key=lambda s: (s["pos"], s["side"]))
        batch: list[Seed] = queue
        queue = []
        for sd in batch:
            if (sd["side"], sd["pos"]) in done:
                continue
            done.add((sd["side"], sd["pos"]))
            got = _at_seed(view, sd, pool, taken, misses)
            if got is None:
                continue
            e, parts = got
            where = _where(e, sd, parts)
            # 1 match's ranges merged first: extended cut segments may overlap (I Stole c0006: 2 notice lines joined
            # by ringing across their 8 px run both extend over both)
            rows = merged((p.start, p.end) for p in parts if p.kind != "keep")
            add: list[Added] = [
                {
                    "start": s,
                    "end": t,
                    "where": where,
                    "decision": e["decision"],
                    "why": e["why"],
                    "credit": e["credit"],
                }
                for x, y in rows
                for s, t in uncovered(x, y, taken)
            ]
            out += add
            taken += [(r["start"], r["end"]) for r in add]
            far = parts[-1].end if sd["side"] == "below" else parts[-1].start
            span = (min(sd["pos"], far), max(sd["pos"], far))
            own = [(r["start"], r["end"]) for r in add]
            matches.append(
                {
                    "decision": e["decision"],
                    "span": span,
                    "ranges": own,
                    "side": sd["side"],
                    "anchor": sd["pos"],
                    "unit": e["unit"],
                    "region": sd["region"],
                }
            )
            if any(p.kind == "keep" for p in parts):
                holes.append({"decision": e["decision"], "span": span, "region": sd["region"], "own": own})
            queue.append({"side": sd["side"], "pos": far, "region": sd["region"], "kind": "chained", "units": []})
            for r in add:
                ends: tuple[tuple[Side, int], ...] = (("below", r["end"]), ("above", r["start"]))
                for side, pos in ends:
                    region = layout.region(pos, view.edge_px)
                    if region is not None and 0 < pos < layout.height:
                        queue.append({"side": side, "pos": pos, "region": region, "kind": "removed", "units": []})
    return {
        "added": sorted(out, key=lambda r: r["start"]),
        "matched": [m["span"] for m in matches],
        "matches": matches,
        "holes": holes,
        "misses": misses,
    }


def _where(e: Entry, sd: Seed, parts: Sequence[Part]) -> str:
    """Return a match's edge: a card's edge version takes the edge's region, a card the unit's, a cut the seed's."""
    if e["why"] == "cut":
        return sd["region"]
    if parts[-1].kind == "edge":
        return "head" if sd["side"] == "above" else "tail"
    if e["where"] is None:
        msg = f"junk decision {e['decision']}'s card entry has no unit edge"
        raise ValueError(msg)
    return e["where"]


def _at_seed(
    view: View, sd: Seed, pool: Sequence[Entry], taken: Sequence[strip.Rows], misses: list[Miss]
) -> tuple[Entry, list[Part]] | None:
    """Return the winning (pool entry, parts as extended) at 1 seed, or None; append the misses there.

    A unit's chain tried here and failed is a miss even when another chain wins at this seed (only its own decision and
    unit's win settles it); covered or uniform alone is no miss; an extension past its cap is a miss for a cut decision
    too (never a silent keep); a cut decision misses by nothing else.
    """
    layout, side, pos = view.layout, sd["side"], sd["pos"]
    cands = [e for e in pool if e["side"] == side and (e["unit"] is None or e["unit"] in sd["units"])]
    cands.sort(key=lambda e: (-nominal(e["version"], side, pos, layout), e["added"], e["decision"]))
    states: dict[tuple[int, strip.Rows | None], list[State]] = {}
    caps: dict[tuple[int, strip.Rows | None], strip.Rows] = {}
    win: tuple[Entry, list[Part]] | None = None
    for e in cands:
        v, key = e["version"], (e["decision"], e["unit"])
        state: State
        if _off_half(v, side, pos, layout.height):
            state = "off"
        elif sd["kind"] == "gap" and not _gap_ok(v, layout.width):
            state = "gap"
        else:
            state, parts = try_version(view, v, side, pos, taken)
            if parts is not None:
                grown, capped = _extended(parts, taken, view.extend)
                if grown is not None:
                    win = (e, grown)
                    break
                state = "signature"
                if capped is not None:
                    caps.setdefault(key, capped)
        states.setdefault(key, []).append(state)
    won = (win[0]["decision"], win[0]["unit"]) if win is not None else None
    misses += _misses(side, {k: st for k, st in states.items() if k != won}, caps)
    return win


def _misses(
    side: Side,
    states: dict[tuple[int, strip.Rows | None], list[State]],
    caps: dict[tuple[int, strip.Rows | None], strip.Rows],
) -> list[Miss]:
    """Return the misses at 1 seed of the (decision, unit) pairs tried there that did not win, by their states."""
    out: list[Miss] = []
    for key, st in states.items():
        decision, unit = key
        if all(s == "off" for s in st):
            continue
        if key in caps:
            out.append({"decision": decision, "side": side, "why": "cap", "unit": unit, "segment": caps[key]})
            continue
        if unit is None or ("signature" not in st and ("covered" in st or "uniform" in st)):
            continue
        why = "signature" if "signature" in st else "clamped"
        out.append({"decision": decision, "side": side, "why": why, "unit": unit, "segment": None})
    return out


def _extended(
    parts: Sequence[Part], taken: Sequence[strip.Rows], extend: Extend
) -> tuple[list[Part] | None, strip.Rows | None]:
    """Return (parts with every cut segment overlap-extended, None), (None, None) or (None, the capped segment).

    (None, None) when an extension reaches a keep segment; (None, (start, end) of the segment) when it adds more than
    the segment's own height (the cap: rows already removed or in the match's own cut / edge segments do not count; I
    Stole c0006: a notice line bridged to a tall panel by an 8 px run). An exact segment is never extended. A span an
    exact segment splits (the exact piece and the next segment, the rest of the span) is reached by an extension
    through its piece facing the extended segment: a cut piece counts as removed (no extension grows into that span),
    a keep piece is blocked like any keep. Either piece of a split span never extends across the other (glued).
    """
    blocked = [(p.start, p.end) for p in parts if p.kind == "keep"]
    own = [(p.start, p.end) for p in parts if p.kind != "keep"]
    pairs = [(p, parts[i + 1]) for i, p in enumerate(parts) if _exact(p.seg)]
    out: list[Part] = []
    for p in parts:
        if p.kind != "cut" or _exact(p.seg):
            out.append(p)
            continue
        x, y = p.start, p.end
        stop: list[strip.Rows] = []
        for pair in pairs:
            rows = [(q.start, q.end) for q in pair]
            if (x, y) in rows:  # this segment's own split span: its glued partner
                stop += [r for r in rows if r != (x, y)]
                continue
            above = max(b for _, b in rows) <= x  # the split span lies above this segment
            facing = max(pair, key=lambda q: q.end) if above else min(pair, key=lambda q: q.start)
            if facing.kind == "cut":
                stop.append((facing.start, facing.end))
        got = extend((x, y), blocked, [*taken, *stop])
        if got is None:
            return None, None
        if sum(e - s for s, e in uncovered(got[0], got[1], [*taken, *own])) > y - x:
            return None, (x, y)
        out.append(Part(p.kind, got[0], got[1], p.seg))
    return out, None


# ---------------------------------------------------------------- overlap extension


def extender(layout: Layout, render: Render) -> Extend:
    """Return extend(segment, blocked, taken): overlap_extend on this strip."""

    def extend(seg: strip.Rows, blocked: Sequence[strip.Rows], taken: Sequence[strip.Rows]) -> strip.Rows | None:
        return overlap_extend(layout, seg, blocked, taken, render)

    return extend


def overlap_extend(
    layout: Layout, seg: strip.Rows, blocked: Sequence[strip.Rows], taken: Sequence[strip.Rows], render: Render
) -> strip.Rows | None:
    """Return a cut segment extended over every span a faint object sharing its rows touches, or None when blocked.

    None: the extension reaches a blocked range (a keep segment of the match). Ink is measured at strip width over the
    segment and the walkable run and span next to it on each side: pixels more than max(INK_FLOOR, INK_MAD x MAD) luma,
    or INK_CHROMA chroma, from the local background (median of the adjacent walkable runs' pixels in an INK_WINDOW
    column window), dilated by INK_DILATE px; an 8-connected component with pixels inside the segment's rows and in a
    neighbouring span extends the segment to that span; repeated until stable. A side is never extended past a strip
    end, a removed range (flush or in its run / span) or a 0-row walkable run (no background).
    """
    a, b = seg
    while True:
        up, down = layout.neighbours(a, b)
        run_up = (up[1] if up else 0, a)
        run_dn = (b, down[0] if down else layout.height)
        up_ok = up is not None and run_up[1] > run_up[0] and not hits(up[0], a, taken)
        dn_ok = down is not None and run_dn[1] > run_dn[0] and not hits(b, down[1], taken)
        # the background: at most BG_ROWS rows of each adjacent walkable run, next to the segment
        bg = [
            r
            for r in ((max(run_up[0], a - BG_ROWS), a), (b, min(run_dn[1], b + BG_ROWS)))
            if r[1] > r[0] and not hits(*r, taken)
        ]
        if not bg or not (up_ok or dn_ok):
            return a, b
        # a component touching a neighbouring span crosses that span's near rows first: those rows (and the dilation
        # margin) are all that is rendered of it
        margin = INK_DILATE + 1
        tops = [x for x, _ in bg] + [a]
        bottoms = [y for _, y in bg] + [b]
        if up is not None and up_ok:
            tops.append(max(up[0], up[1] - margin))
        if down is not None and dn_ok:
            bottoms.append(min(down[1], down[0] + margin))
        top, bottom = min(tops), max(bottoms)
        rgb = np.asarray(render(top, bottom).convert("RGB"), np.float32)
        reach = _reached(rgb, [(x - top, y - top) for x, y in bg], a - top, b - top)
        grow_up = up is not None and up_ok and bool(reach[: up[1] - top].any())
        grow_dn = down is not None and dn_ok and bool(reach[down[0] - top :].any())
        if not (grow_up or grow_dn):
            return a, b
        if (up is not None and grow_up and hits(*up, blocked)) or (
            down is not None and grow_dn and hits(*down, blocked)
        ):
            return None
        a = up[0] if up is not None and grow_up else a
        b = down[1] if down is not None and grow_dn else b


def _dilate(m: NDArray[np.bool_], r: int) -> NDArray[np.bool_]:
    """Return m (2-D bool) dilated by r px (a (2r + 1)-square)."""
    p = np.pad(m, ((r, r), (0, 0)))
    rows = sliding_window_view(p, 2 * r + 1, axis=0).any(axis=-1)
    p = np.pad(rows, ((0, 0), (r, r)))
    return sliding_window_view(p, 2 * r + 1, axis=1).any(axis=-1)


def _local(col: NDArray[np.floating]) -> NDArray[np.floating]:
    """Return the median of every INK_WINDOW column window of col (edges repeated)."""
    p = np.pad(col, INK_WINDOW // 2, mode="edge")
    return np.median(sliding_window_view(p, INK_WINDOW), axis=-1)


def _reached(rgb: NDArray[np.float32], bg_rows: Sequence[strip.Rows], a: int, b: int) -> NDArray[np.bool_]:
    """Return per row of rgb whether an ink component (overlap_extend) touching rows [a, b) has a pixel there."""
    luma = rgb @ np.array(LUMA, np.float32)
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    bl = np.concatenate([luma[x:y] for x, y in bg_rows])
    bc = np.concatenate([chroma[x:y] for x, y in bg_rows])
    col_l, col_c = _local(np.median(bl, axis=0)), _local(np.median(bc, axis=0))
    mad = float(np.median(np.abs(bl - np.median(bl))))
    ink = (np.abs(luma - col_l) > max(INK_FLOOR, INK_MAD * mad)) | (np.abs(chroma - col_c) > INK_CHROMA)
    mask = _dilate(ink, INK_DILATE)
    cur = np.zeros_like(mask)
    cur[a:b] = mask[a:b]
    while True:
        nxt = _dilate(cur, 1) & mask
        if np.array_equal(nxt, cur):
            return cur.any(axis=1)
        cur = nxt


# ---------------------------------------------------------------- card holes


def card_holes(height: int, removed: Iterable[Ranged], holes: Iterable[Hole], edge_px: int) -> list[strip.Rows]:
    """Return the kept rows of every card hole, sorted.

    Per card match (anchored_ranges' holes), the maximal run holding its matched span bounded by removed ranges other
    than its own cut segments, by the strip ends, or by the boundary of the edge region its seed lies in (a hole never
    leaves its edge region; a matched span reaching inward of it stays whole), without its removed rows. A hole only
    shields its rows from the tail rules; it never suppresses a candidate.
    """
    every = [(r["start"], r["end"]) for r in removed]
    out: list[strip.Rows] = []
    for h in holes:
        lo, hi = h["span"]
        r_lo, r_hi = region_bounds(height, h["region"], edge_px)
        others = [r for r in every if r not in h["own"]]
        a = max([e for _, e in others if e <= lo] + [min(r_lo, lo)])
        b = min([s for s, _ in others if s >= hi] + [max(r_hi, hi)])
        out += uncovered(a, b, every)
    return sorted(set(out))
