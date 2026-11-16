"""Logo units (SPEC "Junk decisions": units, candidates, the chapter edit's blank): a page's logo measured at the
scan, which logos survive the rest of the edit, variants decided by their representatives, the blanks, the review
images (junk/logo_units.py)."""

import io

import numpy as np
import pytest
from PIL import Image

from manga_pipeline.junk import logo_units, marks

from tests.builders import present
from tests.timing import budget
from tests.world import logo_page


def page_rgb(badge="marks_scans_plain.png"):
    return Image.open(io.BytesIO(logo_page(badge))).convert("RGB")


def solid(grey, w=40):
    return marks.Template(np.full((20, w, 3), grey, np.uint8), np.ones((20, w), bool))


def logo(page, grey=200, ring_frac=1.0, tight=(100, 100, 140, 120), rows=None):
    """A scan's logo dict with a solid template of `grey` (grays 40 apart are 1 variant, 80 apart are not)."""
    x0, y0, x1, y1 = tight
    out = {
        "page": page,
        "tight": list(tight),
        "box": [x0 - 15, y0 - 15, x1 + 15, y1 + 15],
        "fill": [255, 255, 255],
        "ring_frac": ring_frac,
        "template": solid(grey).key_text(),
    }
    if rows is not None:
        out["rows"] = list(rows)
    return out


def store_of(**decided):
    """decide_unit over stored logo decisions {grey: verdict} (series-scoped rows), matched as the store matches."""
    stored = [(solid(g), {"id": g, "verdict": v, "series_key": "S"}) for g, v in decided.items()]

    def decide_unit(kind, key):
        assert kind == "logo", "logos are decided by logo decisions only"
        unit = marks.Template.from_key(key)
        return next((row for t, row in stored if marks.same_variant(t, unit)), None)

    return decide_unit


def every(_logo):
    """Every logo survives the rest of the edit."""
    return True


@budget(0.5, load="pure")
def test_a_gray_mark_page_gives_its_logo_and_a_colour_page_none():
    got = present(logo_units.measure(3, page_rgb()))
    assert got["page"] == 3, "the plain-margin badge is a blankable logo"
    assert got["ring_frac"] >= marks.RING_FRAC, "the plain-margin badge is a blankable logo"
    (x0, y0, x1, y1), (tx0, ty0, tx1, ty1) = got["box"], got["tight"]
    assert 100 <= tx0 < tx1 <= 337, "the tight box lies in the pasted crop (237 x 97)"
    assert 100 <= ty0 < ty1 <= 197, "the tight box lies in the pasted crop (237 x 97)"
    assert x0 < tx0, "the padded box surrounds it"
    assert y0 < ty0, "the padded box surrounds it"
    assert tx1 < x1, "the padded box surrounds it"
    assert ty1 < y1, "the padded box surrounds it"
    assert marks.Template.from_key(got["template"]).size == tuple(np.subtract(got["tight"][2:], got["tight"][:2])), (
        "the template is the tight box"
    )
    colour = Image.fromarray(np.random.default_rng(1).integers(0, 256, (240, 160, 3), dtype=np.uint8))
    assert logo_units.measure(0, colour) is None, "a colour page has no logo"


@budget(0.5, load="pure")
def test_a_busy_ring_is_measured_but_never_a_unit_or_a_blank():
    got = logo_units.measure(3, page_rgb("marks_scans_busy.png"))
    assert got is None or got["ring_frac"] < marks.RING_FRAC, "the badge next to art is not blankable"
    busy = logo(3, ring_frac=0.9)
    out = logo_units.decide([busy], every, store_of())
    assert out == {"blank": [], "undecided": []}, "a logo whose ring is not plain is no unit"
    out = logo_units.decide([busy], every, store_of(**{"200": "junk"}))
    assert out["blank"] == [], "and never blanked, even when its template is junk"


@budget(load="pure")
def test_an_undecided_variant_is_1_unit_with_its_representatives():
    out = logo_units.decide([logo(2, 200), logo(5, 160), logo(7, 120), logo(9, 40)], every, store_of())
    assert out["blank"] == [], "nothing is blanked while undecided"
    units = out["undecided"]
    assert [u["page"] for u in units] == [2, 9], "grays 200, 160, 120 are 1 variant (single link), 40 another"
    assert [len(u["keys"]) for u in units] == [2, 1], "200 and 120 represent the first variant (160 matches 200)"
    assert units[0]["key"] == units[0]["keys"][0]
    assert units[0]["reasons"] == [logo_units.REASON]


@budget(load="pure")
def test_a_junk_variant_blanks_every_member_with_its_own_box_and_fill():
    a, b = logo(2, 200), logo(6, 160, tight=(300, 50, 340, 70))
    out = logo_units.decide([b, a], every, store_of(**{"200": "junk"}))
    assert out["blank"] == [
        {"page": 2, "box": a["box"], "fill": a["fill"]},
        {"page": 6, "box": b["box"], "fill": b["fill"]},
    ], "1 blank per member, in page order"
    assert out["undecided"] == []


@budget(load="pure")
def test_a_keep_on_1_representative_beats_a_junk_on_another():
    """Grays 200 and 120 are the variant's 2 representatives: the series keep on 120 wins over the junk on 200."""
    out = logo_units.decide(
        [logo(2, 200), logo(5, 160), logo(7, 120)], every, store_of(**{"200": "junk", "120": "keep"})
    )
    assert out == {"blank": [], "undecided": []}, "the variant is kept whole"


@budget(load="pure")
@pytest.mark.parametrize(
    ("edit", "kept"),
    [
        ({"drop_pages": [2], "bands": []}, False),
        ({"drop_pages": [], "bands": [{"page": 2, "box": [0, 300, 1200, 1800]}]}, False),
        ({"drop_pages": [], "bands": [{"page": 2, "box": [0, 110, 1200, 1800]}]}, True),
        ({"drop_pages": [], "bands": [{"page": 3, "box": [0, 300, 1200, 1800]}]}, True),
    ],
    ids=["dropped", "cropped-off", "partly-cropped", "other-page-cropped"],
)
def test_a_logo_survives_a_page_edit_unless_dropped_or_cropped_off(edit, kept):
    out = logo_units.decide([logo(2)], logo_units.page_survives(edit), store_of(**{"200": "junk"}))
    assert bool(out["blank"]) is kept, "a logo the edit removes is neither blanked nor offered"


@budget(load="pure")
@pytest.mark.parametrize(
    ("removed", "kept"),
    [
        ([{"start": 900, "end": 1100}], False),
        ([{"start": 900, "end": 1000}, {"start": 1000, "end": 1050}], False),
        ([{"start": 900, "end": 1030}], True),
        ([{"start": 0, "end": 500}, {"start": 1040, "end": 2000}], True),
    ],
    ids=["inside-1-range", "inside-2-ranges", "partly-removed", "between-ranges"],
)
def test_a_logo_survives_a_strip_edit_unless_its_rows_are_removed(removed, kept):
    g = logo(2, rows=(1000, 1040))
    out = logo_units.decide([g], logo_units.strip_survives(removed), store_of())
    assert bool(out["undecided"]) is kept, "a logo inside the removed rows is not offered"


@budget(0.5, load="pure")
def test_review_images_show_the_badge_before_and_after_its_blank_and_the_page():
    page = page_rgb()
    g = logo_units.measure(3, page)
    unit, context = logo_units.review_images(page, g)
    assert unit.height == logo_units.UNIT_HEIGHT, "the unit image, then the page"
    assert context is page, "the unit image, then the page"
    half = (unit.width - logo_units.GAP) // 2
    before, after = np.asarray(unit)[:, :half], np.asarray(unit)[:, half + logo_units.GAP :]

    def spread(a):
        return (a.max(axis=2).astype(int) - a.min(axis=2)).max()

    assert spread(before) > marks.STRONG, "the badge is gone after its blank"
    assert spread(after) <= marks.STRONG, "the badge is gone after its blank"
