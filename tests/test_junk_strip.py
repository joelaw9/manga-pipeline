"""Junk units of a long-strip chapter (SPEC "Junk decisions": units, decisions; manga_pipeline.junk.strip_units): edge
blocks and their probes, stored decisions on them (match_blocks), the watermark rule, the tail pass (tail pieces, W1
blank, W3 wedge), the join rule and the kept check, on synthetic strips and layouts and on 1 real strip head
(tests/fixtures_junk/s5_2_real_head_c0003). Ported from the bulk converter's tests/test_restitch.py,
test_anchored.py and test_junk_single.py (chains, cut rows, card holes and the cross-chapter watermark rule are not
ported: strip_units has none of them)."""

import hashlib
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import images, strip
from manga_pipeline.junk import strip_units as su

from tests import pages as fx
from tests.timing import budget

JUNK_FIX = Path(__file__).with_name("fixtures_junk")
BANNER = fx.PAGE_FIX / "strip_bring_the_love_ch87_p001_head.png"  # banner glued onto the cover at row 166


# ---------------------------------------------------------------- shared builders


def strip_of(pages):
    """A chapter's Strip as the junk stage builds it: header sizes and each page's row profile."""
    return strip.make_strip(
        pages, [images.page_size(p) for p in pages], [strip.page_rows(images.open_image(p)[0]) for p in pages]
    )


def sliced(out_dir, im, height=1000):
    """The image uploaded as fixed-height PNG slices; its Strip."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, top in enumerate(range(0, im.height, height), 1):
        paths.append(out_dir / f"{n:03d}.png")
        im.crop((0, top, im.width, min(im.height, top + height))).save(paths[-1])
    return strip_of(paths)


def save_one(out_dir, im):
    """The image saved as 1 PNG page; its Strip."""
    p = out_dir / "001.png"
    im.save(p)
    return strip_of([p])


def panel_strip(panels, width=800, gutter=80, seed=0):
    """Webtoon strip: colour panels of the given heights separated by white gutters; (image, [gutter (start, end)])."""
    rows, gutters, y = [], [], 0
    for i, h in enumerate(panels):
        rows.append(np.asarray(fx.webtoon_page((width, h), seed=seed + i)))
        y += h
        rows.append(np.full((gutter, width, 3), 255, np.uint8))
        gutters.append((y, y + gutter))
        y += gutter
    return Image.fromarray(np.vstack(rows)), gutters


def store(*rows):
    """decide_unit over decisions (kind, key, verdict, id): the first of the unit's kind whose key matches."""

    def decide_unit(kind, sig):
        for k, key, verdict, i in rows:
            if k == kind and images.matches(images.sig_from_str(key), images.sig_from_str(sig)):
                return {"id": i, "verdict": verdict, "key": key}
        return None

    return decide_unit


def sig_of(im):
    return images.sig_to_str(images.signature(im))


def flat_block(h, w=200):
    """Columns alternating 245 / 235: every row varies (std 5: no gap row) but the block's range is 10: uniform."""
    a = np.full((h, w, 3), 245, np.uint8)
    a[:, ::2] = 235
    return Image.fromarray(a)


def span(rows):
    return [(r["start"], r["end"]) for r in rows]


# ---------------------------------------------------------------- edge blocks and stored decisions (match_blocks)


@budget(2.0, load="fs")
def test_approved_junk_block_removed_unapproved_kept(tmp_path):
    """The tail notice is undecided until a block decision approves it, then removed; the join rule takes its blank
    run to the strip end and the gutter above it whole (1 join at the strip end)."""
    im, _ = panel_strip([1200, 1500, 1300, 600], seed=3)  # the last panel plays the recruitment notice
    sc = su.scan(sliced(tmp_path, im))
    before = su.decide(sc, store())
    assert before["removed"] == [], "nothing approved: nothing removed"
    tail = sc["blocks"][-1]  # strip < 2 x 6,000 px: every block is an edge block
    assert tail["where"] == "tail", tail
    assert (tail["start"], tail["end"]) in [(u["start"], u["end"]) for u in before["undecided"]], "offered undecided"
    after = su.decide(sc, store(("block", tail["sig"], "junk", 9)))
    assert [r["why"] for r in after["removed"]] == ["join", "match", "join"], after["removed"]
    assert after["removed"][-1]["end"] == im.height, "the blank run to the strip end goes"
    assert (tail["start"], tail["end"]) not in [(u["start"], u["end"]) for u in after["undecided"]]


@budget(1.0, load="fs")
def test_approved_banner_cut_off_its_edge_block():
    """Bring The Love ch87: the approved banner piece is removed at its hard join, the cover stays an undecided block
    without probes."""
    banner = sig_of(Image.open(BANNER).convert("RGB").crop((0, 0, 360, 166)))
    got = su.decide(su.scan(strip_of([BANNER])), store(("probe", banner, "junk", 3)))
    assert got["removed"] == [{"start": 0, "end": 166, "why": "match"}], got["removed"]
    assert [(u["kind"], u["start"], u["end"], u["probes"]) for u in got["undecided"]] == [("block", 166, 300, [])], (
        "the cover stays"
    )


@budget(1.0, load="fs")
def test_edge_block_kept_whole_when_no_piece_is_approved_junk():
    """The banner join is there but nothing is approved: no cut through the block; the banner piece is its probe."""
    got = su.decide(su.scan(strip_of([BANNER])), store())
    assert got["removed"] == []
    (block,) = got["undecided"]
    assert (block["start"], block["end"]) == (0, 300)
    assert span(block["probes"]) == [(0, 166)]


@budget(1.0, load="fs")
def test_a_probe_decided_keep_is_no_longer_a_probe():
    """The banner probe was decided keep: the block stays undecided, without that probe, so it is not offered again."""
    sc = su.scan(strip_of([BANNER]))
    (probe,) = [j for j in sc["blocks"][0]["joins"] if not j["uniform"]]
    got = su.decide(sc, store(("probe", probe["sig"], "keep", 3)))
    (block,) = got["undecided"]
    assert (block["start"], block["end"], block["probes"]) == (0, 300, []), block


@budget(load="fs")
def test_short_and_uniform_edge_blocks_are_no_candidates(tmp_path):
    """A 30 px block (< MIN_EDGE_BLOCK_PX 40) and a uniform one (channel range 10) are never edge blocks."""
    im = fx.stack(fx.blob(1, (200, 600)), (40,), fx.blob(2, (200, 30)), (40,), flat_block(100), (40,))
    assert span(su.scan(sliced(tmp_path, im))["blocks"]) == [(0, 600)]


@budget(load="fs")
def test_a_uniform_remainder_after_a_glued_junk_cut_is_dropped(tmp_path):
    """Approved junk glued onto a uniform card: the junk piece is removed, the uniform rest is no undecided block."""
    a = np.zeros((200, 200, 3), np.uint8)
    a[:100] = np.asarray(fx.credit((200, 100)))
    a[100:] = np.asarray(flat_block(100))
    junk = sig_of(Image.fromarray(a[:100]))
    sc = su.scan(sliced(tmp_path, fx.stack(Image.fromarray(a), (40,), fx.blob(1, (200, 600)))))
    got = su.decide(sc, store(("probe", junk, "junk", 5)))
    assert [(r["start"], r["end"]) for r in got["removed"] if r["why"] == "match"] == [(0, 100)], got["removed"]
    assert span(got["undecided"]) == [(240, 840)], "the art block alone is left"


@budget(load="fs")
def test_no_ocr_no_text_and_a_split_remainder_has_no_probes():
    """Without ocr_text no text is read; a remainder after a glued-junk cut keeps no probes (1 cut per block)."""
    banner = sig_of(Image.open(BANNER).convert("RGB").crop((0, 0, 360, 166)))
    got = su.decide(su.scan(strip_of([BANNER])), store(("probe", banner, "junk", 3)))
    (block,) = got["undecided"]
    assert (block["start"], block["probes"], "text" in block) == (166, [], False)


@budget(load="fs")
def test_an_unmatched_edge_block_carries_its_banner_probe_with_its_text():
    """Nothing approved: the cover block keeps its banner piece as a probe, each with its own OCR text."""
    got = su.decide(su.scan(strip_of([BANNER]), lambda im: f"{im.height} px"), store())
    (block,) = got["undecided"]
    assert block["text"] == "300 px" and [q["text"] for q in block["probes"]] == ["166 px"], "each unit's own read"


@budget(load="fs")
def test_a_unit_with_a_stored_decision_is_not_read():
    """ocr_text reads only units `undecided` says have no stored decision: the decided banner piece gets no text."""
    banner = images.signature(Image.open(BANNER).convert("RGB").crop((0, 0, 360, 166)))
    sc = su.scan(strip_of([BANNER]), lambda im: "read", lambda sig: not images.matches(sig, banner))
    (block,) = sc["blocks"]
    assert block["text"] == "read" and "text" not in block["joins"][0], block["joins"]


def glued_block(where):
    """A scanned edge block of rows 0-200 with hard joins at rows 60 and 120, walked from the chapter edge: Bring The
    Love ch87's banner glued onto the cover is such a block (1 join there)."""

    def join(a, b, rest):
        return {
            "start": a,
            "end": b,
            "sig": f"p{a}-{b}",
            "uniform": False,
            "rest": {"start": rest[0], "end": rest[1], "sig": f"r{rest[0]}-{rest[1]}"},
        }

    joins = (
        [join(0, 60, (60, 200)), join(0, 120, (120, 200))]
        if where == "head"
        else [join(120, 200, (0, 120)), join(60, 200, (0, 60))]
    )
    return {"start": 0, "end": 200, "where": where, "sig": "whole", "joins": joins}


def approver(approved, row=7):
    """approve(kind, sig) approving only `approved`; .calls lists every (kind, sig) asked, in order."""

    def approve(kind, sig):
        approve.calls.append((kind, sig))
        return row if sig == approved else None

    approve.calls = []
    return approve


@budget(load="pure")
@pytest.mark.parametrize(
    ("where", "approved", "cut", "left"),
    [("head", "p0-120", (0, 120), (120, 200)), ("tail", "p60-200", (60, 200), (0, 60))],
    ids=["head-from-the-top", "tail-from-the-bottom"],
)
def test_match_blocks_cuts_at_the_first_join_whose_edge_piece_is_approved(where, approved, cut, left):
    """Joins are tried from the chapter edge inward after the whole block; the first approved piece is removed, the
    remainder is checked, then left as an edge block without probes."""
    approve = approver(approved)
    removed, blocks = su.match_blocks([glued_block(where)], approve)
    assert removed == [{"start": cut[0], "end": cut[1], "where": where, "decision": 7, "why": "match"}], removed
    assert [(b["start"], b["end"], b["where"], b["probes"], b["rest"]) for b in blocks] == [(*left, where, [], True)]
    nearer = glued_block(where)["joins"][0]["sig"]
    assert approve.calls == [
        ("block", "whole"),
        ("probe", nearer),
        ("probe", approved),
        ("block", f"r{left[0]}-{left[1]}"),
    ], "the nearer join's piece is checked first, the remainder as a block"


@budget(load="pure")
def test_match_blocks_without_an_approved_piece_cuts_nothing():
    """No piece matches: the block stays whole with its non-uniform pieces as probes, never a cut at a seam alone."""
    block = glued_block("head")
    block["joins"][0]["uniform"] = True
    approve = approver(None)
    removed, blocks = su.match_blocks([block], approve)
    assert removed == []
    assert approve.calls == [("block", "whole"), ("probe", "p0-60"), ("probe", "p0-120")]
    (b,) = blocks
    assert span(b["probes"]) == [(0, 120)], "a uniform piece is no probe"
    assert "joins" not in b
    assert set(b["probes"][0]) == {"start", "end", "sig"}


@budget(load="pure")
def test_a_block_matching_whole_is_removed_why_match():
    blocks = [{"start": 0, "end": 100, "where": "head", "sig": "a", "joins": []}]
    removed, left = su.match_blocks(blocks, lambda kind, sig: 4)
    assert removed == [{"start": 0, "end": 100, "where": "head", "decision": 4, "why": "match"}]
    assert left == []


@budget(1.0, load="fs")
def test_a_keep_decision_removes_nothing_and_leaves_nothing_undecided():
    """A keep on the cover block: nothing removed, and the decided block is no undecided unit."""
    cover = sig_of(Image.open(BANNER).convert("RGB"))
    got = su.decide(su.scan(strip_of([BANNER])), store(("block", cover, "keep", 2)))
    assert got == {"removed": [], "undecided": [], "kept_problem": None, "watermarks": []}, got


# ---------------------------------------------------------------- the real strip head (fixtures_junk)


@budget(2.0, load="fs")
def test_a_block_decision_removes_the_magus_band_of_a_real_end_card(tmp_path):
    """I Was the Real Head of the House c0003's D&C end card (fixture strip.png, its row 0 = strip row 111380) under
    2,400 px of story, the Magus band the crop cuts at 29 px extended to 149 px (its last row repeated) so it is an edge
    block: a block decision on the band removes it, with the blank rows above it (strip end), and the card's other
    blocks stay undecided."""
    a = np.asarray(Image.open(JUNK_FIX / "s5_2_real_head_c0003" / "strip.png").convert("RGB"))
    paths = [tmp_path / "001.png", tmp_path / "002.png"]
    fx.webtoon_page((720, 2400), seed=3).save(paths[0])
    Image.fromarray(np.vstack([a, np.repeat(a[-1:], 120, axis=0)])).save(paths[1])
    st = strip_of(paths)
    sc = su.scan(st)
    start = 111931 - 111380 + 2400
    (band,) = [b for b in sc["blocks"] if b["start"] == start]
    assert (band["end"], band["where"]) == (st.height, "tail"), band
    got = su.decide(sc, store(("block", band["sig"], "junk", 298)))
    assert {"start": start, "end": st.height, "why": "match"} in got["removed"], got["removed"]
    assert [r["why"] for r in got["removed"]] == ["join", "match"], "the blank run above the band joins the strip end"
    assert got["undecided"] and all(u["end"] <= got["removed"][0]["start"] for u in got["undecided"]), got["undecided"]
    assert got["kept_problem"] is None


# ---------------------------------------------------------------- watermarks (W2: 1 chapter)


def tsig(n):
    """A signature text of 1 tile distinct for each n (tiles of different n differ by far more than Hamming 6)."""
    return images.sig_to_str(((int.from_bytes(hashlib.sha256(str(n).encode()).digest()[:8], "big"),), 1.0))


def removed_band(decision, where="tail"):
    return {"where": where, "decision": decision, "key": tsig(decision)}


@budget(load="pure")
def test_an_id_twice_at_one_chapters_tail_is_a_watermark():
    """W2, The Unwelcome Guests of House Fildette ch149 (junk 966 twice at the tail)."""
    assert su.watermarks([removed_band(966), removed_band(966), removed_band(1)], []) == {966}


@budget(load="pure")
def test_an_id_once_at_the_tail_and_once_at_the_head_is_no_repeat():
    """The repeat rule counts tail bands only (adapted from the old 2-chapter case: the rule reads 1 chapter now)."""
    assert su.watermarks([removed_band(5), removed_band(5, where="head")], [tsig("other")]) == set()


@budget(load="pure")
def test_an_id_matching_a_middle_block_is_a_watermark():
    """W2, The Protection of Lariensa Gelinus ch89: the MangaStic watermark also sits mid-chapter."""
    assert su.watermarks([removed_band(855), removed_band(1)], [tsig(855)]) == {855}


@budget(load="pure")
def test_repeat_and_middle_watermarks_together():
    """A repeated id and an id matching a middle block are both watermarks."""
    removed = [removed_band(7), removed_band(7), removed_band(3)]
    assert su.watermarks(removed, [tsig(3), tsig("x")]) == {7, 3}


@budget(load="pure")
def test_ids_matching_no_middle_block_are_credits():
    """No repeat, no middle match: no watermark."""
    assert su.watermarks([removed_band(1), removed_band(2)], [tsig("other")]) == set()


# ---------------------------------------------------------------- middle blocks (scan)


@budget(2.0, load="fs")
def test_middle_blocks_leave_the_edge_blocks_out(tmp_path):
    """Only blocks reaching past the first 6,000 px and starting before the last 6,000 are middle blocks; signed."""
    mark = fx.blob(9, (200, 80))
    im = fx.stack(
        fx.webtoon_page((200, 5960), seed=1),  # 0-5960: head block
        (40,),
        mark,  # 6000-6080: middle
        (40,),
        fx.webtoon_page((200, 400), seed=2),  # 6120-6520: middle
        (40,),
        fx.webtoon_page((200, 5960), seed=4),  # 6560-12520: tail block (the tail region starts at 6560)
        (40,),
    )
    sc = su.scan(save_one(tmp_path, im))
    assert span(sc["blocks"]) == [(0, 5960), (6560, 12520)], span(sc["blocks"])
    assert len(sc["middle"]) == 2, sc["middle"]
    assert images.matches(images.sig_from_str(sc["middle"][0]), images.signature(mark)), "the mark is a middle block"


@budget(2.0, load="fs")
def test_uniform_middle_blocks_are_left_out(tmp_path):
    """A uniform block (channel range 10) mid-chapter is no middle block; the art block beside it is."""
    art = fx.webtoon_page((200, 400), seed=2)
    im = fx.stack(
        fx.webtoon_page((200, 5960), seed=1), (40,), flat_block(80), (40,), art, (40,),
        fx.webtoon_page((200, 5960), seed=4), (40,),
    )  # fmt: skip
    sc = su.scan(save_one(tmp_path, im))
    assert len(sc["middle"]) == 1
    assert images.matches(images.sig_from_str(sc["middle"][0]), images.signature(art))


@budget(3.0, load="fs")
def test_a_block_too_tall_to_match_a_band_is_never_rendered(tmp_path):
    """A middle block taller than any tail band can match is skipped before rendering (it rendered ~190 MB)."""
    im = fx.stack(
        fx.webtoon_page((200, 5960), seed=1), (40,), fx.webtoon_page((200, 6500), seed=2), (40,),
        fx.webtoon_page((200, 5960), seed=4), (40,),
    )  # fmt: skip
    st = save_one(tmp_path, im)
    with mock.patch.object(strip, "render_rows", wraps=strip.render_rows) as spy:
        sc = su.scan(st)
    assert sc["middle"] == []
    rendered = [c.args[2:4] for c in spy.call_args_list]
    assert (6000, 12500) not in rendered and rendered, rendered


# ---------------------------------------------------------------- the tail pass (W1 blank, W3 wedge), via decide


def tail_chapter(bands, pieces, middle=()):
    """A scan of a strip's tail without pixels: bands [(start, end, id) or (start, end, id, where)] are edge blocks
    a block decision (id) removes, pieces [(start, end, std, ink)] the measured ink between them; the spans are both,
    there are no gaps, the strip ends at the last of them. middle: ids whose band key also sits mid-chapter.
    Returns (scan, decide_unit)."""
    bands = [(*b, "tail")[:4] for b in bands]
    spans = sorted([(s, e) for s, e, *_ in bands] + [(s, e) for s, e, *_ in pieces])
    sc = {
        "layout": "strip",
        "width": 1000,
        "height": spans[-1][1],
        "gaps": [],
        "spans": spans,
        "blocks": [{"start": s, "end": e, "where": w, "sig": tsig(i), "joins": []} for s, e, i, w in bands],
        "measured": {
            f"{s}:{e}": {"std": std, "ink": ink, "uniform": False, "unsignable": False, "sig": tsig(f"p{s}")}
            for s, e, std, ink in pieces
        },
        "middle": [tsig(i) for i in middle],
    }
    return sc, store(*[("block", tsig(i), "junk", i) for _, _, i, _ in bands])


def piece(start, end, std=25.0, ink=200):
    return (start, end, std, ink)


def classes(bands, pieces, middle=()):
    """{index into pieces: "blank" | "wedge" | "review"} as decide settles them: dropped why blank / wedge, or
    undecided tail pieces; pieces before the credit start are absent."""
    got = su.decide(*tail_chapter(bands, pieces, middle))
    why = {(r["start"], r["end"]): r["why"] for r in got["removed"]}
    review = {(u["start"], u["end"]) for u in got["undecided"] if u["kind"] == "piece"}
    out = {}
    for i, (s, e, *_) in enumerate(pieces):
        if (s, e) in review:
            out[i] = "review"
        elif why.get((s, e)) in ("blank", "wedge"):
            out[i] = why[(s, e)]
    return out


@budget(load="pure")
def test_short_wedge_between_two_credits_dropped_a_taller_one_reviewed():
    """W3, This Girl Is A Little Wild Chapter 9: a 164 px line between credits is reviewed, a 110 px one dropped."""
    bands = [(1000, 1300, 1), (1410, 1700, 2), (1864, 2100, 3)]
    pieces = [piece(1300, 1410), piece(1700, 1864), piece(2100, 2400)]
    assert classes(bands, pieces) == {0: "wedge", 1: "review", 2: "review"}


@budget(load="pure")
@pytest.mark.parametrize(("height", "want"), [(129, "wedge"), (130, "review")], ids=["129-px", "130-px"])
def test_wedge_px_is_the_first_reviewed_height(height, want):
    """W3: shorter than WEDGE_PX 130 is a wedge; 130 px is reviewed."""
    got = classes([(1000, 1300, 1), (1300 + height, 1600, 2)], [piece(1300, 1300 + height)])
    assert got == {0: want}, "WEDGE_PX is the first reviewed height"


@budget(load="pure")
def test_a_short_piece_after_the_last_credit_is_no_wedge():
    """W3 needs a credit band on both sides: a 50 px piece closing the chapter is reviewed."""
    assert classes([(1000, 1300, 1)], [piece(1300, 1350)]) == {0: "review"}


@budget(load="pure")
def test_blank_piece_dropped_a_faint_line_is_not_blank():
    """W1: a flat piece between credits is dropped; a near-white piece with a faint line (ink > 40) is not blank."""
    bands = [(1000, 1300, 1), (1340, 1600, 2), (1900, 2000, 3)]
    pieces = [piece(1300, 1340, std=0.0, ink=2), piece(1600, 1900, std=2.6, ink=180), piece(2000, 2100, 0.4, 9)]
    assert classes(bands, pieces) == {0: "blank", 1: "review", 2: "blank"}


@budget(load="pure")
@pytest.mark.parametrize(
    ("std", "ink", "want"),
    [(2.9, 40, "blank"), (3.0, 0, "review"), (0.0, 41, "review")],
    ids=["std-under-3-ink-40", "std-3", "ink-41"],
)
def test_blank_thresholds(std, ink, want):
    """W1: luma std < BLANK_STD 3 and no pixel more than BLANK_INK 40 from the median."""
    assert classes([(1000, 1300, 1)], [piece(1300, 1600, std, ink)]) == {0: want}


@budget(load="pure")
def test_a_watermark_band_never_starts_the_credit_block():
    """W2, The Protection of Lariensa Gelinus ch89: the credit block starts at the first credit after the watermark."""
    bands = [(1000, 1100, 855), (1500, 1800, 1)]
    assert classes(bands, [piece(1100, 1500), piece(1800, 1900)], middle=[855]) == {1: "review"}


@budget(load="pure")
def test_without_the_watermark_rule_the_story_before_is_reviewed():
    """The same chapter with 855 no watermark: the block starts at 1,000 and the story piece is reviewed."""
    bands = [(1000, 1100, 855), (1500, 1800, 1)]
    assert classes(bands, [piece(1100, 1500), piece(1800, 1900)]) == {0: "review", 1: "review"}


@budget(load="pure")
def test_only_watermark_tail_bands_start_no_credit_block():
    """No non-watermark tail band: no tail pass for the chapter."""
    assert classes([(1000, 1100, 855)], [piece(1100, 1500)], middle=[855]) == {}


@budget(load="pure")
def test_a_head_band_starts_no_credit_block():
    """Only removed tail bands count: a head band before the pieces leaves them story."""
    assert classes([(0, 100, 1, "head")], [piece(100, 200)]) == {}


@budget(load="pure")
def test_the_credit_block_starts_at_the_earliest_tail_band_whatever_the_list_order():
    """Bands listed out of strip order: the block still starts at the band at 1,000."""
    bands = [(1500, 1800, 1), (1000, 1100, 2)]
    assert classes(bands, [piece(1100, 1500), piece(1800, 1900)]) == {0: "review", 1: "review"}


@budget(load="pure")
def test_a_piece_beside_a_watermark_band_is_no_wedge():
    """W3 needs credits, not watermarks, on both sides."""
    bands = [(1000, 1200, 1), (1300, 1350, 855), (1400, 1600, 2)]
    pieces = [piece(1200, 1300), piece(1350, 1400), piece(1600, 1700)]
    assert classes(bands, pieces, middle=[855]) == {0: "review", 1: "review", 2: "review"}


@budget(load="pure")
def test_an_edge_block_that_is_a_tail_piece_is_offered_once_as_a_piece():
    """An undecided edge block after the credit start is a tail piece: offered as the piece, not again as a block."""
    sc, decide_unit = tail_chapter([(1000, 1300, 1)], [piece(1300, 1500)])
    sc["blocks"].append({"start": 1300, "end": 1500, "where": "tail", "sig": tsig("p1300"), "joins": []})
    got = su.decide(sc, decide_unit)
    assert [(u["kind"], u["start"], u["end"]) for u in got["undecided"]] == [("piece", 1300, 1500)], got["undecided"]


@budget(load="pure")
def test_a_piece_decision_removes_a_tail_piece():
    """A junk piece decision drops the piece (why piece); a keep leaves it, decided."""
    sc, decide_unit = tail_chapter([(1000, 1300, 1)], [piece(1300, 1500)])

    def with_piece(verdict):
        return lambda kind, sig: (
            decide_unit(kind, sig) or ({"id": 8, "verdict": verdict, "key": sig} if kind == "piece" else None)
        )

    junk = su.decide(sc, with_piece("junk"))
    assert {"start": 1300, "end": 1500, "why": "piece"} in junk["removed"]
    assert junk["undecided"] == []
    keep = su.decide(sc, with_piece("keep"))
    assert all(r["why"] != "piece" for r in keep["removed"])
    assert keep["undecided"] == []


@budget(load="pure")
def test_an_unmeasured_tail_piece_fails_fast():
    """A tail piece the scan never measured is refused, never classified."""
    sc, decide_unit = tail_chapter([(1000, 1300, 1)], [piece(1300, 1500)])
    sc["measured"] = {}
    with pytest.raises(ValueError, match="1300-1500 was not measured"):
        su.decide(sc, decide_unit)


@budget(2.0, load="fs")
def test_pieces_after_the_first_tail_band_measured(tmp_path):
    """Kept rows from the credit start on, split at every gap: a faint dot's rows are a measured piece of their own, no
    W1 blank, the gaps around it blank; with its gutters it is WEDGE_PX or taller: reviewed, not a wedge."""
    speck = np.full((60, 200, 3), 255, np.uint8)
    speck[30, 100:102] = 0  # a faint line's dot: std < 3, ink > 40
    im = fx.stack(
        fx.blob(1, (200, 600)), (40,), fx.blob(2, (200, 150)), (40,), Image.fromarray(speck), (40,),
        fx.blob(3, (200, 150)), (40,),
    )  # fmt: skip
    st = sliced(tmp_path, im)
    blocks = strip.content_blocks(st, strip.find_gaps(st))
    assert len(blocks) == 4, blocks
    (s1, e1), (dot_s, dot_e), (s2, e2) = blocks[1], blocks[2], blocks[3]
    sc = su.scan(st)
    dot = sc["measured"][f"{dot_s}:{dot_e}"]
    assert not su.is_blank({**dot, "gap": False, "blank": False}), "the dot's rows alone: ink, no W1 blank"
    assert (dot_e - dot_s) + (dot_s - e1) + (s2 - dot_e) >= su.WEDGE_PX
    sigs = {(b["start"], b["end"]): b["sig"] for b in sc["blocks"]}
    got = su.decide(sc, store(("block", sigs[(s1, e1)], "junk", 1), ("block", sigs[(s2, e2)], "junk", 2)))
    assert [(u["start"], u["end"]) for u in got["undecided"] if u["kind"] == "piece"] == [(dot_s, dot_e)], got
    assert not [r for r in got["removed"] if r["why"] == "blank"], "the gap pieces' drops are settled by the join rule"


# ---------------------------------------------------------------- tail pieces, W1 and W3 (direct)


def layout(height, spans, width=1000):
    """A Layout whose gaps are the walkable runs of at least 1 gutter between and around the spans."""
    lay = su.Layout({"height": height, "width": width, "spans": spans, "gaps": []})
    gaps = [r for r in lay.runs() if r[1] - r[0] >= su.gutter(width)]
    return su.Layout({"height": height, "width": width, "spans": spans, "gaps": gaps})


def band(start, end, decision=1, where="tail", why="match"):
    return {"start": start, "end": end, "where": where, "decision": decision, "why": why}


@budget(load="pure")
def test_tail_pieces_split_at_every_gap():
    lay = layout(20000, [(0, 18000), (18100, 18200), (18300, 18310), (18320, 18400), (18600, 18700), (19000, 20000)])
    removed = [band(18100, 18200), band(18600, 18700, 2), band(19000, 20000, 3)]
    pieces = su.tail_pieces(lay, removed, 18100)
    assert [(p["start"], p["end"], p["gap"]) for p in pieces] == [
        (18200, 18300, True),
        (18300, 18400, False),
        (18400, 18600, True),
        (18700, 19000, True),
    ], "the 10 px run inside 18300-18400 is no gap; removed rows are no piece"


@budget(load="pure")
def test_w1_drops_gaps_blank_uniform_and_unsignable_pieces():
    p = {"start": 0, "end": 50, "gap": False, "blank": False, "uniform": False, "unsignable": False, "std": 30.0}
    p["ink"] = 200
    assert not su.is_blank(p), "an inked piece is no blank"
    for k in ("gap", "blank", "uniform", "unsignable"):
        assert su.is_blank(dict(p, **{k: True})), f"a {k} piece is blank"
    assert su.is_blank(dict(p, std=1.0, ink=20)), "a flat piece is blank"


@budget(load="pure")
def test_w3_reads_the_nearest_non_blank_neighbours():
    """This Girl Is A Little Wild ch9: a short line between two credit bands is a wedge; a blank piece between it and a
    band does not stop that; a removed non-band range does."""
    piece_ = {"start": 1100, "end": 1130, "gap": False, "blank": False, "uniform": False, "unsignable": False}
    piece_.update(std=40.0, ink=200)
    blank = dict(piece_, start=1130, end=1180, gap=True)
    assert su.tail_classes([piece_, blank], [(1000, 1100), (1180, 1300)], []) == {0: "wedge", 1: "blank"}
    assert su.tail_classes([piece_, blank], [(1000, 1100)], [(1180, 1300)]) == {0: "review", 1: "blank"}
    tall = dict(piece_, end=1100 + su.WEDGE_PX)
    assert su.tail_classes([tall], [(1000, 1100), (tall["end"], 1300)], []) == {0: "review"}


@budget(load="pure")
def test_w3_measures_a_piece_with_its_gutters():
    """This Girl Is A Little Wild ch9: an 84 px line + 2 x 40 px gutters = 164 px between 2 credit bands is reviewed;
    a 30 px line with 2 x 20 px gutters (70 px) is a wedge."""
    line = {"start": 1140, "end": 1224, "gap": False, "blank": False, "uniform": False, "unsignable": False}
    line.update(std=40.0, ink=200)
    gaps = [dict(line, start=1100, end=1140, gap=True), dict(line, start=1224, end=1264, gap=True)]
    assert su.tail_classes([gaps[0], line, gaps[1]], [(1000, 1100), (1264, 1400)], []) == {
        0: "blank",
        1: "review",
        2: "blank",
    }
    short = dict(line, start=1120, end=1150)
    gaps = [dict(line, start=1100, end=1120, gap=True), dict(line, start=1150, end=1170, gap=True)]
    assert su.tail_classes([gaps[0], short, gaps[1]], [(1000, 1100), (1170, 1300)], []) == {
        0: "blank",
        1: "wedge",
        2: "blank",
    }


@budget(load="pure")
def test_measure_piece_reads_std_ink_and_signs_only_signable_pieces():
    """A uniform run is uniform with no signature; an inked one is signed."""
    flat = su.measure_piece(Image.new("RGB", (200, 60), (250, 250, 250)))
    assert (flat["uniform"], flat["sig"], flat["std"], flat["ink"]) == (True, None, 0.0, 0)
    inked = su.measure_piece(fx.blob(1, (200, 150)))
    assert not inked["uniform"]
    assert not inked["unsignable"]
    assert inked["sig"]
    assert inked["ink"] > su.BLANK_INK


# ---------------------------------------------------------------- the join rule and settle


@budget(load="pure")
def test_the_join_rule_keeps_one_gutter_centred_takes_strip_ends_and_slivers():
    lay = layout(10000, [(0, 4000), (4100, 4200), (4300, 10000)])
    g = su.gutter(1000)
    mid = su.join_rule(lay, [band(4100, 4200)], g)
    assert sum(r["end"] - r["start"] for r in mid) == 200 - g, "C = 200 px: C - G removed"
    assert span(mid) == [(4000, 4094), (4206, 4300)], "G kept in the middle"
    end = su.join_rule(lay, [band(4300, 10000)], g)
    assert span(end) == [(4200, 4300)], "a join at the strip end: C removed whole"
    lay = layout(10000, [(0, 4000), (4005, 4100), (4105, 10000)])
    sliver = su.join_rule(lay, [band(4005, 4100)], g)
    assert span(sliver) == [(4000, 4005), (4100, 4105)], "C < G: removed whole"
    assert all(r["why"] == "join" and r["decision"] is None for r in sliver)


@budget(load="pure")
def test_kept_tail_pieces_keep_one_gutter_between_them_after_w1_drops_the_gap():
    """W1 drops every gap after the credit start; the join rule counts those blank rows, so kept pieces keep 1 gutter
    between them, and 1 between the story and the first piece."""
    lay = layout(20000, [(0, 17900), (18000, 18100), (18200, 18300), (18400, 18500), (19000, 20000)])
    g = su.gutter(1000)

    def drop(s, e):
        return {"start": s, "end": e, "where": "tail", "decision": None, "why": "blank"}

    removed = [band(18000, 18100), drop(18100, 18200), drop(18300, 18400), drop(18500, 19000), band(19000, 20000)]
    final = su.settle(lay, removed, g)
    taken = span(final)

    def left(a, b):
        return sum(e - s for s, e in su.uncovered(a, b, taken))

    assert (left(17900, 18200), left(18300, 18400), left(18500, 20000)) == (g, g, 0), final
    assert not [r for r in final if r["why"] == "blank"], "a gap's drop is settled by the join rule"
    # a W1 drop that is no gap piece (5 blank rows between a band and a kept piece) stays `why: blank`
    lay = layout(20000, [(0, 17900), (18000, 18100), (18105, 18300), (19000, 20000)])
    short = su.settle(lay, [band(18000, 18100), drop(18100, 18105), band(19000, 20000)], g)
    assert [(r["start"], r["end"]) for r in short if r["why"] == "blank"] == [(18100, 18105)], short


@budget(load="pure")
def test_uncovered_lists_the_parts_outside_every_range():
    assert su.uncovered(0, 100, [(50, 60), (10, 20), (55, 70)]) == [(0, 10), (20, 50), (70, 100)]
    assert su.uncovered(0, 100, [(0, 100)]) == []


# ---------------------------------------------------------------- the kept check


@budget(load="pure")
def test_the_kept_check_covers_plain_matches_and_tail_drops():
    lay = layout(3000, [(0, 1400), (1500, 3000)])
    assert "1400 px" in su.kept_problem(lay, [band(1400, 3000)]), "under MIN_PAGE from a longer strip is refused"
    blank_only = layout(3000, [(0, 100), (1600, 3000)])
    got = su.kept_problem(blank_only, [band(0, 100, where="head"), band(1600, 3000, None, why="blank")])
    assert "only blank" in got, "only blank rows kept is refused"
    assert su.kept_problem(lay, [band(2900, 3000)]) is None


@budget(load="pure")
def test_the_kept_check_spares_a_chapter_already_shorter_than_min_page():
    """A one-page announcement strip (800 px < MIN_PAGE) with a scanlator head band keeps its removal."""
    assert su.kept_problem(layout(800, [(0, 100), (150, 800)]), [band(0, 100, where="head")]) is None
    blank = layout(800, [(0, 100), (300, 400)])
    assert "only blank" in su.kept_problem(blank, [band(0, 100, where="head"), band(300, 400)])


# ---------------------------------------------------------------- signable


@budget(load="pure")
def test_uniform_and_unsignable_images_have_no_signature():
    assert su.signable(Image.new("RGB", (100, 100), (90, 90, 90))) is None, "uniform: no signature"
    assert su.signable(fx.blob(1, (200, 150))) == sig_of(fx.blob(1, (200, 150)))


# ---------------------------------------------------------------- watermarks the series knows


@budget(load="pure")
def test_a_key_the_series_knows_as_a_watermark_is_one_here_too():
    """W2 across chapters (the bulk converter holds it for the whole series): band 855 sits once at this chapter's
    tail and nowhere in its middle, but an earlier chapter showed it to be a watermark."""
    known = [images.sig_from_str(tsig(855))]
    assert su.watermarks([removed_band(855), removed_band(1)], [], known) == {855}


@budget(load="pure")
def test_an_unsignable_middle_block_makes_no_watermark():
    flat, near_flat = images.sig_to_str(((0,), 1.0)), images.sig_to_str(((7,), 1.0))
    band = {"where": "tail", "decision": 9, "key": near_flat}
    assert su.watermarks([band], [flat]) == set(), "a flat middle block matches nothing"


@budget(load="pure")
def test_a_remembered_watermark_starts_no_credit_block_and_decide_names_the_watermark_keys():
    """The chapter of test_without_the_watermark_rule_the_story_before_is_reviewed, in a series that knows 855."""
    sc, decide_unit = tail_chapter([(1000, 1100, 855), (1500, 1800, 1)], [piece(1100, 1500), piece(1800, 1900)])
    got = su.decide(sc, decide_unit, [images.sig_from_str(tsig(855))])
    reviewed = [(u["start"], u["end"]) for u in got["undecided"] if u["kind"] == "piece"]
    assert reviewed == [(1800, 1900)], "the story after the watermark is no tail piece"
    assert got["watermarks"] == [tsig(855)], "decide names the key for the series to remember"
    assert su.decide(sc, decide_unit)["watermarks"] == [], "without the series' memory 855 is a credit here"
