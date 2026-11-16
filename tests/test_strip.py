"""Strip model, gaps, cuts, layout, slices (strip; contracts section 1/5), on synthetic chapters shaped like the
library's: panels separated by gutters and uploaded as fixed-height slices, pages cut into page + sliver, stitched
pages; and strip segmentation (spans, walkable runs, edge regions) on the real cached blank-row profiles of two
chapters (tests/fixtures_pages/junk/). Ported from the bulk converter's tests/test_restitch.py and
tests/test_segmentation.py (the tests of its junk modules are not ported)."""

import base64
import itertools
import json
import shutil
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import db, images, strip

from tests import pages as fx
from tests.builders import add_series, site_cfg
from tests.timing import budget

RC, SC = strip.RESTITCH, strip.SLICE_REPAIR
MAX_SEAM = SC["max_seam_score"]
JUNK_FIX = fx.PAGE_FIX / "junk"


# ---------------------------------------------------------------- shared builders


@pytest.fixture(scope="module")
def cached(tmp_path_factory):
    """cached(out_dir, key, build, limit=None): fresh copies in out_dir of the files build(dir) -> [paths] wrote, built
    only once per key in this module (the synthetic pages are deterministic, and generating + encoding them was most of
    the module's run time); the first `limit` copies, in build's order."""
    root, built = tmp_path_factory.mktemp("strip-cache"), {}

    def copies(out_dir, key, build, limit=None):
        if key not in built:
            src = root / str(len(built))
            src.mkdir()
            built[key] = (src, [p.relative_to(src) for p in build(src)])
        src, rels = built[key]
        out = []
        for rel in rels[:limit]:
            dst = Path(out_dir) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            out.append(Path(shutil.copyfile(src / rel, dst)))
        return out

    return copies


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


def slice_strip(im, height, out_dir, fmt="WEBP"):
    """Upload the strip as fixed-height slices (cuts land mid-panel), like scanlation sites do."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for n, top in enumerate(range(0, im.height, height), 1):
        p = out_dir / f"{n:03d}.{'webp' if fmt == 'WEBP' else 'png'}"
        im.crop((0, top, im.width, min(im.height, top + height))).save(
            p, fmt, **({"quality": 90} if fmt == "WEBP" else {})
        )
        pages.append(p)
    return pages


def save_pages(out_dir, ims, stem=""):
    """Each image saved as out_dir/<stem>NNN.png (from 000); the paths in order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, im in enumerate(ims):
        paths.append(out_dir / f"{stem}{i:03d}.png")
        im.save(paths[-1])
    return paths


def strip_of(pages):
    """A chapter's Strip as the plan builds it: header sizes and each page's row profile (the plan scan's one
    decode)."""
    return strip.make_strip(
        pages, [images.page_size(p) for p in pages], [strip.page_rows(images.open_image(p)[0]) for p in pages]
    )


def strip_plan(st):
    """A Strip's plan with no junk removed: cut_strip over its gaps. Returns cut_strip's {"kept", "cuts", "forced",
    "length"} + "strip"."""
    return dict(strip.cut_strip(st.height, strip.find_gaps(st), []), strip=st)


def render_cuts(width, pages, kept, cuts, x_dir, out_dir):
    """Execution of a strip / split plan: strip.render over the planned cuts. pages: [{"name", "top", "th"}]."""
    outputs = [{"name": strip.output_name(n), "rows": [a, b]} for n, (a, b) in enumerate(itertools.pairwise(cuts), 1)]
    return strip.render(outputs, x_dir, out_dir, {"width": width, "pages": pages, "kept": kept})


def render_strip(pl, out_dir):
    st = pl["strip"]
    pages = [{"name": Path(p["path"]).name, "top": p["top"], "th": p["th"]} for p in st.pages]
    return render_cuts(st.width, pages, pl["kept"], pl["cuts"], Path(st.pages[0]["path"]).parent, out_dir)


def stacked(paths):
    """The pages' pixels stacked top to bottom (int, for exact comparisons)."""
    return np.vstack([np.asarray(Image.open(p).convert("RGB")) for p in paths]).astype(int)


def rows_strip(sizes, uniform_rows, rows_per_page):
    """A Strip from page sizes and row profiles only (no files): page k has rows_per_page[k] analysis rows, True
    (uniform) at the indexes uniform_rows[k]."""
    rows = []
    for n, on in zip(rows_per_page, uniform_rows, strict=True):
        r = np.zeros(n, bool)
        r[list(on)] = True
        rows.append(r)
    return strip.make_strip([f"{k:03d}.png" for k in range(len(sizes))], sizes, rows)


# ---------------------------------------------------------------- layout


def manga_chapter(out_dir, n=6):
    """Separate pages: unrelated art meets at every join."""
    return save_pages(out_dir, [fx.webtoon_page((600, 900), seed=i * 7) for i in range(n)], "m")


def joins_chapter(out_dir, slices, others):
    """A 400 px wide chapter: `slices` 300 px slices of one smooth strip (continuous joins), then `others` unrelated
    pages (discontinuous joins)."""
    im = fx.webtoon_page((400, 300 * slices), seed=0)
    ims = [im.crop((0, 300 * k, 400, 300 * k + 300)) for k in range(slices)]
    return save_pages(out_dir, ims + [fx.webtoon_page((400, 300), seed=7 * (k + 1)) for k in range(others)])


def webtoon_chapter(out_dir):
    im, _ = panel_strip([900, 1400, 700, 1600])
    return slice_strip(im, 800, out_dir, fmt="PNG")


def metadata_flag(source, claim, value, share):
    return (
        f"layout-metadata: {source} says {claim} ({value}) but continuous joins {share}: check the Kavita series "
        "match and the scraper's source"
    )


@budget(1.0, load="fs")
def test_webtoon_slices_are_a_strip_by_geometry_alone(tmp_path):
    """Webtoon slices with no genre at all (most of the census' strip series carry no Kavita long-strip genre):
    strip."""
    layout, why, flags = strip.classify_layout([webtoon_chapter(tmp_path / "w")])
    assert (layout, flags) == ("strip", []), "geometry alone decides the layout"
    assert why.startswith("continuous joins "), "the reason names the continuous share"


@budget(1.0, load="fs")
def test_page_format_with_a_manga_origin_is_pages_unflagged(tmp_path):
    """Gal Maid & Villainess (page format, Kavita `Manga`): pages, no flag."""
    got = strip.classify_layout([manga_chapter(tmp_path)], None, ["Manga", "Comedy", "Romance"])[::2]
    assert got == ("pages", []), "a Manga origin agreeing with page geometry is not flagged"


@budget(1.0, load="fs")
def test_a_webtoon_with_a_manga_origin_is_a_strip_and_flagged(tmp_path):
    """Leviathan (Kavita `Manga`, 100 % continuous): strip by geometry, the Kavita origin claim flagged."""
    layout, _, flags = strip.classify_layout([joins_chapter(tmp_path, slices=5, others=0)], None, ["Manga", "Action"])
    assert layout == "strip", "geometry decides"
    assert flags == [metadata_flag("Kavita origin", "pages", "Manga", "100%")], "the disagreeing origin is flagged"


@budget(1.0, load="fs")
def test_a_long_strip_genre_on_page_format_is_pages_and_flagged(tmp_path):
    """A Kavita long-strip genre (matched case-insensitively after stripping) no longer restitches: page geometry says
    pages, the claim is flagged with the genre as Kavita spells it."""
    layout, _, flags = strip.classify_layout([manga_chapter(tmp_path)], None, [" Long Strip"])
    assert layout == "pages", "geometry decides"
    assert flags == [metadata_flag("Kavita genre", "strip", "Long Strip", "0%")], "the genre claim is flagged"


@budget(1.0, load="fs")
def test_several_origin_genres_claim_nothing(tmp_path):
    """Manga + Manhwa on one series: no origin claim, so no flag on page format."""
    assert strip.classify_layout([manga_chapter(tmp_path)], None, ["Manga", "Manhwa"])[2] == [], "2 origins: no claim"


@budget(1.0, load="fs")
def test_override_strip_never_silences_the_metadata_check(tmp_path):
    """Blue Glass shape (page-format pages, Kavita `Manhwa`) under `layout: Strip`: the override decides (case-folded);
    the claim is checked against the geometry's verdict (pages): Manhwa disagrees and is flagged, Manga is not."""
    pages = [manga_chapter(tmp_path)]
    layout, why, flags = strip.classify_layout(pages, {"layout": "Strip"}, ["Manhwa"])
    assert (layout, flags) == ("strip", [metadata_flag("Kavita origin", "strip", "Manhwa", "0%")]), (
        "the override decides; the claim is checked against geometry"
    )
    assert why.startswith("override layout: strip"), "the reason names the override"
    assert strip.classify_layout(pages, {"layout": "strip"}, ["Manga"])[::2] == ("strip", []), (
        "a claim agreeing with geometry is not flagged"
    )


@budget(1.0, load="fs")
def test_override_pages_beats_strip_geometry(tmp_path):
    """`layout: pages` forces page format on webtoon slices."""
    assert strip.classify_layout([webtoon_chapter(tmp_path / "w")], {"layout": "pages"})[0] == "pages", (
        "the override decides"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("override", "want"),
    [({}, None), ({"layout": None}, None), ({"layout": " Pages "}, "pages"), ({"layout": "STRIP"}, "strip")],
    ids=["absent", "null", "padded-pages", "upper-strip"],
)
def test_override_layout_values(override, want):
    """The overrides `layout:` is "strip", "pages" or absent, read stripped and case-folded."""
    assert strip.override_layout(override) == want, "read stripped and case-folded"


@budget(load="pure")
def test_unknown_override_layout_is_refused():
    """Any other `layout:` value is a config error, not page format."""
    with pytest.raises(ValueError, match="overrides layout must be 'strip' or 'pages', not 'webtoon'"):
        strip.classify_layout([], {"layout": "webtoon"})


@budget(1.0, load="fs")
def test_continuous_share_at_the_threshold_is_a_strip(tmp_path):
    """3 of 4 joins continuous is STRIP_SHARE 0.75 exactly: strip (>= rule), the share in the reason."""
    layout, why, _ = strip.classify_layout([joins_chapter(tmp_path, slices=4, others=1)])
    assert layout == "strip", "a share of exactly 0.75 is a strip"
    assert "75%" in why, "the reason names the share"


@budget(1.0, load="fs")
def test_half_continuous_joins_are_page_format(tmp_path):
    """2 of 4 joins continuous (0.5): page format; page manga measure up to 0.71 (Immortal Hounds)."""
    layout, _, _ = strip.classify_layout([joins_chapter(tmp_path, slices=3, others=2)])
    assert layout == "pages", "a share of 0.5 is below STRIP_SHARE"


@budget(1.0, load="fs")
def test_continuous_share_below_the_threshold_is_page_format(tmp_path):
    """1 of 4 joins continuous: page format, share 25% in the reason."""
    layout, why, _ = strip.classify_layout([joins_chapter(tmp_path, slices=2, others=3)])
    assert layout == "pages", "a share of 0.25 is below STRIP_SHARE"
    assert "25%" in why, "the reason names the share"


@budget(1.0, load="fs")
def test_a_one_page_chapter_has_no_joins_and_is_left_out_of_the_median(tmp_path):
    """Median over chapters with joins only: a 1-page sample chapter does not pull 0.75 down to 0.375."""
    one = save_pages(tmp_path / "one", [fx.webtoon_page((400, 300), seed=3)])
    layout, why, _ = strip.classify_layout([one, joins_chapter(tmp_path / "c", slices=4, others=1)])
    assert layout == "strip", "a chapter without joins is left out of the median"
    assert "75%" in why, "a chapter without joins is left out of the median"


@budget(1.0, load="fs")
def test_a_join_between_two_blank_edges_is_continuous(tmp_path):
    """Both edge rows blank (gap_row_std) counts as continuous even between unrelated art; one blank side does not."""
    a, b, c = (np.asarray(fx.webtoon_page((400, 300), seed=s)).copy() for s in (1, 8, 15))
    a[-20:] = 255
    b[:20] = 255
    pages = save_pages(tmp_path, [Image.fromarray(x) for x in (a, b, c)])
    assert "100%" in strip.classify_layout([pages[:2]])[1], "blank bottom meeting blank top is continuous"
    assert "50%" in strip.classify_layout([pages])[1], "blank top of b ... art bottom of b meets c: 1 of 2"


# ---------------------------------------------------------------- strip geometry and gaps


@budget(load="pure")
def test_strip_layout_scales_pages_to_the_most_common_width():
    """A 400 px page among 800 px pages is laid at 800 px: twice its height."""
    assert strip.strip_layout([(800, 1000), (400, 500), (800, 1000)]) == (
        800,
        [
            {"w": 800, "h": 1000, "top": 0, "th": 1000},
            {"w": 400, "h": 500, "top": 1000, "th": 1000},
            {"w": 800, "h": 1000, "top": 2000, "th": 1000},
        ],
        3000,
    ), "pages are scaled to the most common width"


@budget(load="pure")
def test_strip_layout_at_a_given_width():
    """An explicit target width overrides the most common one."""
    assert strip.strip_layout([(800, 1000)], 400) == (400, [{"w": 800, "h": 1000, "top": 0, "th": 500}], 500), (
        "a given width wins"
    )


@budget(load="pure")
def test_rows_map_to_strip_px_within_their_own_page():
    """Pages of 1000 and 1003 px with 100 analysis rows each: row starts follow each page (no global-ratio drift)."""
    st = rows_strip([(1000, 1000), (1000, 1003)], [(), ()], [100, 100])
    assert (st.width, st.height, len(st.uniform), st.row_px) == (1000, 2003, 200, pytest.approx(10.015)), "geometry"
    assert [st.row_y[0], st.row_y[100], st.row_y[150], st.row_y[-1]] == [0, 1000, pytest.approx(1501.5), 2003], (
        "rows map within their own page"
    )
    assert [p["path"] for p in st.pages] == ["000.png", "001.png"], "pages keep their paths"


@budget(load="pure")
@pytest.mark.parametrize(
    ("run", "want"), [(range(500, 512), [(500, 512)]), (range(500, 511), [])], ids=["12-rows", "11-rows"]
)
def test_a_gap_needs_gap_min_rows_uniform_rows(run, want):
    """1000 px wide, 1 px per analysis row: gap_min_rows 12 is the shortest gap."""
    assert strip.find_gaps(rows_strip([(1000, 1000)], [run], [1000])) == want, "gap_min_rows 12 at 1000 px"


@budget(load="pure")
@pytest.mark.parametrize(
    ("run", "want"), [(range(100, 103), [(200, 206)]), (range(100, 102), [])], ids=["3-rows", "2-rows"]
)
def test_gap_min_rows_is_measured_at_1000_px_width(run, want):
    """At 500 px wide, 12 rows at 1000 w are 6 px: 3 analysis rows of 2 px."""
    assert strip.find_gaps(rows_strip([(500, 1000)], [run], [500])) == want, "gap_min_rows scales with width"


@budget(load="pure")
def test_a_gap_runs_across_a_page_join():
    """5 uniform rows at the bottom of one page and 7 at the top of the next are one 12 px gap."""
    st = rows_strip([(1000, 1000), (1000, 1000)], [range(995, 1000), range(7)], [1000, 1000])
    assert strip.find_gaps(st) == [(995, 1007)], "a gap runs across a page join"


@budget(load="pure")
def test_a_gap_at_the_strip_end_is_found():
    """A uniform run reaching the last row ends at the strip height."""
    assert strip.find_gaps(rows_strip([(1000, 1000)], [range(980, 1000)], [1000])) == [(980, 1000)], (
        "a gap may end the strip"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("gaps", "want"),
    [
        ([], [(0, 1000)]),
        ([(200, 300)], [(0, 200), (300, 1000)]),
        ([(0, 100), (400, 500), (900, 1000)], [(100, 400), (500, 900)]),
    ],
    ids=["no-gap", "one-gap", "gaps-at-both-ends"],
)
def test_content_blocks_lie_between_gaps(gaps, want):
    """Blocks are the content between gaps; a gap at either strip end leaves no empty block."""
    assert strip.content_blocks(rows_strip([(1000, 1000)], [()], [1000]), gaps) == want, "blocks between gaps"


@budget(15.0, load="fs")
def test_restitched_pages_cut_in_gutters_and_keep_every_row(tmp_path):
    """9-panel strip in 1,000 px WebP slices: cuts in the gutters, every page <= max_page, pixels carried untouched."""
    im, gutters = panel_strip([900, 1400, 700, 1600, 1100, 800, 1300, 1000, 900])
    pages = slice_strip(im, 1000, tmp_path / "in")  # 1,000 px slices cut panels in half
    pl = strip_plan(strip_of(pages))
    assert pl["length"] == im.height, "nothing removed"
    out = render_strip(pl, tmp_path / "out")
    heights = [Image.open(p).height for p in out]
    assert sum(heights) == im.height, "output heights sum to the strip height (no block removed)"
    assert max(heights) <= RC["max_page"], "every page within max_page"
    assert [c for c in pl["cuts"][1:-1] if not any(s <= c < e for s, e in gutters)] == [], "every cut in a gutter"
    assert np.abs(stacked(out) - stacked(pages)).max() == 0, "pixels carried over untouched before encoding"


def drifting_slices(out_dir):
    """720 px strip of 40 slices of 1000 px then 40 of 1003 px, a 40 px gutter at 39980-40020 deep in the chapter."""
    rows = [np.asarray(fx.webtoon_page((720, 1000 if i < 40 else 1003), seed=i)) for i in range(80)]
    a = np.vstack(rows).copy()
    a[39980:40020] = 255
    pages, y = [], 0
    for i, r in enumerate(rows):
        pages.append(out_dir / f"{i:03d}.png")
        Image.fromarray(a[y : y + len(r)]).save(pages[-1], compress_level=1)
        y += len(r)
    return pages


@budget(8.0, load="fs")
def test_gaps_located_exactly_despite_varying_slice_heights(tmp_path):
    """Per-page row mapping: the deep gutter is found where it is (one global ratio drifted it by ~25 px)."""
    gaps = strip.find_gaps(strip_of(drifting_slices(tmp_path)))
    g = [s for s in gaps if 39000 < s[0] < 41000]
    assert len(g) == 1, f"one gap near the gutter: {gaps[-3:]}"
    s, e = g[0]
    assert s >= 39975, f"the gap is where the gutter is: {g}"
    assert e <= 40025, f"the gap is where the gutter is: {g}"
    assert 39980 <= (s + e) // 2 < 40020, f"the gap is where the gutter is: {g}"


@budget(2.0, load="fs")
def test_mixed_widths_scaled_to_the_common_width(tmp_path):
    """Military Phoenix: a slice downscaled to 568 px is rendered at the chapter's 700 px."""
    im, _ = panel_strip([1200, 1200, 1200], width=700)
    pages = slice_strip(im, 1000, tmp_path / "in", fmt="PNG")
    Image.open(pages[1]).resize((568, 811)).save(pages[1])
    pl = strip_plan(strip_of(pages))
    assert pl["strip"].width == 700, "the strip is at the most common width"
    assert {Image.open(p).width for p in render_strip(pl, tmp_path / "out")} == {700}, "every page at that width"


# ---------------------------------------------------------------- partition


@budget(load="pure")
def test_every_page_within_limits_and_on_gaps():
    """Gaps every 1,300 px over 30,000 px: no forced cut, every cut on a gap, every page in [min_page, max_page]."""
    gaps = [(c, 80) for c in range(1200, 30000, 1300)]
    cuts, forced = strip.plan_cuts(30000, gaps)
    lens = [b - a for a, b in itertools.pairwise(cuts)]
    assert [n for n in lens if not RC["min_page"] <= n <= RC["max_page"]] == [], f"pages within limits: {lens}"
    assert forced == [], "no forced cut where gaps fit"
    assert set(cuts[1:-1]) <= {c for c, _ in gaps}, "every cut on a gap"


@budget(load="pure")
def test_prefers_lengths_near_the_middle():
    """Gaps every 500 px: every page within 1,000 px of the 3,250 middle."""
    gaps = [(c, 80) for c in range(500, 20000, 500)]
    cuts, _ = strip.plan_cuts(20000, gaps)
    mid = (RC["min_page"] + RC["max_page"]) / 2
    assert [b - a for a, b in itertools.pairwise(cuts) if abs(b - a - mid) > 1000] == [], "lengths near the middle"


@budget(load="pure")
def test_the_stronger_of_two_equally_placed_gaps_wins():
    """Gaps 50 px either side of the middle: the run-length 80 gap beats the 20 one."""
    assert strip.plan_cuts(6500, [(3200, 20), (3300, 80)]) == ([0, 3300, 6500], []), "the stronger gap wins"


@budget(load="pure")
def test_no_gap_cuts_through_art_at_exactly_max():
    """Can We Become a Family: continuous background, no gutter for >5,000 px: forced cuts at 5,000 and 10,000."""
    assert strip.plan_cuts(12000, []) == ([0, 5000, 10000, 12000], [5000, 10000]), "forced cuts at max_page"


@budget(load="pure")
@pytest.mark.parametrize(
    ("length", "want"),
    [
        (5200, ([0, 2600, 5200], [2600])),
        (6400, ([0, 3200, 6400], [3200])),
        (5001, ([0, 2500, 5001], [2500])),
        (11000, ([0, 3667, 7333, 11000], [3667, 7333])),
        (6500, ([0, 5000, 6500], [5000])),
    ],
    ids=["5200", "6400", "max-plus-one", "three-pieces", "remainder-at-min-cut-at-max"],
)
def test_gapless_run_split_evenly_when_max_cut_strands_a_short_page(length, want):
    """5,000 + 200 would leave a 200 px page cut through the art: the gapless run is split evenly instead."""
    assert strip.plan_cuts(length, []) == want, "a stranded short page splits the run evenly"


@budget(load="pure")
@pytest.mark.parametrize(
    ("length", "gaps"),
    [(2600, [(1300, 80)]), (5000, []), (900, [])],
    ids=["short-with-gap", "exactly-max", "under-min"],
)
def test_a_chapter_up_to_max_page_is_one_page(length, gaps):
    """Nothing to cut at or under max_page, gaps or not."""
    assert strip.plan_cuts(length, gaps) == ([0, length], []), "at or under max_page: one page"


@budget(load="pure")
def test_a_gap_that_would_strand_a_short_last_page_is_not_the_cut():
    """Every page within [min_page, max_page]; a gap at 5,000 of 6,200 px leaves a 1,200 px page."""
    cuts, _ = strip.plan_cuts(6200, [(5000, 80)])
    lens = [b - a for a, b in itertools.pairwise(cuts)]
    assert [n for n in lens if not RC["min_page"] <= n <= RC["max_page"]] == [], (
        f"pages {lens}: only a chapter shorter than min_page may be one short page"
    )


@budget(load="pure")
def test_cut_strip_takes_removed_ranges_out_and_maps_gaps_into_kept_px():
    """A removed head block of 1,000 px: the gap at 4,000-4,100 is a candidate at 3,050 of the 9,000 kept px."""
    assert strip.cut_strip(10000, [(4000, 4100)], [{"start": 0, "end": 1000}]) == {
        "kept": [(1000, 10000)],
        "cuts": [0, 3050, 6025, 9000],
        "forced": [6025],
        "length": 9000,
    }, "gaps map into kept px"


@budget(load="pure")
def test_a_gaps_strength_is_its_run_length():
    """Gaps 100 px (middle 3,200) and 20 px (middle 3,340) tall: the longer run is the stronger, chosen gap."""
    assert strip.cut_strip(6500, [(3150, 3250), (3330, 3350)], [])["cuts"] == [0, 3200, 6500], "longer run wins"


@budget(load="pure")
def test_a_gap_inside_a_removed_range_is_no_cut_candidate():
    """The gap's middle lies in the removed block: the 11,000 kept px are split evenly as a gapless run."""
    assert strip.cut_strip(12000, [(5400, 5500)], [{"start": 5000, "end": 6000}]) == {
        "kept": [(0, 5000), (6000, 12000)],
        "cuts": [0, 3667, 7333, 11000],
        "forced": [3667, 7333],
        "length": 11000,
    }, "a removed gap is no candidate"


@budget(load="pure")
def test_removed_ranges_in_any_order_at_both_strip_ends():
    """Removed ranges are sorted; ones touching the strip's start and end leave no empty kept range."""
    removed = [{"start": 2500, "end": 3000}, {"start": 0, "end": 500}]
    assert strip.cut_strip(3000, [], removed) == {
        "kept": [(500, 2500)],
        "cuts": [0, 2000],
        "forced": [],
        "length": 2000,
    }, "removed ranges sorted, no empty kept range"


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b", "want"),
    [(50, 250, [(50, 100), (300, 450)]), (100, 200, [(300, 400)])],
    ids=["across-a-removed-block", "after-the-first-range"],
)
def test_kept_ranges_map_kept_px_back_to_strip_ranges(a, b, want):
    """Kept [(0, 100), (300, 500)]: kept px [a, b) are these strip ranges."""
    assert strip.kept_ranges([(0, 100), (300, 500)], a, b) == want, "kept px map back to strip ranges"


@budget(load="pure")
def test_cut_strip_takes_overrides_of_the_frozen_limits():
    """A caller's own limits replace RESTITCH: max_page 1,000 cuts a gapless 2,500 px strip at 1,000 and 2,000."""
    cfg = dict(RC, min_page=500, max_page=1000)
    assert strip.cut_strip(2500, [], [], cfg)["cuts"] == [0, 1000, 2000, 2500], "the caller's limits are used"


# ---------------------------------------------------------------- rendering


@budget(load="pure")
@pytest.mark.parametrize(("n", "want"), [(1, "001"), (12, "012"), (1000, "1000")], ids=["1", "12", "1000"])
def test_output_name(n, want):
    """Rendered pages are numbered from 001, STEM_WIDTH 3 digits."""
    assert strip.output_name(n) == want, "3-digit stems"


@budget(load="fs")
def test_a_single_uncropped_part_keeps_its_mode(tmp_path):
    """A cover passes through render as it is (a gray cover stays gray)."""
    Image.new("L", (50, 60), 90).save(tmp_path / "000_cover.png")
    (out,) = strip.render([{"name": "001", "parts": [["000_cover.png", None]]}], tmp_path, tmp_path / "out")
    assert (out.name, Image.open(out).mode, Image.open(out).size) == ("001.png", "L", (50, 60)), "mode kept"


# ---------------------------------------------------------------- hard joins


@budget(load="pure")
@pytest.mark.parametrize(("height", "want"), [(80, [40]), (79, [])], ids=["seam-40-from-both-ends", "39-from-the-end"])
def test_hard_joins_keep_min_edge_block_px_from_either_end(height, want):
    """A black/white seam at row 40 is a hard join only when 40 rows (MIN_EDGE_BLOCK_PX) remain on both sides."""
    a = np.full((height, 100), 255, np.uint8)
    a[:40] = 0
    assert strip.hard_joins(Image.fromarray(a), MAX_SEAM) == want, "MIN_EDGE_BLOCK_PX from both ends"


def three_tones():
    """200 rows of 0 / 128 / 255 gray, seams at rows 60 and 120."""
    a = np.full((200, 100), 255, np.uint8)
    a[:60], a[60:120] = 0, 128
    return Image.fromarray(a).convert("RGB")


@budget(load="pure")
def test_one_hard_join_per_seam():
    """Two seams (rows 60 and 120) of a 3-tone block: one row each, in order."""
    assert strip.hard_joins(three_tones(), MAX_SEAM) == [60, 120], "one row per seam"


# ---------------------------------------------------------------- sliced page repair

MAX_SLICED = 5


def sliced_chapter(cached, out_dir, n=4):
    """Little Lord ch3: every 1,600 px page uploaded as 1,500 + 100 with the art crossing the cut."""

    def build(d):  # page i is the same whatever n: build the longest once
        pages = []
        for i in range(MAX_SLICED):
            page = fx.manga_page((1125, 1600), seed=i, noise=4)
            for box in ((0, 0, 1125, 1500), (0, 1500, 1125, 1600)):
                pages.append(d / f"{len(pages) + 1:03d}.webp")
                page.crop(box).save(pages[-1], "WEBP", quality=90)
        return pages

    return cached(out_dir, "sliced_chapter", build, limit=2 * n)


def slices_of(pages):
    """strip.find_slices with the edge rows the plan scan measures for every slice candidate pair."""
    sizes = [images.page_size(p) for p in pages]
    need = {j for i in strip.slice_pairs(sizes) for j in (i - 1, i)}
    return strip.find_slices(sizes, {k: strip.edge_rows(images.open_image(pages[k])[0]) for k in need})


def render_joined(pages, joins, out_dir):
    """Execution of a page-format plan's joins: every non-sliver page with its slivers appended (strip.render)."""
    attach = {j["page"]: j["sliver"] for j in joins if j["join"]}
    outputs, n = [], 0
    for i, _ in enumerate(pages):
        if i in attach.values():
            continue
        chain = [i]
        while chain[-1] in attach:
            chain.append(attach[chain[-1]])
        n += 1
        outputs.append({"name": strip.output_name(n), "parts": [[pages[k].name, None] for k in chain]})
    return strip.render(outputs, pages[0].parent, out_dir)


@budget(5.0, load="fs")
def test_sliced_pages_rejoined_to_full_pages(tmp_path, cached):
    """Little Lord ch3: 4 pages of 1,500 + 100 rejoined to 4 pages of 1125x1600."""
    pages = sliced_chapter(cached, tmp_path)
    joins = slices_of(pages)
    assert sum(j["join"] for j in joins) == 4, "every sliver joined"
    assert [Image.open(p).size for p in render_joined(pages, joins, tmp_path / "out")] == [(1125, 1600)] * 4, (
        "rejoined to whole pages"
    )


@budget(2.0, load="fs")
def test_intact_pages_untouched(tmp_path):
    """Whole 1125x1600 pages: no join."""
    pages = save_pages(tmp_path, [fx.manga_page((1125, 1600), seed=i) for i in range(4)])
    assert [j for j in slices_of(pages) if j["join"]] == [], "whole pages are never joined"


@budget(load="fs")
def test_ad_banner_after_white_margin_is_not_joined(tmp_path):
    """MangaBuddy banner appended after a page whose bottom is blank margin: no art on both sides, no join."""
    page = np.asarray(fx.manga_page((1000, 1500))).copy()
    page[-120:] = 255
    banner = np.full((300, 1000, 3), 255, np.uint8)
    banner[60:240, 100:900] = (30, 30, 30)
    joins = slices_of(save_pages(tmp_path, [Image.fromarray(page), Image.fromarray(banner)]))
    assert joins[0]["join"] is False, "a banner after blank margin is not joined"


@budget(2.0, load="fs")
def test_page_cut_into_three_pieces_keeps_every_row(tmp_path):
    """A 1,600 px page as 1,000 + 400 + 200: both cuts joined back into one page."""
    page = fx.manga_page((1125, 1600), noise=4)
    pages = save_pages(tmp_path, [page.crop((0, a, 1125, b)) for a, b in ((0, 1000), (1000, 1400), (1400, 1600))])
    joins = slices_of(pages)
    assert [j["join"] for j in joins] == [True, True], "both cuts joined"
    assert [Image.open(p).size for p in render_joined(pages, joins, tmp_path / "out")] == [(1125, 1600)], "one page"


@budget(6.0, load="fs")
def test_height_pattern_joins_a_cut_on_blank_margin(tmp_path, cached):
    """Little Lord ch3: the 5th 1,500 + 100 pair cut on blank margin joins by the confirmed height pattern."""
    pages = sliced_chapter(cached, tmp_path, n=5)
    im = np.asarray(Image.open(pages[8])).copy()
    im[-20:] = 255
    Image.fromarray(im).save(pages[8], "WEBP", quality=90)
    im = np.asarray(Image.open(pages[9])).copy()
    im[:20] = 255
    Image.fromarray(im).save(pages[9], "WEBP", quality=90)
    (last,) = [j for j in slices_of(pages) if j["page"] == 8]
    assert (last["join"], last["why"]) == (True, "pattern"), "joined by the confirmed height pattern"


@budget(load="pure")
@pytest.mark.parametrize(
    ("sizes", "want"),
    [
        ([(1125, 1600), (1125, 800)], [1]),
        ([(1125, 1600), (1125, 801)], []),
        ([(1125, 1600), (1000, 100)], []),
        ([(1125, 1500), (1125, 100), (1125, 1500), (1125, 100)], [1, 3]),
    ],
    ids=["half-height", "over-half", "other-width", "two-pairs"],
)
def test_slice_pairs_are_same_width_and_at_most_half_as_tall(sizes, want):
    """Shape: the short page has its predecessor's width and <= max_sliver_ratio 0.5 of its height."""
    assert strip.slice_pairs(sizes) == want, "same width, at most max_sliver_ratio as tall"


TEX = np.tile((np.arange(400) % 50 * 4).astype(np.float32), (6, 1))  # textured edge rows (std 57.7), smooth across
BLANK = np.zeros((6, 400), np.float32)


def alternating(hi):
    """6 edge rows alternating 0 / hi across x: row std hi / 2."""
    return np.tile(np.array([0, hi], np.float32), (6, 200))


@budget(load="pure")
def test_a_continuous_textured_cut_joins_by_seam():
    """Identical rows across the cut: seam 0, textured, joined by "seam"."""
    joins = strip.find_slices([(1125, 1500), (1125, 100)], {0: (TEX, TEX), 1: (TEX, TEX)})
    assert joins == [
        {"page": 0, "sliver": 1, "heights": [1500, 100], "seam": 0.0, "join": True, "textured": True, "why": "seam"}
    ], "a continuous textured cut joins by seam"


@budget(load="pure")
@pytest.mark.parametrize(("jump", "want"), [(3.5, True), (3.6, False)], ids=["at-max", "over-max"])
def test_seam_score_up_to_max_seam_score_joins(jump, want):
    """Smooth rows on both sides (inside change 0, floored to 1): a jump of max_seam_score 3.5 still joins."""
    (j,) = strip.find_slices([(1125, 1500), (1125, 100)], {0: (TEX, TEX), 1: (TEX + jump, TEX)})
    assert (j["seam"], j["join"]) == (round(jump, 2), want), "joins up to max_seam_score"


@budget(load="pure")
@pytest.mark.parametrize(("hi", "want"), [(16, True), (15, False)], ids=["std-8", "std-7.5"])
def test_edge_rows_need_min_edge_texture(hi, want):
    """Art on both sides: the rows at the cut need std >= min_edge_texture 8."""
    rows = alternating(hi)
    (j,) = strip.find_slices([(1125, 1500), (1125, 100)], {0: (rows, rows), 1: (rows, rows)})
    assert (j["textured"], j["join"]) == (want, want), "min_edge_texture 8"


@budget(load="pure")
def test_a_blank_cut_is_not_joined_by_seam():
    """Blank margin on both sides of the cut: seam 0 but not textured: no join, no reason."""
    (j,) = strip.find_slices([(1125, 1500), (1125, 100)], {0: (TEX, BLANK), 1: (BLANK, TEX)})
    assert (j["join"], j["textured"], j["why"]) == (False, False, None), "a blank cut is not joined"


def pairs_with_blank_last(seam_pairs, extra=((1500, 100),)):
    """seam_pairs textured continuous 1125-wide pairs of 1,500 + 100, then the `extra` height pairs cut on blank
    margin: (sizes, edges)."""
    heights = [(1500, 100)] * seam_pairs + list(extra)
    sizes = [(1125, h) for pair in heights for h in pair]
    edges = dict.fromkeys(range(2 * seam_pairs), (TEX, TEX))
    edges.update(dict.fromkeys(range(2 * seam_pairs, len(sizes)), (BLANK, BLANK)))
    return sizes, edges


@budget(load="pure")
@pytest.mark.parametrize(
    ("seam_pairs", "want"), [(3, (True, "pattern")), (2, (False, None))], ids=["confirmed-3-times", "confirmed-twice"]
)
def test_height_pattern_needs_pattern_min_joins_confirmations(seam_pairs, want):
    """A height pair confirmed by seam pattern_min_joins 3 times joins elsewhere even on blank margin."""
    joins = strip.find_slices(*pairs_with_blank_last(seam_pairs))
    assert (joins[-1]["join"], joins[-1]["why"]) == want, "pattern_min_joins 3"


@budget(load="pure")
def test_height_pattern_needs_exactly_the_confirmed_heights():
    """Pairs of 1,400 + 200 are not joined by a 1,500 + 100 pattern."""
    joins = strip.find_slices(*pairs_with_blank_last(3, extra=((1400, 200),)))
    assert joins[-1]["join"] is False, "only the exact confirmed heights join by pattern"


@budget(load="pure")
@pytest.mark.parametrize(("top_jump", "want"), [(6, 3.0), (0, 0.0)], ids=["jump-6-over-change-2", "no-jump"])
def test_seam_score_is_the_jump_over_the_in_page_row_change(top_jump, want):
    """Rows rising 2 per row on both sides: a jump of 6 across the join scores 3."""
    bottom = np.array([[0, 0], [2, 2], [4, 4]], np.float32)
    top = bottom + 4 + top_jump
    assert strip.seam_score(bottom, top) == pytest.approx(want), "jump over in-page change"


@budget(load="pure")
def test_seam_score_floors_the_in_page_change_at_1():
    """Flat rows on both sides: the jump itself is the score."""
    assert strip.seam_score(np.zeros((3, 2), np.float32), np.full((3, 2), 6, np.float32)) == 6.0, "floored at 1"


# ---------------------------------------------------------------- stitched page-format chapters


def stitched(cached, out_dir, pages_per_image=(3, 3, 2), w=1000, h=1422):
    """Little Lord ch9: page-format manga uploaded as images of whole pages (white gutters inside each page)."""

    def build(d, n, k, seed):
        # pages have white panel gutters inside them, so white rows are NOT page boundaries
        parts = []
        for s in range(seed, seed + k):
            page = np.asarray(fx.manga_page((w, h), seed=s)).copy()
            page[h // 2 - 30 : h // 2 + 30] = 255
            parts.append(page)
        p = d / f"{n:03d}.webp"
        Image.fromarray(np.vstack(parts)).save(p, "WEBP", quality=90)
        return [p]

    out, seed = [], 0
    for n, k in enumerate(pages_per_image, 1):  # each image built once: (3,) is the first image of (3, 3, 2)
        out += cached(out_dir, ("stitched", n, k, seed, w, h), lambda d, n=n, k=k, s=seed: build(d, n, k, s))
        seed += k
    return out


def plan_of(pages, aspects):
    return strip.plan_pages([p.name for p in pages], [images.page_size(p) for p in pages], aspects)


@budget(6.0, load="fs")
def test_split_into_whole_pages(tmp_path, cached):
    """Little Lord ch9: images of 3, 3 and 2 pages are stitched and split into 8 pages of 1000x1422."""
    pages = stitched(cached, tmp_path)
    assert strip.is_stitched([images.file_dimensions(p) for p in pages], [1.422]) is True, "tall images: stitched"
    pl = plan_of(pages, [1.422])
    out = render_cuts(pl["width"], pl["pages"], [[0, pl["height"]]], pl["cuts"], pages[0].parent, tmp_path / "out")
    assert [Image.open(p).size for p in out] == [(1000, 1422)] * 8, "split into whole pages"
    assert pl["irregular"] == [], "every image is whole pages"


@budget(2.0, load="fs")
def test_ordinary_pages_are_not_stitched(tmp_path):
    """Pages of the series shape are no stitched chapter."""
    pages = save_pages(tmp_path, [fx.manga_page((1000, 1422), seed=i) for i in range(5)], "p")
    assert strip.is_stitched([images.file_dimensions(p) for p in pages], [1.422]) is False, "series-shaped pages"


@budget(load="fs")
def test_series_shape_learned_from_unstitched_chapters(tmp_path):
    """6 pages of 1125x1600 (sizes read from their headers, as the plan's inventory does): the one candidate shape
    1.422."""
    pages = save_pages(tmp_path, [Image.new("RGB", (1125, 1600))] * 6, "p")
    assert strip.page_aspect([[images.file_dimensions(p) for p in pages]]) == [round(1600 / 1125, 3)], (
        "the page shape is learned"
    )


@budget(6.0, load="fs")
def test_wrong_series_shape_from_sliced_chapters_is_overridden(tmp_path, cached):
    """Little Lord: sliced chapters make 1500/1125 most common; ch10's 3 x 1600 images pick 1600."""
    pages = stitched(cached, tmp_path, (3, 3, 1), w=1125, h=1600)
    pl = plan_of(pages, [round(1500 / 1125, 3), round(1600 / 1125, 3)])
    assert (pl["page_h"], pl["irregular"]) == (1600, []), "the shape that splits the images whole wins"


@budget(4.0, load="fs")
def test_image_that_is_not_whole_pages_is_reported(tmp_path, cached):
    """A 3,800 px image is 2.67 pages: reported irregular (still split evenly)."""
    pages = stitched(cached, tmp_path, (3,))
    Image.open(pages[0]).crop((0, 0, 1000, 3800)).save(pages[0], "WEBP", quality=90)
    assert [r["page"] for r in plan_of(pages, [1.422])["irregular"]] == ["001.webp"], "reported irregular"


@budget(load="pure")
def test_plan_pages_cuts_each_image_into_equal_pages():
    """Little Lord ch9 sizes 4266, 4266, 2844 at shape 1.422: k = 3, 3, 2 pages of 1,422 px."""
    pl = strip.plan_pages(["001", "002", "003"], [(1000, 4266), (1000, 4266), (1000, 2844)], [1.422])
    assert (pl["width"], pl["height"], pl["page_h"], pl["irregular"]) == (1000, 11376, 1422, []), "plan geometry"
    assert pl["cuts"] == [0, 1422, 2844, 4266, 5688, 7110, 8532, 9954, 11376], "equal pages per image"
    assert [p["name"] for p in pl["pages"]] == ["001", "002", "003"], "pages keep their names"


@budget(load="pure")
@pytest.mark.parametrize(
    ("h", "want"),
    [(3060, []), (3061, [{"page": "001", "height": 3061, "pages": 3.061}])],
    ids=["at-snap-window", "past-snap-window"],
)
def test_snap_window_bounds_a_whole_number_of_pages(h, want):
    """Page 1,000 px: an image within +-6% (60 px) of 3 pages is regular; 61 px off is irregular."""
    assert strip.plan_pages(["001"], [(1000, h)], [1.0])["irregular"] == want, "snap_window 6%"


@budget(load="pure")
def test_without_a_series_shape_the_images_suggest_the_page_height():
    """No shape: smallest image 2,844 / 1, 2, 3 within 0.9-1.8 x width; 1,422 splits 2,844 and 4,266 whole."""
    assert strip.plan_pages(["001", "002"], [(1000, 2844), (1000, 4266)], None)["page_h"] == 1422, "image-derived"


@budget(load="pure")
def test_no_page_like_height_falls_back_to_fallback_page_aspect():
    """A 5,680 px image alone (5,680, 2,840, 1,893 all over 1.8 x width): pages of 1.42 x width, 4 of them."""
    pl = strip.plan_pages(["001"], [(1000, 5680)], None)
    assert (pl["page_h"], pl["cuts"]) == (1420, [0, 1420, 2840, 4260, 5680]), "fallback_page_aspect 1.42"


@budget(load="pure")
@pytest.mark.parametrize(
    ("sizes", "aspects", "want"),
    [
        ([(1000, 2700)] * 3, [1.0, 1.5], True),
        ([(1000, 2699)] * 3, [1.0, 1.5], False),
        ([(1000, 1000), (1000, 3000), (1000, 3000)], [1.5], True),
        ([(1000, 1000), (1000, 1000), (1000, 3000)], [1.5], False),
        ([(1000, 2557)], None, True),
        ([(1000, 2555)], None, False),
    ],
    ids=["at-1.8x-largest-shape", "under", "median-tall", "median-short", "fallback-over", "fallback-under"],
)
def test_stitched_when_the_median_shape_reaches_stitched_ratio_times_the_series_shape(sizes, aspects, want):
    """Median h/w >= 1.8 x the largest candidate shape (or fallback_page_aspect 1.42)."""
    assert strip.is_stitched(sizes, aspects) is want, "stitched_ratio 1.8 x the series shape"


@budget(load="pure")
@pytest.mark.parametrize(
    ("pages", "want"),
    [
        ([(1000, 900)] * 3, [0.9]),
        ([(1000, 899)] * 3, None),
        ([(1000, 1799)] * 3, [1.799]),
        ([(1000, 1800)] * 3, None),
        ([(1125, 1600)] * 2, None),
    ],
    ids=["aspect-0.9", "under-0.9", "under-stitched-ratio", "at-stitched-ratio", "two-pages"],
)
def test_page_aspect_counts_page_like_shapes_seen_3_times(pages, want):
    """Candidates: h/w in [0.9, stitched_ratio), at least 3 pages."""
    assert strip.page_aspect([pages]) == want, "page-like shapes seen 3 times"


@budget(load="pure")
def test_page_aspect_lists_the_3_most_common_shapes():
    """Shapes seen 6, 5, 4 and 3 times across chapters: the first 3, most common first."""
    chapters = [[(1000, 1400)] * 6 + [(1000, 1500)] * 5, [(1000, 1300)] * 4 + [(1000, 1200)] * 3]
    assert strip.page_aspect(chapters) == [1.4, 1.5, 1.3], "the 3 most common, most common first"


# ---------------------------------------------------------------- credit band pages

ASPECT = 1.42  # series shape 1.42, pages 600 wide -> one page is 852 px


def art(h=852, seed=1):
    return fx.manga_page((600, h), seed=seed, noise=3)


@budget(load="pure")
@pytest.mark.parametrize(("aspects", "want"), [([1.5, 1.795], 1.5), (None, 1.42)], ids=["most-common", "fallback"])
def test_series_shape_is_the_most_common_candidate(aspects, want):
    """My Noble Family ch7-10: banded pages are a candidate too, so not max(); none -> fallback_page_aspect."""
    assert strip.series_shape(aspects) == want, "the most common candidate, else the fallback"


@budget(load="pure")
def test_band_pages_are_pages_taller_than_the_shape_by_band_tolerance():
    """Shape 1.42 at 600 px: 852 x 1.01 = 860.52; 860 px is no band page, 861 px is, anywhere in the chapter."""
    sizes = [(600, 860), (600, 861), (600, 852), (600, 2000)]
    assert strip.band_pages(["001.jpg", "002.jpg", "003.jpg", "004.jpg"], sizes, ASPECT) == [1, 3], "band_tolerance"


@budget(load="pure")
@pytest.mark.parametrize(("h", "want"), [(1350, []), (1351, [0])], ids=["at-the-limit", "one-px-over"])
def test_a_band_page_is_strictly_taller_than_the_limit(h, want):
    """band_tolerance 0.5 (an exact limit: 600 x 1.5 x 1.5 = 1,350 px): a page of exactly the limit is no band page."""
    assert strip.band_pages(["001.jpg"], [(600, h)], 1.5, dict(SC, band_tolerance=0.5)) == want, "strictly taller"


@budget(load="pure")
def test_cover_named_pages_are_no_band_pages():
    """A tall cover-named page is never band-checked."""
    assert strip.band_pages(["000_cover.jpg", "001.jpg"], [(600, 2000), (600, 2000)], ASPECT) == [1], "cover skipped"


@budget(1.0, load="fs")
def test_cover_named_page_is_never_band_checked(tmp_path):
    """A banded 000_cover.png is no band page."""
    paths = [tmp_path / f"{i:03d}.png" for i in range(1, 5)]
    for i, p in enumerate(paths, 1):
        art(seed=i).save(p)
    fx.stack(fx.warning_banner(), art(seed=9)).save(tmp_path / "000_cover.png")
    paths.insert(0, tmp_path / "000_cover.png")
    sizes = [images.file_dimensions(p) for p in paths]
    assert strip.band_pages(paths, sizes, ASPECT) == [], "a cover-named page is never band-checked"


@budget(load="pure")
@pytest.mark.parametrize(
    ("n", "want"),
    [(0, []), (1, [0]), (4, [0, 1, 2, 3]), (5, [0, 1, 2, 3, 4]), (6, [0, 1, 3, 4, 5])],
    ids=["0", "1", "4", "5", "6"],
)
def test_edge_indexes_are_the_first_2_and_last_3(n, want):
    """END_HEAD 2 and END_TAIL 3, overlapping in short chapters."""
    assert strip.edge_indexes(n) == want, "first 2 and last 3"


# ---------------------------------------------------------------- segmentation (spans, walkable runs, edge regions)


def unpack_rows(d):
    """A page's blank-row profile as the bulk converter's plan scan cached it (bits, base64)."""
    return np.unpackbits(np.frombuffer(base64.b64decode(d["bits"]), np.uint8))[: d["n"]].astype(bool)


def fixture_strip(name):
    """(Strip of the cached blank-row profiles of a fixture chapter's pages, the fixture's crop [start, end)): rows
    mapped per page as strip.make_strip maps them; the strip ends with the last page in the fixture."""
    scan = json.loads((JUNK_FIX / name / "scan.json").read_text(encoding="utf-8"))
    pages, rows, starts = [], [], []
    for b in scan["blank_rows"]:
        r = unpack_rows(b)
        pages.append({"path": b["page"], "top": b["top"], "th": b["th"]})
        rows.append(r)
        starts.append(b["top"] + np.arange(len(r)) * b["th"] / len(r))
    end = pages[-1]["top"] + pages[-1]["th"]
    uniform = np.concatenate(rows)
    st = strip.Strip(
        pages,
        scan["strip"]["width"],
        end,
        uniform,
        sum(p["th"] for p in pages) / len(uniform),
        np.append(np.concatenate(starts), end),
    )
    return st, scan["crop"]


def spans_in(st, crop):
    return [s for s in strip.spans(st) if s[1] > crop[0] and s[0] < crop[1]]


@budget(load="fs")
def test_glued_lines_are_one_span():
    """I Stole the Female Lead's First Love c0006: the D&C logo, the (c) line and the bunny watermark glued with no
    blank row between them are one span; the three notice lines above it, 9 px blank runs apart, are three."""
    st, crop = fixture_strip("s5_2_i_stole_c0006")
    got = spans_in(st, crop)
    assert (132462, 132663) in got, f"glued lines are one span: {got}"
    assert [s for s in got if 132340 <= s[0] < 132430] == [(132346, 132365), (132374, 132393), (132403, 132420)], (
        "lines 9 px apart are separate spans"
    )


@budget(load="fs")
def test_notice_lines_with_blank_runs_between_are_separate_spans():
    """I Was the Real Head of the House c0003: 18 px notice lines with 9 px blank runs between them; every span starts
    and ends on a non-blank row (no blank rows at its ends) and every walkable run between spans is >= SPAN_BREAK_PX."""
    st, crop = fixture_strip("s5_2_real_head_c0003")
    got = spans_in(st, crop)
    assert [s for s in got if 111600 <= s[0] < 111700] == [(111612, 111630), (111639, 111657), (111666, 111684)], (
        "each notice line is a span"
    )
    breaks = [(a[1], b[0]) for a, b in itertools.pairwise(got)]
    assert all(e - s >= strip.SPAN_BREAK_PX for s, e in breaks), f"spans are SPAN_BREAK_PX apart: {breaks}"


def one_page_strip(uniform):
    """A strip of one 400 px wide page whose analysis rows are its pixel rows (analysis_width 400): uniform[i] = row i
    is blank."""
    uniform = np.asarray(uniform, bool)
    return strip.make_strip(["001.png"], [(400, len(uniform))], [uniform])


@budget(load="pure")
@pytest.mark.parametrize(
    ("run", "want"),
    [
        (strip.SPAN_BREAK_PX - 1, [(0, 30)]),
        (strip.SPAN_BREAK_PX, [(0, 14), (14 + strip.SPAN_BREAK_PX, 30)]),
    ],
    ids=["shorter-run-joins", "break-run-splits"],
)
def test_a_walkable_run_breaks_a_span_from_span_break_px(run, want):
    """Two text lines (I Was the Real Head of the House c0003's notice lines are 18 px) with a `run` px blank run
    between them: shorter than SPAN_BREAK_PX they stay one span (glued lines), from SPAN_BREAK_PX on two."""
    rows = [False] * 14 + [True] * run + [False] * (30 - 14 - run)
    st = one_page_strip(rows)
    assert strip.walkable_runs(st) == [(14, 14 + run)], "a walkable run of any length"
    assert strip.spans(st) == want, "SPAN_BREAK_PX splits spans"


@budget(load="pure")
def test_spans_leave_out_blank_rows_at_their_ends_and_an_all_blank_strip_has_none():
    """A span never starts or ends on a blank row, even a 1-row run at a strip end; an all-blank strip has no span."""
    assert strip.spans(one_page_strip([True] + [False] * 10 + [True])) == [(1, 11)], "no blank rows at span ends"
    assert strip.spans(one_page_strip([True] * 12)) == [], "an all-blank strip has no span"


@budget(load="pure")
@pytest.mark.parametrize(
    ("start", "end", "height", "want"),
    [
        (3330, 3480, 3570, "tail"),  # the Bato card at the end of a 3,570 px chapter: bottom half
        (0, 150, 3570, "head"),  # the same card at its head
        (1700, 1870, 3570, "head"),  # middle exactly at H / 2: head
        (1701, 1871, 3570, "tail"),
        (1000, 1200, 20000, "head"),  # a long strip: within the first edge_px
        (18500, 19000, 20000, "tail"),
        (5000, 7000, 20000, None),  # reaches past the first edge_px: no edge block
    ],
)
def test_edge_region_of_a_short_strip_takes_the_half_it_lies_in(start, end, height, want):
    """101 Concubine Walkthrough kind: in a strip shorter than 2 x edge_px a unit lies in both edge regions and takes
    the half its middle lies in."""
    assert strip.edge_region(start, end, height, 6000) == want, "edge region by the half the middle lies in"


@budget(load="db")
def test_ensure_series_layout_keeps_a_stored_layout_and_stores_a_computed_one(tmp_path, monkeypatch):
    cfg = site_cfg(tmp_path)
    with closing(db.connect(cfg)) as conn:
        recorded, fresh = add_series(conn, "Recorded", "kavita"), add_series(conn, "Fresh", "kavita")
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO series_conversion (series_id, layout, origin) VALUES (%s, 'strip', 'record')", (recorded,)
            )
        monkeypatch.setattr(strip, "classify_layout", lambda _pages: ("pages", "why", []))
        assert strip.ensure_series_layout(conn, recorded, []) == "strip", "the record's layout wins"
        assert strip.ensure_series_layout(conn, fresh, []) == "pages"
        rows = conn.execute("SELECT layout, origin FROM series_conversion WHERE series_id = %s", (fresh,)).fetchall()
        assert [tuple(r) for r in rows] == [("pages", "computed")], "computed once, then stored"
