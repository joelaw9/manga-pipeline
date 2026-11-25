"""The matcher of chains and cut decisions (SPEC "Junk decisions": chains and cuts; manga_pipeline.junk.anchored).

Seeds, the walk, versions, misses, overlap extension and its cap, card holes and the pools, on synthetic layouts and
numpy strips and on the real end cards of tests/fixtures_pages/junk/<case>/ (strip.png row 0 = the strip row
scan.json's crop starts at). Ported from the bulk converter's tests/test_anchored.py with its expected values; a
synthetic span's signature is its own tiles text (tile: far apart in Hamming distance, so only equal tiles match).
"""

import hashlib
import json
from pathlib import Path
from typing import TypedDict

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import images, strip
from manga_pipeline.junk import anchored as an
from manga_pipeline.junk import chains
from manga_pipeline.junk import strip_units as su

from tests.builders import db_row, present
from tests.timing import budget

FIX = Path(__file__).parent / "fixtures_pages" / "junk"
EDGE = su.EDGE_PX
ADDED = "2026-11-05T10:00:00+00:00"
SHA = "a" * 64


# ---------------------------------------------------------------- synthetic layouts (no pixels)


def tile(n):
    return hashlib.sha256(str(n).encode()).hexdigest()[:16]


def sig(n):
    """Return a synthetic span signature of tiles tile(n)."""
    return f"{tile(n)}|1.0"


def sigs_of(spans):
    return {(s, e): f"{tile(s)}|{(e - s) / 1000!r}" for s, e in spans}


def layout(height, spans, width=1000):
    """Return a Layout whose gaps are the walkable runs of at least 1 gutter between and around the spans."""
    lay = an.Layout(height, width, spans, [])
    return an.Layout(height, width, spans, [r for r in lay.runs() if r[1] - r[0] >= su.gutter(width)])


def seg(kind, s=0, e=0, width=1000):
    if kind == "edge":
        return {"kind": "edge"}
    return {"kind": kind, "h": round((e - s) / width, 4), "tiles": tile(s)}


def exact(kind, s, e, width=1000):
    return {**seg(kind, s, e, width), "exact": True}


def raw_version(*segs, src=True):
    """Return a version as a chain's JSON holds it."""
    return {
        "segments": list(segs),
        "ref": [["S", "c.cbz", 0]],
        "src": {"file": "c.cbz", "sha256": SHA} if src else None,
    }


def version(*segs, src=True) -> chains.Version:
    """Return a version as the matcher reads it."""
    v = raw_version(*segs, src=src)
    return {"segments": v["segments"], "ref": v["ref"], "src": v["src"]}


def entry(decision, side, v, card=None, *, credit=False) -> an.Entry:
    """Return a pool entry: a cut decision's, or a card's when card = (its unit, the unit's edge)."""
    unit, where = (None, None) if card is None else card
    return {
        "decision": decision,
        "added": ADDED,
        "why": "cut" if card is None else "card",
        "side": side,
        "version": v,
        "unit": unit,
        "where": where,
        "credit": credit,
    }


class Band(TypedDict):
    """A removed range as the matcher reads it."""

    start: int
    end: int
    where: str
    decision: int | None
    why: str


def band(start, end, jid=1, where="tail", why="match") -> Band:
    return {"start": start, "end": end, "where": where, "decision": jid, "why": why}


def keep_extent(s, _blocked, _taken):
    return s


def run(lay, removed, pool, sigs, extend=keep_extent):
    view = an.View(lay, lambda a, b: sigs.get((a, b)), extend, EDGE)
    return an.anchored_ranges(view, removed, an.seeds(lay, removed, EDGE), pool)


def ranges(got):
    return [(r["start"], r["end"], r["decision"], r["why"]) for r in got["added"]]


def no_unit(got):
    """Return the misses of an anchored_ranges result without their unit."""
    return [{k: v for k, v in m.items() if k != "unit"} for m in got["misses"]]


def miss(decision, side, why, segment=None):
    return {"decision": decision, "side": side, "why": why, "segment": segment}


def as_drops(rs):
    """Return ranges as strip_units' Drop ranges (what its join rule reads)."""
    out: list[su.Drop] = [
        {
            "start": r["start"],
            "end": r["end"],
            "where": r["where"],
            "decision": r["decision"],
            "why": r["why"],
            "sig": None,
        }
        for r in rs
    ]
    return out


# ---------------------------------------------------------------- the rows (Layout, regions)


@budget(load="pure")
def test_a_short_strips_position_in_both_regions_takes_its_half():
    short = an.Layout(5000, 720, [], [])
    assert [short.region(p, EDGE) for p in (2000, 2499, 2500, 3000)] == ["head", "head", "tail", "tail"], (
        "a position in both edge regions of a short strip takes the half it lies in"
    )
    assert an.Layout(20000, 720, [], []).region(10000, EDGE) is None, "a mid-strip position is in no edge region"
    assert an.region_bounds(5000, "head", EDGE) == (0, 2500), "a short strip's head is its first half"
    assert an.region_bounds(5001, "tail", EDGE) == (2501, 5001), "a short strip's tail is its last half"
    assert an.region_bounds(20000, "tail", EDGE) == (20000 - EDGE, 20000), "the tail is the last edge_px rows"


@budget(load="pure")
def test_an_edge_region_is_head_or_tail():
    with pytest.raises(ValueError, match="head or tail"):
        an.region_bounds(20000, "middle", EDGE)


@budget(load="pure")
def test_the_walk_takes_the_next_span_and_neighbours_lie_wholly_outside():
    lay = an.Layout(1000, 100, [(100, 200), (300, 400)], [])
    assert (lay.next_span(0, "below"), lay.next_span(150, "below"), lay.next_span(400, "below")) == (
        (100, 200),
        (150, 200),
        None,
    ), "below: the next span after the run at pos, a span pos lies inside taken from pos on, None past the end"
    assert (lay.next_span(1000, "above"), lay.next_span(350, "above"), lay.next_span(100, "above")) == (
        (300, 400),
        (300, 350),
        None,
    ), "above: likewise walking up"
    assert lay.neighbours(200, 300) == ((100, 200), (300, 400)), "the nearest spans wholly outside the rows"
    assert (lay.blank(200, 300), lay.blank(150, 160)) == (True, False), "blank: rows holding no span row"


# ---------------------------------------------------------------- seeds


@budget(load="pure")
def test_gaps_in_the_middle_of_the_strip_are_never_seeds():
    lay = layout(20000, [(0, 2990), (3020, 9990), (10020, 16990), (17020, 20000)])
    got = {(s["side"], s["pos"], s["kind"], s["region"]) for s in an.seeds(lay, [], EDGE)}
    assert got == {
        ("below", 0, "strip", "head"),
        ("above", 20000, "strip", "tail"),
        ("above", 2990, "gap", "head"),
        ("below", 3020, "gap", "head"),
        ("above", 16990, "gap", "tail"),
        ("below", 17020, "gap", "tail"),
    }, "the gap at 10,000 lies in neither edge region: no seed"


@budget(load="pure")
def test_removed_ranges_seed_and_their_units_are_named():
    lay = layout(20000, [(0, 19000), (19100, 19500), (19600, 20000)])
    at = {(s["side"], s["pos"]): s for s in an.seeds(lay, [band(19100, 19500, jid=7)], EDGE)}
    assert at[("above", 19100)]["kind"] == "removed", "a removed range's start seeds above"
    assert at[("above", 19100)]["units"] == [(19100, 19500)], "a removed unit's edges carry it (its row's chain)"
    assert at[("below", 19500)]["units"] == [(19100, 19500)], "a removed unit's edges carry it (its row's chain)"


# ---------------------------------------------------------------- matching


@budget(load="pure")
def test_longest_version_first_across_rows_whatever_the_recording_order():
    spans = [(0, 18000), (18100, 18160), (18170, 18260), (19000, 20000)]
    lay = layout(20000, spans)
    short = entry(1, "above", version(seg("cut", 18170, 18260)))
    short["added"] = "2026-11-01T00:00:00+00:00"  # the shorter row is the older one
    long_ = entry(2, "above", version(seg("cut", 18170, 18260), seg("cut", 18100, 18160)))
    got = run(lay, [band(19000, 20000)], [short, long_], sigs_of(spans))
    assert ranges(got) == [(18100, 18160, 2, "cut"), (18170, 18260, 2, "cut")], "the longest version wins"
    again = run(lay, [band(19000, 20000)], [long_, short], sigs_of(spans))
    assert ranges(again) == ranges(got), "pool order changes nothing (deterministic)"


@budget(load="pure")
def test_the_gap_rule_holds_only_at_a_pure_gap_seed():
    spans = [(0, 3000), (3050, 3070), (3100, 20000)]
    lay = layout(20000, spans)
    line = entry(1, "above", version(seg("cut", 3050, 3070)))  # 20 px, no keep: below MIN_EDGE_BLOCK_PX
    assert ranges(run(lay, [], [line], sigs_of(spans))) == [], "at the gap seed 3100 it is not tried"
    assert ranges(run(lay, [band(3100, 3500, where="head")], [line], sigs_of(spans))) == [(3050, 3070, 1, "cut")], (
        "a removed range at the same rows seeds it"
    )
    kept = entry(2, "above", version(seg("keep", 3050, 3070), seg("cut", 0, 3000)))
    assert ranges(run(lay, [], [kept], sigs_of(spans)))[0][:2] == (0, 3000), "a keep segment passes the gap rule"


@budget(load="pure")
def test_a_chain_matches_from_its_anchor_and_chains_from_its_far_edge():
    spans = [(0, 18000), (18200, 18300), (18400, 18500), (19000, 20000)]
    lay = layout(20000, spans)
    pool = [entry(1, "above", version(seg("cut", 18400, 18500))), entry(2, "above", version(seg("cut", 18200, 18300)))]
    got = run(lay, [band(19000, 20000)], pool, sigs_of(spans))
    assert ranges(got) == [(18200, 18300, 2, "cut"), (18400, 18500, 1, "cut")], "the 2nd card chains from the 1st"
    assert all(r["where"] == "tail" and not r["credit"] for r in got["added"]), "a cut takes its seed's region"
    assert all(set(r) == {"start", "end", "where", "decision", "why", "credit"} for r in got["added"]), (
        "an added range carries no signature"
    )


@budget(load="pure")
def test_a_credit_cut_marks_its_ranges_credit_and_a_card_never():
    spans = [(0, 18000), (18400, 18500), (19000, 19500), (19600, 19700)]
    lay = layout(20000, spans)
    cut = entry(1, "above", version(seg("cut", 18400, 18500)), credit=True)
    card = entry(2, "below", version(seg("cut", 19600, 19700), src=False), card=((19000, 19500), "tail"))
    got = run(lay, [band(19000, 19500, jid=2)], [cut, card], sigs_of(spans))
    assert [(r["decision"], r["why"], r["credit"]) for r in got["added"]] == [(1, "cut", True), (2, "card", False)], (
        "a credit cut's ranges carry credit; a card range never does"
    )


@budget(load="pure")
def test_heights_within_2_px_or_3_percent_match_and_tiles_must_match():
    spans = [(0, 18000), (18402, 18500), (19000, 20000)]
    lay = layout(20000, spans)
    v = {"kind": "cut", "h": 0.1, "tiles": tile(18402)}  # 100 px: 98 px is within 3 px
    assert ranges(run(lay, [band(19000, 20000)], [entry(1, "above", version(v))], sigs_of(spans))), (
        "98 px matches a 100 px segment (within 3 %)"
    )
    other = {**v, "tiles": tile(1)}
    assert not ranges(run(lay, [band(19000, 20000)], [entry(1, "above", version(other))], sigs_of(spans))), (
        "other tiles never match"
    )


@budget(load="pure")
def test_a_version_wholly_removed_already_is_not_tried():
    spans = [(0, 18000), (18400, 18500), (19000, 20000)]
    lay = layout(20000, spans)
    got = run(
        lay,
        [band(18400, 18500, jid=5), band(19000, 20000)],
        [entry(1, "above", version(seg("cut", 18400, 18500)))],
        sigs_of(spans),
    )
    assert (ranges(got), got["matched"]) == ([], []), "a version whose rows are removed already takes nothing"


@budget(load="pure")
def test_an_edge_version_off_its_half_cuts_nothing_and_misses_nothing():
    spans = [(0, 300), (400, 600), (700, 19000), (19100, 19300), (19400, 20000)]
    lay = layout(20000, spans)
    v = version(seg("edge"), src=False)
    head = band(400, 600, jid=3, where="head")
    got = run(lay, [head], [entry(3, "below", v, card=((400, 600), "head"))], sigs_of(spans))
    assert (ranges(got), got["misses"]) == ([], []), "extending toward the far half would delete the chapter"
    tail = band(19100, 19300, jid=3)
    got = run(lay, [tail], [entry(3, "below", v, card=((19100, 19300), "tail"))], sigs_of(spans))
    assert ranges(got) == [(19300, 20000, 3, "card")], "an edge version in its half takes every row to the end"
    assert got["added"][0]["where"] == "tail", "a below edge card is a tail range"


@budget(load="pure")
def test_a_unit_row_chain_acts_only_on_its_own_units_and_misses_by_signature():
    spans = [(0, 18000), (18400, 18500), (18600, 18700), (19000, 20000)]
    lay = layout(20000, spans)
    v = version(seg("cut", 18600, 18700), src=False)
    got = run(
        lay,
        [band(19000, 20000, jid=9)],
        [entry(3, "above", v, card=((18400, 18500), "tail"))],
        sigs_of(spans),
    )
    assert ranges(got) == [], "row 3's chain is tried on its own unit's edges only (18400-18500 is not removed)"
    lay2 = layout(20000, [(0, 18000), (18400, 18500), (18600, 18700), (18700, 18750), (19000, 20000)])
    wrong = version({"kind": "cut", "h": 0.1, "tiles": tile(1)}, src=False)
    got = run(
        lay2,
        [band(18700, 18750, jid=3)],
        [entry(3, "above", wrong, card=((18700, 18750), "tail"))],
        sigs_of(lay2.spans),
    )
    assert no_unit(got) == [miss(3, "above", "signature")], "a unit's chain that differs there is a miss"


@budget(load="pure")
def test_a_unit_chain_clamped_at_the_strip_end_misses_unless_the_rest_is_blank():
    spans = [(0, 300), (400, 600), (19100, 19300)]
    lay = layout(20000, spans)
    v = version(seg("cut", 400, 600), seg("cut", 0, 300), src=False)
    unit = band(19100, 19300, jid=3)
    pool = [entry(3, "below", v, card=((19100, 19300), "tail"))]
    got = run(lay, [unit], pool, sigs_of(spans))
    assert (ranges(got), got["misses"]) == ([], []), "only blank rows below the unit: nothing to cut"
    lay = layout(20000, [*spans, (19500, 19600)])
    got = run(lay, [unit], pool, sigs_of(lay.spans))
    assert no_unit(got) == [miss(3, "below", "signature")], "art below the unit that differs: a miss"


@budget(load="pure")
def test_5_1_a_range_between_two_removed_ranges_leaves_at_most_one_gutter():
    # metadata-only: I Married the Male Lead's Dad c0147, rows 2400-3100: a cut anchored at the lower band's start;
    # its far end meets the upper band, and the join rule removes the blank run there
    spans = [(0, 2620), (2624, 2696), (2699, 2850), (2855, 2947), (2960, 118754)]
    lay = layout(118754, spans, width=720)
    removed = [band(2624, 2696, jid=10161, where="head"), band(2855, 2947, jid=10162, where="head")]
    v = {
        "segments": [{"kind": "cut", "h": round(151 / 720, 4), "tiles": tile(2699)}],
        "ref": [("S", "c", 0)],
        "src": {"file": "Chapter 106.cbz", "sha256": SHA},
    }
    got = run(lay, removed, [entry(10648, "above", v)], sigs_of(spans))
    assert ranges(got) == [(2699, 2850, 10648, "cut")], "the notice between the 2 bands is cut"
    joined = su.join_rule(lay, as_drops(removed + got["added"]), su.gutter(720))
    assert [(r["start"], r["end"], r["why"]) for r in joined] == [
        (2620, 2624, "join"),
        (2696, 2699, "join"),
        (2850, 2851, "join"),
        (2952, 2960, "join"),
    ], "C = 25 px >= G (9 px): 16 px go, no residual row at 2696"


@budget(load="pure")
def test_step_5_a_cut_row_anchors_on_a_tail_drop():
    spans = [(0, 18000), (18300, 18340), (18500, 18600), (19000, 20000)]
    lay = layout(20000, spans)
    drop: Band = {"start": 18400, "end": 18500, "where": "tail", "decision": None, "why": "blank"}
    start = [s for s in an.seeds(lay, [drop], EDGE) if s["kind"] == "removed"]
    view = an.View(lay, lambda a, b: sigs_of(spans).get((a, b)), keep_extent, EDGE)
    pool = [entry(1, "above", version(seg("cut", 18300, 18340)))]
    got = an.anchored_ranges(view, [band(19000, 20000), drop], start, pool)
    assert ranges(got) == [(18300, 18340, 1, "cut")], "a cut anchors on a tail drop's edge"


@budget(load="pure")
def test_a_cut_seeded_above_a_head_band_is_where_head():
    spans = [(0, 100), (200, 300), (400, 20000)]
    lay = layout(20000, spans)
    got = run(
        lay, [band(200, 300, jid=4, where="head")], [entry(5, "above", version(seg("cut", 0, 100)))], sigs_of(spans)
    )
    assert [(r["start"], r["end"], r["where"]) for r in got["added"]] == [(0, 100, "head")], (
        "a cut takes its seed's region"
    )


# ---------------------------------------------------------------- misses


@budget(load="pure")
def test_a_covered_shorter_version_never_silences_a_signature_miss():
    spans = [(0, 18000), (18400, 18500), (18600, 18700), (19000, 19200)]
    lay = layout(20000, spans)
    removed = [band(19000, 19200, jid=3), band(18600, 18700, jid=9)]
    long_ = version(seg("cut", 18600, 18700), {"kind": "cut", "h": 0.1, "tiles": tile(1)}, src=False)
    short = version(seg("cut", 18600, 18700), src=False)
    pool = [entry(3, "above", v, card=((19000, 19200), "tail")) for v in (long_, short)]
    got = run(lay, removed, pool, sigs_of(spans))
    assert no_unit(got) == [miss(3, "above", "signature")], "a long version's signature miss stands"


@budget(load="pure")
def test_a_uniform_clamp_never_silences_a_signature_miss():
    spans = [(0, 18000), (19000, 19200), (19300, 19400)]
    lay = layout(20000, spans)
    long_ = version({"kind": "cut", "h": 0.05, "tiles": tile(1)}, src=False)
    short = version(seg("cut", 19300, 19400), seg("cut", 19300, 19400), src=False)
    taken = [(19000, 19200), (19300, 19400)]
    view = an.View(lay, lambda _a, _b: None, keep_extent, EDGE)
    assert an.try_version(view, short, "below", 19200, taken)[0] == "uniform", (
        "a walk to the strip end over removed and blank rows only is uniform"
    )
    removed = [band(19000, 19200, jid=3), band(19300, 19400, jid=9)]
    pool = [entry(3, "below", v, card=((19000, 19200), "tail")) for v in (long_, short)]
    got = run(lay, removed, pool, sigs_of(spans))
    assert no_unit(got) == [miss(3, "below", "signature")], "the signature miss stands beside a uniform clamp"


@budget(load="pure")
def test_a_unit_chain_miss_is_recorded_when_another_row_wins_at_its_seed():
    spans = [(0, 18000), (18300, 18400), (18600, 18700), (18705, 19200)]
    lay = layout(20000, spans)
    chain_v = version({"kind": "cut", "h": 0.1, "tiles": tile(1)}, seg("cut", 18300, 18400), src=False)
    pool = [
        entry(3, "above", chain_v, card=((18705, 19200), "tail")),
        entry(5, "above", version(seg("cut", 18600, 18700))),
    ]
    got = run(lay, [band(18705, 19200, jid=3)], pool, sigs_of(spans))
    assert ranges(got) == [(18600, 18700, 5, "cut")], "the cut decision wins at the seed"
    assert no_unit(got) == [miss(3, "above", "signature")], "the unit's chain that failed there is still a miss"


@budget(load="pure")
def test_a_failed_chain_on_a_second_unit_of_the_winning_row_is_still_a_miss():
    spans = [(0, 18000), (18300, 18400), (18600, 18700), (18705, 19200)]
    lay = layout(20000, spans)
    units = [band(18705, 19200, jid=3), band(18705, 19000, jid=3)]
    long_ = version({"kind": "cut", "h": 0.1, "tiles": tile(1)}, seg("cut", 18300, 18400), src=False)
    short = version(seg("cut", 18600, 18700), src=False)
    pool = [entry(3, "above", v, card=(u, "tail")) for u in ((18705, 19200), (18705, 19000)) for v in (long_, short)]
    got = run(lay, units, pool, sigs_of(spans))
    assert ranges(got) == [(18600, 18700, 3, "card")], "the chain on the first unit wins"
    assert no_unit(got) == [miss(3, "above", "signature")], "misses compare (decision, unit), not the decision alone"


# ---------------------------------------------------------------- exact segments


@budget(load="pure")
def test_an_exact_segment_takes_exactly_its_rows_inside_a_span():
    lay = layout(10000, [(0, 9800)])
    v = version(exact("cut", 9500, 9800), seg("keep", 0, 9500))
    got = run(lay, [], [entry(7, "above", v)], {(9500, 9800): sig(9500), (0, 9500): sig(0)})
    assert ranges(got) == [(9500, 9800, 7, "cut")], "the exact cut takes its 300 rows of the glued span"
    assert got["matches"][0]["span"] == (0, 9800), "the match's far edge is the span boundary"


@budget(load="pure")
def test_the_rest_of_a_split_span_is_verified_with_the_height_tolerance():
    lay = layout(10000, [(500, 9800)])
    v = version(exact("cut", 9500, 9800), seg("keep", 0, 9500))
    assert ranges(run(lay, [], [entry(7, "above", v)], {(9500, 9800): sig(9500), (500, 9500): sig(0)})) == [], (
        "the span's rest 500 px shorter than named fails"
    )


@budget(load="pure")
def test_the_rest_of_a_split_span_of_the_right_height_but_other_tiles_misses():
    lay = layout(10000, [(0, 9800)])
    v = version(exact("cut", 9500, 9800), seg("keep", 0, 9500))
    sigs = {(9500, 9800): sig(9500), (0, 9500): "ffff000000000000|9.5"}
    assert ranges(run(lay, [], [entry(7, "above", v)], sigs)) == [], "the rest of the span is tile-verified"


@budget(load="pure")
def test_the_matcher_refuses_a_version_whose_exact_segment_is_outermost():
    lay = layout(10000, [(0, 9800)])
    view = an.View(lay, lambda _a, _b: tile(9500), keep_extent, EDGE)
    with pytest.raises(ValueError, match="exact segment not followed"):
        an.try_version(view, version(exact("cut", 9500, 9800)), "above", 10000, [])


@budget(load="pure")
def test_an_exact_keep_never_leaves_an_interior_chained_seed():
    lay = layout(20000, [(0, 600), (602, 952), (1000, 20000)])
    v = version(seg("cut", 602, 952), exact("keep", 507, 600), seg("keep", 0, 507))
    got = run(lay, [], [entry(7, "above", v)], {(602, 952): sig(602), (507, 600): sig(507), (0, 507): sig(0)})
    assert ranges(got) == [(602, 952, 7, "cut")], "the title is cut, the card kept"
    far = got["matches"][0]["span"][0]
    assert far == 0, "the chained seed lies at the span boundary"
    assert any(far == s for s, _ in lay.spans), "the chained seed lies at the span boundary"


@budget(load="pure")
@pytest.mark.parametrize(
    ("sigs", "spans"),
    [
        ({(9500, 9800): "ffff000000000000|0.3"}, [(0, 9800)]),
        ({}, [(0, 8000), (9700, 9800)]),
        ({(9500, 9800): sig(9500)}, [(0, 8000), (9500, 9800)]),
    ],
    ids=["glued-3px-taller-tiles-differ", "span-shorter-than-the-segment", "span-exactly-the-segment"],
)
def test_an_exact_segment_has_no_height_tolerance(sigs, spans):
    v = version(exact("cut", 9500, 9800), seg("keep", 0, 9500))
    got = run(layout(10000, spans), [], [entry(7, "above", v)], {**sigs, (0, 9500): sig(0)})
    assert ranges(got) == [], "an exact segment's far edge lies inside its span and its rows are its own"


@budget(load="pure")
def test_no_overlap_extension_grows_into_the_span_of_an_exact_segment():
    lay = layout(20000, [(0, 600), (602, 952), (1000, 20000)])
    v = version(seg("cut", 602, 952), exact("cut", 507, 600), seg("keep", 0, 507))
    sigs = {(602, 952): sig(602), (507, 600): sig(507), (0, 507): sig(0)}

    def extend(s, _blocked, taken):
        return s if s != (602, 952) or any(x < 600 and y > 0 for x, y in taken) else (0, 952)

    got = run(lay, [], [entry(7, "above", v)], sigs, extend=extend)
    assert ranges(got) == [(507, 600, 7, "cut"), (602, 952, 7, "cut")], "the exact cut piece counts as removed"


def grow_to(target, calls):
    """Return an overlap extension growing the segment `target` names: it stops at taken rows, fails at blocked."""

    def extend(s, blocked, taken):
        calls.append((s, list(blocked), list(taken)))
        grown = target.get(s, s)

        def hit(rs):
            return any(x < grown[1] and grown[0] < y and not (s[0] <= x and y <= s[1]) for x, y in rs)

        return s if hit(taken) else None if hit(blocked) else grown

    return extend


@budget(load="pure")
def test_ink_reaching_an_exact_keep_fails_the_match():
    lay = layout(20000, [(0, 600), (602, 952), (1000, 20000)])
    v = version(seg("cut", 602, 952), exact("keep", 507, 600), seg("keep", 0, 507))
    sigs = {(602, 952): sig(602), (507, 600): sig(507), (0, 507): sig(0)}
    calls = []
    got = run(lay, [], [entry(7, "above", v)], sigs, extend=grow_to({(602, 952): (0, 952)}, calls))
    assert ranges(got) == [], "the extension reached a keep: no cut"
    [(_, blocked, taken)] = calls
    assert (507, 600) in blocked, "the exact keep is blocked"
    assert (507, 600) not in taken, "the exact keep is not taken"


@budget(load="pure")
def test_an_extension_facing_the_cut_piece_of_a_split_span_stops_there():
    lay = layout(20000, [(0, 600), (602, 952), (1000, 20000)])
    v = version(exact("keep", 0, 507), seg("cut", 507, 600), seg("cut", 602, 952))
    sigs = {(0, 507): sig(0), (507, 600): sig(507), (602, 952): sig(602)}
    calls = []
    got = run(lay, [], [entry(7, "below", v)], sigs, extend=grow_to({(602, 952): (0, 952)}, calls))
    assert ranges(got) == [(507, 600, 7, "cut"), (602, 952, 7, "cut")], calls
    assert [(s, (507, 600) in taken) for s, _, taken in calls] == [((507, 600), False), ((602, 952), True)], (
        "the extension faces the split span's cut piece: taken"
    )
    assert (0, 507) in calls[0][2], "the rest's glued exact partner counts as taken for it"


@budget(load="pure")
def test_the_rest_of_a_split_span_never_extends_across_its_exact_keep():
    lay = layout(20000, [(0, 600), (602, 952), (1000, 20000)])
    v = version(seg("keep", 602, 952), exact("keep", 507, 600), seg("cut", 0, 507))
    sigs = {(602, 952): sig(602), (507, 600): sig(507), (0, 507): sig(0)}
    calls = []
    got = run(lay, [], [entry(7, "above", v)], sigs, extend=grow_to({(0, 507): (0, 952)}, calls))
    assert ranges(got) == [(0, 507, 7, "cut")], "the match holds"
    [(s, _, taken)] = calls
    assert s == (0, 507), calls
    assert (507, 600) in taken, "the glued keep's rows count as taken for the rest of its span"


# ---------------------------------------------------------------- extension and its cap (synthetic)


@budget(load="pure")
def test_a_matchs_overlapping_extended_segments_are_merged_once():
    spans = [(0, 18000), (18300, 18400), (18408, 18500), (19000, 20000)]
    lay = layout(20000, spans)
    v = version(seg("cut", 18408, 18500), seg("cut", 18300, 18400))
    got = run(
        lay, [band(19000, 20000)], [entry(1, "above", v)], sigs_of(spans), extend=lambda _s, _b, _t: (18300, 18500)
    )
    assert ranges(got) == [(18300, 18500, 1, "cut")], "1 range, never a duplicate"
    nested = strip.cut_strip(1000, [], [{"start": 100, "end": 500}, {"start": 200, "end": 300}])
    assert nested["kept"] == [(0, 100), (500, 1000)], "nested ranges never step the kept rows back"


@budget(load="pure")
def test_the_cap_never_counts_the_matchs_own_edge_rows():
    spans = [(0, 17000), (18100, 18200), (18300, 18400), (18500, 20000)]
    lay = layout(20000, spans)
    v = version(seg("cut", 18300, 18400), seg("edge"))
    got = run(
        lay, [band(18100, 18200)], [entry(1, "below", v)], sigs_of(spans), extend=lambda _s, _b, _t: (18300, 19500)
    )
    assert (ranges(got), got["misses"]) == ([(18300, 20000, 1, "cut")], []), "the edge segment's rows go anyway"


# ---------------------------------------------------------------- card holes


@budget(load="pure")
def test_card_holes_run_between_the_removed_ranges_around_a_card_match():
    # metadata-only: I Became the Tyrant's Dying Wife c0046, Carrotoon end card
    removed = [band(109000, 109400, jid=6978), band(110800, 110900, jid=50, why="cut")]
    holes: list[an.Hole] = [{"decision": 50, "span": (110800, 112404), "region": "tail", "own": [(110800, 110900)]}]
    assert an.card_holes(112404, removed, holes, EDGE) == [(109400, 110800), (110900, 112404)], (
        "logo and SNS lines (109400-110800) are protected although the chain never names them"
    )
    alone = an.card_holes(112404, removed[1:], holes, EDGE)
    assert alone[0] == (112404 - EDGE, 110800), "no removed range above the card: the hole stops at the tail region"


# ---------------------------------------------------------------- signatures and pools


@budget(load="pure")
def test_tiles_match_by_tile_count_and_hamming_distance_only():
    tiles = "00ff00ff00ff00ff"
    assert an.tiles_match(tiles, "00ff00ff00ff00c0|9.0"), "6 bits apart, any aspect: a match"
    assert not an.tiles_match(tiles, "00ff00ff00ff0080|0.1"), "7 bits apart: no match"
    assert not an.tiles_match(tiles, f"{tiles}:{tiles}|2.0"), "another tile count: no match"
    assert not an.tiles_match(tiles, None), "a span with no signature never matches"


@budget(load="pure")
def test_span_sigs_renders_each_span_once():
    calls = []

    def render(a, b):
        calls.append((a, b))
        return Image.new("RGB", (100, b - a), "white")

    span_sig = an.span_sigs(render)
    assert (span_sig(0, 50), span_sig(0, 50)) == (None, None), "a uniform span has no signature"
    assert calls == [(0, 50)], "each span is rendered once"


@budget(1.0, load="pure")  # the first chain parse of a process signs the uniform tiles (~0.45 s)
def test_the_pools_take_every_version_and_skip_units_without_a_chain():
    cut = {"above": [raw_version(seg("cut", 100, 200)), raw_version(seg("cut", 100, 300))]}
    rows = [db_row(id=4, chain=chains.text(cut), decided_at=ADDED, credit=True)]
    got = an.cut_pool(rows)
    assert [(e["decision"], e["why"], e["side"], e["unit"], e["credit"]) for e in got] == [
        (4, "cut", "above", None, True),
        (4, "cut", "above", None, True),
    ], "every version of a cut decision, tried at every seed, with its credit"
    card = {"below": [raw_version(seg("edge"), src=False)]}
    units: list[an.UnitRange] = [
        {"start": 100, "end": 200, "where": "tail", "decision": 5, "chain": chains.text(card), "added": ADDED},
        {"start": 300, "end": 400, "where": "tail", "decision": 6, "chain": None, "added": ADDED},
    ]
    got = an.unit_pool(units)
    assert [(e["decision"], e["why"], e["side"], e["unit"], e["where"], e["credit"]) for e in got] == [
        (5, "card", "below", (100, 200), "tail", False)
    ], "a unit's chain acts at its own unit; a unit without a chain gives no entry"
    with pytest.raises(ValueError, match="credit"):
        an.cut_pool([db_row(id=4, chain=chains.text(cut), decided_at=ADDED, credit=None)])


# ---------------------------------------------------------------- real strips (fixtures with pixels)


def fixture(name):
    """Return (Strip of the fixture's strip.png as a 1-page strip, its crop start, render, its scan.json)."""
    d = FIX / name
    scan = json.loads((d / "scan.json").read_text(encoding="utf-8"))
    path = d / "strip.png"
    im = Image.open(path).convert("RGB")
    st = strip.make_strip([path], [images.page_size(path)], [strip.page_rows(im)])

    def render(a, b):
        return im.crop((0, a, im.width, b))

    return st, scan["crop"][0], render, scan


def strip_layout(st):
    return an.Layout(st.height, st.width, strip.spans(st), strip.find_gaps(st))


def rseg(kind, render, width, s, e):
    return {"kind": kind, "h": round((e - s) / width, 4), "tiles": present(an.signable(render(s, e))).split("|")[0]}


def real_run(lay, render, removed, pool):
    view = an.View(lay, an.span_sigs(render), an.extender(lay, render), EDGE)
    return an.anchored_ranges(view, removed, an.seeds(lay, removed, EDGE), pool)


@budget(2.0, load="fs")
def test_5_2_thin_notice_lines_are_cut_from_the_logo_and_collapse_to_one_gutter():
    # I Was the Real Head of the House c0003: D&C card, 3 notice lines 111612-111684, D&C logo 111751-111787, (c) line
    # 111832-111846, the removed Magus band from 111931. A cut [keep (c)][keep logo][cut][cut][cut] from the band
    st, o, render, _ = fixture("s5_2_real_head_c0003")
    w = st.width
    sp = [
        (s - o, e - o)
        for s, e in [(111830, 111847), (111751, 111787), (111665, 111684), (111638, 111657), (111612, 111630)]
    ]
    assert set(sp) <= set(strip.spans(st)), strip.spans(st)
    v = {
        "segments": [
            rseg(k, render, w, s, e) for k, (s, e) in zip(("keep", "keep", "cut", "cut", "cut"), sp, strict=True)
        ],
        "ref": [("I Was the Real Head of the House", "Gamma_Chapter 3.cbz", 0)],
        "src": {"file": "Gamma_Chapter 3.cbz", "sha256": SHA},
    }
    removed = [band(111931 - o, st.height, jid=298)]
    lay = strip_layout(st)
    got = real_run(lay, render, removed, [entry(10700, "above", v)])
    assert [(r["start"] + o, r["end"] + o, r["why"]) for r in got["added"]] == [
        (111612, 111630, "cut"),
        (111638, 111657, "cut"),
        (111665, 111684, "cut"),
    ], "the 3 notice lines are cut"
    every = as_drops(removed + got["added"])
    every += su.join_rule(lay, every, su.gutter(w))
    kept = an.uncovered(111558 - o, 111751 - o, [(r["start"], r["end"]) for r in every])
    assert sum(e - s for s, e in kept) == su.gutter(w), "between the credits and the logo: 1 gutter left"
    assert all(lay.blank(s, e) for s, e in kept), "between the credits and the logo: blank rows only"
    assert got["holes"], "a card match with keep segments is a hole"
    assert got["holes"][0]["span"] == (111612 - o, 111847 - o), "the gap seed under the (c) line anchors it"


@budget(2.0, load="fs")
def test_5_2_lines_glued_without_blank_rows_are_one_span():
    st, o, render, _ = fixture("s5_2_i_stole_c0006")
    glued = (132463 - o, 132663 - o)
    assert glued in strip.spans(st), "the D&C logo, (c) line and bunny watermark are 1 span"
    assert an.signable(render(*glued)) is not None, "the glued span is signable"


@budget(2.0, load="fs")
@pytest.mark.parametrize(
    ("name", "span"),
    [("s5_4_possessed_c0062", (58235, 58339)), ("s5_4_swapped_c0007", (128064, None))],
    ids=["logo-spans-every-notice-row", "notice-beside-the-title-logo"],
)
def test_5_4_a_logo_sharing_rows_with_the_notice_is_one_span(name, span):
    st, o, _, _ = fixture(name)
    want = (span[0] - o, st.height if span[1] is None else span[1] - o)
    assert want in strip.spans(st), "a logo sharing rows with the notice is 1 span with it"


def faint_strip():
    """Return a 760 x 600 white strip: a notice line 300-318, a logo 240-260 and a faint stroke 250-330 joining them."""
    a = np.full((600, 760, 3), 255, np.uint8)
    a[300:318, 300:700] = 0
    a[250:330, 58:61] = 240
    a[240:260, 40:120] = 60
    a[100:140, 100:600] = 30
    a[480:520, 100:600] = 30
    return a


def array_render(a):
    def render(x, y):
        return Image.fromarray(a[x:y])

    return render


@budget(2.0, load="pure")
def test_5_4_overlap_extension_takes_a_faint_object_crossing_a_walkable_run():
    render = array_render(faint_strip())
    lay = an.Layout(600, 760, [(100, 140), (240, 260), (300, 318), (480, 520)], [])
    assert an.overlap_extend(lay, (300, 318), [], [], render) == (240, 318), "the faint stroke takes the logo's span"
    assert an.overlap_extend(lay, (300, 318), [(240, 260)], [], render) is None, "reaching a keep fails the match"
    assert an.overlap_extend(lay, (300, 318), [], [(230, 262)], render) == (300, 318), (
        "a removed range there: never extended past"
    )
    flush = an.Layout(600, 760, [(100, 140), (240, 300), (300, 318), (480, 520)], [])
    assert an.overlap_extend(flush, (300, 318), [], [], render) == (300, 318), "a 0-row run: no background"
    plain = np.full((600, 760, 3), 255, np.uint8)
    plain[300:318, 300:700] = 0
    plain[240:260, 40:120] = 60
    assert an.overlap_extend(lay, (300, 318), [], [], array_render(plain)) == (300, 318), "no faint object: unchanged"


@budget(2.0, load="pure")
def test_overlap_extension_never_passes_a_strip_end():
    a = faint_strip()
    a[0:300, 58:61] = 240  # the faint stroke runs up to the strip start
    top = an.Layout(600, 760, [(300, 318), (480, 520)], [])
    assert an.overlap_extend(top, (300, 318), [], [], array_render(a)) == (300, 318), (
        "the stroke reaches the strip start, no span: the segment is never extended past a strip end"
    )


@budget(3.0, load="fs")
def test_5_5_a_notice_inside_an_official_card_is_cut_and_its_card_is_a_hole():
    # Bring The Love c0095: the Korean notice 97354-97449 (3 lines), the Wink / Seoul Media logo row (kept), the
    # removed Daeva band from 97618. A cut from the band [keep][keep logo][cut x3], no credit: the card is a hole
    st, o, render, _ = fixture("s5_5_bring_the_love_c0095")
    w = st.width
    sp = [(97569, 97618), (97475, 97537), (97423, 97449), (97387, 97413), (97354, 97380)]
    sp = [(s - o, e - o) for s, e in sp]
    v = {
        "segments": [
            rseg(k, render, w, s, e) for k, (s, e) in zip(("keep", "keep", "cut", "cut", "cut"), sp, strict=True)
        ],
        "ref": [("Bring The Love", "Gamma_Chapter 94.cbz", 0)],
        "src": {"file": "Gamma_Chapter 94.cbz", "sha256": SHA},
    }
    removed = [band(97618 - o, st.height, jid=9471)]
    got = real_run(strip_layout(st), render, removed, [entry(10800, "above", v)])
    assert [(r["start"] + o, r["end"] + o) for r in got["added"]] == [(97354, 97380), (97387, 97413), (97423, 97449)], (
        "the 3 notice lines are cut"
    )
    holes = an.card_holes(st.height, removed + got["added"], got["holes"], EDGE)
    assert holes == [(97380 - o, 97387 - o), (97413 - o, 97423 - o), (97449 - o, 97618 - o)], (
        "the matched span stays whole; nothing above it is in the hole"
    )


def stole_strip(panel_end):
    """Return a 760 x 600 white strip: a band 240-280, a notice line 300-318, an 8 px run, a panel 326-panel_end.

    A faint stroke (luma 240) across the run joins the line to the panel (JPEG ringing).
    """
    a = np.full((600, 760, 3), 255, np.uint8)
    a[240:280, 100:600] = 30
    a[300:318, 300:700] = 0
    a[326:panel_end, 50:700] = 90
    a[310:330, 200:203] = 240
    return a


def pixel_run(a, spans, removed, pool):
    render = array_render(a)
    lay = layout(a.shape[0], spans, width=a.shape[1])
    return real_run(lay, render, removed, pool)


@budget(2.0, load="pure")
def test_an_overlap_extension_past_the_segment_height_fails_the_match():
    # I Stole c0006: the notice line (18 px) bridged to a 200 px panel by an 8 px run
    a = stole_strip(526)
    v = version(rseg("cut", array_render(a), 760, 300, 318))
    spans = [(240, 280), (300, 318), (326, 526)]
    got = pixel_run(a, spans, [band(240, 280, where="head")], [entry(10900, "below", v)])
    assert (got["added"], got["matched"]) == ([], []), "the extension would add 208 px: the match fails"
    assert no_unit(got) == [miss(10900, "below", "cap", (300, 318))], "a cut decision's cap failure is a miss"
    unit = band(240, 280, jid=4817, where="head")
    got = pixel_run(a, spans, [unit], [entry(4817, "below", v, card=((240, 280), "head"))])
    assert no_unit(got) == [miss(4817, "below", "cap", (300, 318))], "a unit's chain's too"
    small = stole_strip(334)  # an 8 px object: 8 + 8 = 16 px <= 18 px, taken with the notice
    got = pixel_run(
        small, [(240, 280), (300, 318), (326, 334)], [band(240, 280, where="head")], [entry(10900, "below", v)]
    )
    assert [(r["start"], r["end"]) for r in got["added"]] == [(300, 334)], "an extension within the cap is taken"


@budget(2.0, load="pure")
def test_an_overlap_extension_at_a_gap_seed_takes_its_background_from_the_gap():
    a = np.full((600, 760, 3), 255, np.uint8)
    a[100:140, 100:600] = 30
    a[240:282, 300:700] = 0
    a[290:305, 40:120] = 60
    a[270:295, 58:61] = 240
    a[480:520, 100:600] = 30
    v = version(rseg("cut", array_render(a), 760, 240, 282))
    spans = [(100, 140), (240, 282), (290, 305), (480, 520)]
    got = pixel_run(a, spans, [], [entry(1, "below", v)])
    assert [(r["start"], r["end"]) for r in got["added"]] == [(240, 305)], "the notice takes the logo (23 px <= 42 px)"
    lay = layout(600, spans, width=760)
    assert [s["kind"] for s in an.seeds(lay, [], EDGE) if (s["pos"], s["side"]) == (240, "below")] == ["gap"], (
        "the match is at a gap seed"
    )


SIGNED = ["s5_2_i_stole_c0006", "s5_4_possessed_c0062", "s5_5_bring_the_love_c0095"]  # fixtures with a unit in crop


@budget(3.0, load="fs")
@pytest.mark.parametrize("name", SIGNED)
def test_the_old_tools_signatures_equal_signable_of_the_same_rows(name):
    st, o, render, scan = fixture(name)
    units = [
        u for key in ("blocks", "removed", "pieces") for u in scan[key] if o <= u["start"] and u["end"] <= o + st.height
    ]
    assert units, "the fixture holds a unit inside its crop"
    for u in units:
        assert an.signable(render(u["start"] - o, u["end"] - o)) == u["sig"], (
            f"rows {u['start']}-{u['end']}: the old tool's signature is the pipeline's (decoder and hash agree)"
        )
