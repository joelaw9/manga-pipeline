"""strip_units.decide with chains and cut decisions (SPEC "Junk decisions": chains and cuts), on a pixel strip.

The order of decide (plan phase-10-4, PR B design): tail bands and watermarks after the matcher, card holes in the tail
pass, unmeasured pieces measured through render, step 5's cuts at the tail drops, step 6's owned spans, misses.
"""

import functools
from typing import NamedTuple

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import images, strip
from manga_pipeline.junk import anchored as an
from manga_pipeline.junk import chains
from manga_pipeline.junk import strip_units as su

from tests import pages as fx
from tests.builders import db_row, present
from tests.test_junk_strip import save_one
from tests.timing import budget

W = 1000
DECIDED = "2026-01-01T00:00:00+00:00"
SHA = "a" * 64
# the strip as built: blobs at these rows, white between them; S5A and S5B are 1 block (11 px apart, under 1 gutter)
BUILT = {"head": (0, 2000), "panel": (2060, 2460), "line1": (2520, 2580), "line2": (2640, 2700), "s5a": (2760, 2860)}
BUILT["s5b"] = (2871, 3060)
HEIGHT = 3120


def strip_image(*, short_line2=False, same_lines=False):
    a = np.full((HEIGHT, W, 3), 255, np.uint8)
    for seed, (name, (s, built_end)) in enumerate(BUILT.items(), 1):
        e = s + 30 if short_line2 and name == "line2" else built_end  # 30 px: under the gap rule's 40
        a[s:e] = np.asarray(fx.blob(3 if same_lines and name == "line2" else seed, (W, e - s)))
    return Image.fromarray(a)


class Scanned(NamedTuple):
    """A scanned pixel strip: its scan, its render and the rows of each built blob as the scan found its span."""

    sc: su.StripScan
    render: an.Render
    rows: dict[str, strip.Rows]


def scanned(tmp_path, *, short_line2=False, same_lines=False) -> Scanned:
    """Return the strip scanned as the junk stage scans it (each blob 1 span, a few edge rows wider than built)."""
    st = save_one(tmp_path, strip_image(short_line2=short_line2, same_lines=same_lines))
    sc = su.scan(st)
    assert len(sc["spans"]) == len(BUILT), sc["spans"]
    rows = {name: (s, e) for name, (s, e) in zip(BUILT, sc["spans"], strict=True)}
    for name, (s, e) in rows.items():
        assert abs(s - BUILT[name][0]) <= 3, (name, s, e)
    return Scanned(sc, functools.partial(strip.render_rows, st.pages, st.width, cache={}), rows)


def seg(kind, rows, render, *, exact_rows=None):
    """Return a cut or keep segment of strip rows (its tiles read from render); exact_rows: an exact segment's rows."""
    s, e = rows if exact_rows is None else exact_rows
    out = {"kind": kind, "h": (e - s) / W, "tiles": present(an.signable(render(*rows))).split("|")[0]}
    return out if exact_rows is None else {**out, "exact": True}


def chain(side, *segs, cut=True):
    """Return a chain's text of 1 side and 1 version (a cut decision's carries its source, a unit's none)."""
    src = {"file": "c.cbz", "sha256": SHA} if cut else None
    return chains.text({side: [{"segments": list(segs), "ref": [["S", "c.cbz", 0]], "src": src}]})


def cut_row(decision, text, *, credit):
    return db_row(id=decision, kind="cut", chain=text, credit=credit, decided_at=DECIDED, verdict="junk")


def deciding(sc, units=(), pieces=()):
    """Return decide_unit: block decisions [(rows, id, chain)] on the scan's blocks, piece decisions [(sig, id)]."""
    sigs = {(b["start"], b["end"]): b["sig"] for b in sc["blocks"]}
    rows = [("block", sigs[r], i, c) for r, i, c in units] + [("piece", s, i, None) for s, i in pieces]

    def decide_unit(kind, sig):
        for k, key, i, c in rows:
            if k == kind and images.matches(images.sig_from_str(key), images.sig_from_str(sig)):
                return db_row(id=i, verdict="junk", key=key, chain=c, decided_at=DECIDED)
        return None

    return decide_unit


def removed(got):
    return [(r["start"], r["end"], r["why"], r.get("decision"), "hash" in r) for r in got["removed"]]


def undecided(got):
    return [(u["kind"], u["start"], u["end"]) for u in got["undecided"]]


def credit_lines(x, *more, credit=True):
    """Return a cut decision (9) whose version, below, cuts LINE1, LINE2 and the rows named in `more`."""
    segs = [seg("cut", x.rows[n], x.render) for n in ("line1", "line2", *more)]
    return cut_row(9, chain("below", *segs), credit=credit)


@budget(5.0, load="fs")
def test_the_pixel_strip_is_laid_as_built(tmp_path):
    x = scanned(tmp_path)
    s5a, s5b = x.rows["s5a"], x.rows["s5b"]
    assert s5b[0] - s5a[1] < su.gutter(W), "S5A and S5B are 1 block"
    assert f"{s5a[0]}:{s5b[1]}" in x.sc["measured"], "measured whole by the scan"


@budget(5.0, load="fs")
def test_a_credit_cut_of_2_ranges_is_1_band_no_watermark_and_starts_the_credit(tmp_path):
    x = scanned(tmp_path)
    r = x.rows
    got = su.decide(x.sc, deciding(x.sc), cuts=[credit_lines(x)], render=x.render)
    assert (*r["line1"], "cut", 9, False) in removed(got), "a cut range names its decision, with no unit hash"
    assert (*r["line2"], "cut", 9, False) in removed(got)
    assert ("piece", r["s5a"][0], r["s5b"][1]) in undecided(got), "1 match is 1 band, no watermark: the credit starts"
    assert got["watermarks"] == []
    assert got["misses"] == []


@budget(5.0, load="fs")
def test_a_cut_without_credit_starts_no_credit(tmp_path):
    x = scanned(tmp_path)
    r = x.rows
    got = su.decide(x.sc, deciding(x.sc), cuts=[credit_lines(x, credit=False)], render=x.render)
    assert (*r["line1"], "cut", 9, False) in removed(got)
    assert not [u for u in got["undecided"] if u["kind"] == "piece"], "no band: no tail pass, no piece"
    assert ("block", r["s5a"][0], r["s5b"][1]) in undecided(got), "the tail block is offered as a block"


@budget(5.0, load="fs")
def test_a_credit_cuts_key_equal_to_a_middle_block_or_a_known_watermark_is_no_watermark(tmp_path):
    x = scanned(tmp_path)
    cut = credit_lines(x)
    key = chains.key_span(chains.parse(cut["chain"]))
    x.sc["middle"] = [key]
    got = su.decide(x.sc, deciding(x.sc), [images.sig_from_str(key)], [cut], x.render)
    assert ("piece", x.rows["s5a"][0], x.rows["s5b"][1]) in undecided(got), (
        "a cut decision's key is never compared: still the credit start"
    )
    assert got["watermarks"] == [], "nor remembered"


@budget(5.0, load="fs")
def test_a_watermark_chained_block_returns_only_its_match_key_and_its_card_is_no_band(tmp_path):
    x = scanned(tmp_path)
    r = x.rows
    decide_unit = deciding(x.sc, [(r["panel"], 1, chain("below", seg("cut", r["line1"], x.render), cut=False))])
    key = present(decide_unit("block", next(b["sig"] for b in x.sc["blocks"] if b["start"] == r["panel"][0])))["key"]
    got = su.decide(x.sc, decide_unit, [images.sig_from_str(key)], render=x.render)
    assert (*r["panel"], "match", 1, True) in removed(got)
    assert (*r["line1"], "card", 1, False) in removed(got), "the card's range: its decision, no hash"
    assert got["watermarks"] == [key], "the block's match key alone"
    assert not [u for u in got["undecided"] if u["kind"] == "piece"], "a watermark and a card start no credit"


@budget(5.0, load="fs")
def test_a_cut_inside_a_tail_block_leaves_a_piece_measured_through_render(tmp_path):
    x = scanned(tmp_path)
    r = x.rows
    rest = (r["s5a"][1], r["s5b"][1])
    assert f"{rest[0]}:{rest[1]}" not in x.sc["measured"], "the scan never measured the rest of the block"
    got = su.decide(x.sc, deciding(x.sc), cuts=[credit_lines(x, "s5a")], render=x.render)
    assert (*r["s5a"], "cut", 9, False) in removed(got), got["misses"]
    assert ("piece", *rest) in undecided(got), "the rest of the block is a piece, measured from its pixels"


@budget(5.0, load="fs")
def test_a_card_hole_survives_the_piece_rule_and_its_blocks_are_no_candidates(tmp_path):
    """#48 / #59's step 6: the matched span's blocks are no candidates; a head block outside it still is."""
    x = scanned(tmp_path)
    r = x.rows
    card = chain("below", seg("keep", r["line1"], x.render), seg("cut", r["line2"], x.render), cut=False)
    logo = present(x.sc["measured"].get("{}:{}".format(*r["line1"])))["sig"]
    got = su.decide(x.sc, deciding(x.sc, [(r["panel"], 1, card)], [(logo, 7)]), render=x.render)
    assert (*r["panel"], "match", 1, True) in removed(got)
    assert (*r["line2"], "card", 1, False) in removed(got)
    assert not [q for q in got["removed"] if q["start"] < r["line1"][1] and r["line1"][0] < q["end"]], (
        "the kept logo is a hole: no piece decision removes it"
    )
    assert ("block", *r["line1"]) not in undecided(got), "an edge block inside the matched span is no candidate"
    assert ("block", *r["head"]) in undecided(got), "a head block outside it still is"


@budget(5.0, load="fs")
def test_step_5_a_cut_seeded_by_a_tail_drop_withdraws_the_piece_it_covers(tmp_path):
    """#66: the cut's 30 px span fails the gap rule at its gaps; only the dropped piece's edge seeds it."""
    x = scanned(tmp_path, short_line2=True)
    r = x.rows
    block = (r["s5a"][0], r["s5b"][1])
    tail = present(x.sc["measured"].get("{}:{}".format(*block)))["sig"]
    decide_unit = deciding(x.sc, [(r["panel"], 1, None)], [(tail, 7)])
    alone = su.decide(x.sc, decide_unit, render=x.render)
    assert ("piece", *r["line2"]) in undecided(alone), "without the cut: a review piece"
    cut = cut_row(9, chain("above", seg("cut", r["line2"], x.render)), credit=False)
    got = su.decide(x.sc, decide_unit, cuts=[cut], render=x.render)
    assert (*block, "piece", 7, True) in removed(got), "the piece decision drops the tail block"
    assert (*r["line2"], "cut", 9, False) in removed(got), "the cut, seeded by that drop's edge"
    assert ("piece", *r["line2"]) not in undecided(got), "the piece it covers is withdrawn"
    assert ("piece", *r["line1"]) in undecided(got), "the other piece stays"


@budget(5.0, load="fs")
def test_step_5_a_piece_the_cut_covers_leaves_the_rest_of_card_and_a_decided_one_stays(tmp_path):
    """W5: a piece wholly removed by a step-5 cut is no rest-of-card piece; the piece decision's dropped one is."""
    x = scanned(tmp_path, short_line2=True)
    r = x.rows
    block = (r["s5a"][0], r["s5b"][1])
    tail = present(x.sc["measured"].get("{}:{}".format(*block)))["sig"]
    decide_unit = deciding(x.sc, [(r["panel"], 1, None)], [(tail, 7)])
    cut = cut_row(9, chain("above", seg("cut", r["line2"], x.render)), credit=False)

    def rest(got):
        return [(p["start"], p["end"]) for g in got["rest"] for p in g["pieces"]]

    assert r["line2"] in rest(su.decide(x.sc, decide_unit, render=x.render)), "without the cut: a W5 piece"
    got = rest(su.decide(x.sc, decide_unit, cuts=[cut], render=x.render))
    assert r["line2"] not in got, "wholly cut at step 5: it leaves the pieces"
    assert block in got, "the tail block a piece decision drops is still a W5 piece"


@budget(5.0, load="fs")
def test_a_card_that_does_not_match_is_a_miss(tmp_path):
    x = scanned(tmp_path)
    r = x.rows
    wrong = chain("below", seg("cut", r["line2"], x.render), cut=False)  # LINE2's tiles where LINE1 lies
    got = su.decide(x.sc, deciding(x.sc, [(r["panel"], 1, wrong)]), render=x.render)
    a, b = r["panel"]
    assert got["misses"] == [
        f"junk decision 1's card (below) does not match beside rows {a}-{b}: fix or undo the decision"
    ]


@budget(load="pure")
def test_a_cap_miss_names_its_segment():
    m: an.Miss = {"decision": 9, "side": "above", "why": "cap", "unit": None, "segment": (300, 318)}
    assert su.miss_text(m) == "junk decision 9's cut grows past its segment at rows 300-318: fix or undo the decision"


@budget(5.0, load="fs")
def test_a_chain_or_cut_with_no_render_fails_fast(tmp_path):
    x = scanned(tmp_path)
    r = x.rows
    with pytest.raises(ValueError, match="no render"):
        su.decide(x.sc, deciding(x.sc), cuts=[credit_lines(x)])
    card = chain("below", seg("cut", r["line1"], x.render), cut=False)
    with pytest.raises(ValueError, match="no render"):
        su.decide(x.sc, deciding(x.sc, [(r["panel"], 1, card)]))
    assert su.decide(x.sc, deciding(x.sc, [(r["panel"], 1, None)]))["misses"] == [], "no chain, no cut: no pixels"


@budget(5.0, load="fs")
def test_a_credit_cut_matching_twice_in_1_chapter_is_a_watermark(tmp_path):
    """#53 per chapter: LINE2 is LINE1's art; 1 cut of it matches at both: 2 bands, a watermark, no credit start."""
    x = scanned(tmp_path, same_lines=True)
    r = x.rows
    cut = cut_row(9, chain("below", seg("cut", r["line1"], x.render)), credit=True)
    got = su.decide(x.sc, deciding(x.sc), cuts=[cut], render=x.render)
    assert (*r["line1"], "cut", 9, False) in removed(got)
    assert (*r["line2"], "cut", 9, False) in removed(got), "the same cut, matched a second time"
    assert not [u for u in got["undecided"] if u["kind"] == "piece"], "a watermark starts no credit"
    assert got["watermarks"] == [], "a cut decision's key is never remembered"
