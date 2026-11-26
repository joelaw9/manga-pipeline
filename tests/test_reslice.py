"""Re-sliced page chapters (SPEC "Junk decisions", Pages; "Conversion", Layout).

The rule by sizes and by pixels, the recorded page count, the stack cut.

Fixture pages are 120 x 168 (shape 1.4), each with its own base colour and a smooth vertical gradient, so a page
boundary is a seam no slice makes and a cut through a page is continuous.
"""

import json

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import strip
from manga_pipeline.convert import plan
from manga_pipeline.junk import page_units

from tests.timing import budget

W, H = 120, 168  # a page: shape 1.4
ASPECTS = [1.4]


def page(i: int, height: int = H) -> np.ndarray:
    """Page i: its own base colour, a gentle vertical gradient and column texture (never blank)."""
    base = 30 + (i * 67) % 160
    rows = base + np.linspace(0, 12, height)[:, None]
    cols = 25 * np.sin(np.arange(W) / 6)[None, :]
    g = np.clip(rows + cols, 0, 255).astype(np.uint8)
    return np.stack([g, np.roll(g, 3, axis=1), g[:, ::-1]], axis=2)


def recut(stack: np.ndarray, heights: list[int]) -> list[np.ndarray]:
    """Cut a stack of pages into images of these heights (they sum to its height)."""
    assert sum(heights) == len(stack), "the images cover the stack"
    out, top = [], 0
    for h in heights:
        out.append(stack[top : top + h])
        top += h
    return out


def sizes(ims: list[np.ndarray]) -> list[tuple[int, int]]:
    return [(im.shape[1], im.shape[0]) for im in ims]


def opener(ims: list[np.ndarray]):
    return lambda k: Image.fromarray(ims[k])


def split(ims: list[np.ndarray], aspects=ASPECTS) -> page_units.Split | None:
    return page_units.split_of(opener(ims), sizes(ims), aspects)


PAGES_28 = np.concatenate([page(i) for i in range(28)])  # 28 pages of 168 = 21 images of 224 (1 1/3 pages)


# ---------------------------------------------------------------- the oracle


@budget(load="fs")
def test_a_stack_recut_at_1_1_3_pages_is_cut_back_into_its_pages_pixel_for_pixel(tmp_path):
    ims = recut(PAGES_28, [224] * 21)
    assert split(ims) == page_units.Split(28), "re-sliced into its 28 pages"
    for k, im in enumerate(ims):
        Image.fromarray(im).save(tmp_path / f"{k + 1:03d}.png")
    names = [f"{k + 1:03d}.png" for k in range(21)]
    pl = strip.plan_pages(names, sizes(ims), ASPECTS, pages_n=28)
    assert (pl["page_h"], pl["irregular"]) == (H, []), "no image irregular: the stack is cut, not each image"
    layout: strip.RenderStrip = {
        "width": W,
        "pages": [{"name": p["name"], "top": p["top"], "th": p["th"]} for p in pl["pages"]],
        "kept": [[0, pl["height"]]],
    }
    outputs: list[strip.RenderOutput] = [
        {"name": f"{i + 1:03d}", "rows": [pl["cuts"][i], pl["cuts"][i + 1]]} for i in range(28)
    ]
    rendered = strip.render(outputs, tmp_path, tmp_path / "out", layout)
    for i, path in enumerate(rendered):
        with Image.open(path) as im:
            assert np.array_equal(np.asarray(im.convert("RGB")), page(i)), f"page {i} comes back pixel for pixel"


@budget(load="pure")
def test_a_stack_ending_in_whole_pages_is_recut_as_chapter_16_was():
    stack = np.concatenate([page(i) for i in range(26)])
    assert split(recut(stack, [224] * 18 + [H, H])) == page_units.Split(26), "18 x 1 1/3 pages, then 2 whole pages"


@budget(load="pure")
def test_pages_cut_in_2_are_rejoined_by_their_continuous_joins():
    ims = recut(np.concatenate([page(i) for i in range(20)]), [99, 69] * 20)
    assert split(ims) == page_units.Split(20), "every cut on an image boundary; every in-page join continuous"


# ---------------------------------------------------------------- not re-sliced


@budget(load="pure")
def test_tall_pages_whose_even_cuts_run_through_the_art_are_not_re_sliced():
    ims = [page(i, height=252) for i in range(4)]  # 1 1/2 pages tall by size, but each 1 page: 797's shape
    assert strip.reslice_pages(sizes(ims), ASPECTS) == 6, "by its sizes it would be"
    assert split(ims) is None, "its cuts are continuous: no page boundary"


@budget(load="pure")
def test_inserted_half_pages_that_shift_the_grid_are_not_re_sliced():
    full = [page(i) for i in range(30)]
    half = [page(100), page(101)]  # 2 half pages: 84 rows each, together 1 page
    pieces = [*full[:10], half[0][:84], *full[10:20], half[1][:84], *full[20:]]
    heights = [h for p in pieces for h in ((99, 69) if len(p) == H else (50, 34))]
    ims = recut(np.concatenate(pieces), heights)  # 50695's shape
    assert strip.reslice_pages(sizes(ims), ASPECTS) == 31, "by its sizes it would be"
    assert split(ims) is None, "the shifted stretch's cuts run through pages"


@budget(load="pure")
def test_2_separate_pages_of_99_and_69_rows_are_not_rejoined():
    ims = [page(i)[: (99 if i % 2 == 0 else 69)] for i in range(40)]  # each part its own page: discontinuous joins
    assert strip.reslice_pages(sizes(ims), ASPECTS) == 20, "by its sizes it would be"
    assert split(ims) is None, "its joins are page boundaries, not cuts through a page"


@pytest.mark.parametrize(
    ("ims_sizes", "aspects", "why"),
    [
        ([(120, 168)] * 10, ASPECTS, "whole pages"),
        ([(120, 224)] * 20 + [(240, 224)], ASPECTS, "2 widths: a spread"),
        ([(120, 224)] * 21, [], "no aspect candidate"),
        ([(120, 160), (120, 8)] * 10, ASPECTS, "slivers: slice repair's"),
        ([(120, 178)] * 21, ASPECTS, "images 6 % off a page: near whole"),
    ],
)
@budget(load="pure")
def test_the_size_rule_leaves_these_alone(ims_sizes, aspects, why):
    assert strip.reslice_pages(ims_sizes, aspects) is None, why


@budget(load="pure")
def test_a_stitched_chapter_is_split_per_image_as_today():
    stitched = [(120, 504)] * 5  # 3 pages per image
    assert page_units.split_of(lambda _k: Image.new("RGB", (1, 1)), stitched, ASPECTS) == page_units.Split(None)
    assert strip.plan_pages([f"{k}" for k in range(5)], stitched, ASPECTS)["cuts"] == list(range(0, 2521, 168))


# Real sizes (step 0's cached library sizes): series 947's file 74783 (pages cut in 2 whose stack is not whole, with
# a 1.042 candidate that makes it whole by chance), 644's 63615 (1 1/3 pages, heights 2263 and 2262).
FILE_74783 = (
    [(1200, 1000)] * 2
    + [(1200, 221), (1200, 512), (1200, 341)]
    + [(1200, 1000), (1200, 707)] * 22
    + [(1200, 512), (1200, 341)]
    + [(1200, 1000), (1200, 707)] * 8
    + [(1200, 1000)] * 3
    + [(1200, 605)]
)
FILE_63615 = [(1200, 2263), (1200, 2263), (1200, 2262)] * 4


@budget(load="pure")
def test_the_size_rule_on_real_files_and_a_polluting_candidate():
    assert strip.reslice_pages(FILE_74783, [1.042, 1.421]) is None, "947: the 1.042 cut meets too few boundaries"
    assert strip.reslice_pages(FILE_63615, [1.334, 1.414, 1.415]) == 16, "644: the candidate the boundaries fall on"
    polluted = [2263 / 1200, 1.414]  # under the first the images are whole pages: it is passed over
    assert strip.reslice_pages(FILE_63615, polluted) == 16


@budget(load="pure")
def test_of_2_fitting_candidates_the_one_the_most_boundaries_fall_on_gives_the_count():
    halves = [(W, 99), (W, 69)] * 20  # pages of 168 cut in 2: every 2nd boundary on a 168 cut, every 4th on a 336 cut
    assert strip.reslice_pages(halves, [2.8, 1.4]) == 20, "1.4's cuts meet twice the boundaries 2.8's do"


# ---------------------------------------------------------------- junk records it, convert executes it


@budget(load="fs")
def test_junk_records_the_page_count_in_its_scan_and_edit(tmp_path):
    ims = recut(PAGES_28, [224] * 21)
    paths = []
    for k, im in enumerate(ims):
        paths.append(tmp_path / f"{k + 1:03d}.png")
        Image.fromarray(im).save(paths[-1])
    sc = page_units.scan(paths, [p.name for p in paths], sizes(ims), [False] * 21, ASPECTS)
    assert sc == {"layout": "pages", "split": True, "pages": 28, "marks": []}, "no page units, no logos"
    out = page_units.decide(sc, lambda _kind, _key: None)
    assert out["edit"] == {"split": True, "split_pages": 28, "drop_pages": [], "bands": [], "join": []}


@pytest.mark.parametrize(
    ("split_value", "pages_n", "ok"),
    [
        (True, 28, True),
        (True, None, True),
        (False, None, True),
        (True, 1, False),
        (False, 28, False),
        (True, "28", False),
    ],
)
@budget(load="pure")
def test_the_edit_holds_split_pages_only_as_a_page_count_of_a_split_chapter(split_value, pages_n, ok):
    edit = {"drop_pages": [], "strip_removed": [], "bands": [], "join": [], "blank": []}
    text = json.dumps({**edit, "split": split_value, "split_pages": pages_n})
    if ok:
        assert plan.check_edit(text)["split_pages"] == pages_n
    else:
        with pytest.raises(plan.EditError, match="split_pages"):
            plan.check_edit(text)


@budget(load="pure")
def test_an_edit_without_split_pages_is_malformed():
    text = json.dumps({"drop_pages": [], "strip_removed": [], "bands": [], "join": [], "split": False, "blank": []})
    with pytest.raises(plan.EditError, match="keys"):
        plan.check_edit(text)
