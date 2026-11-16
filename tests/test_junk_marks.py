"""Logo marks (SPEC "Junk decisions": units, keys; junk/marks.py; images.blank): measuring in the gray-mark box, the
ring test, variant matching, variants and a variant's decision, the blank. Ported from the bulk converter's
tests/test_marks.py; its Library tests (decisions, supersession, scopes, persistence) are covered by the store and
import tests instead (tests/test_junk_store.py).

Fixtures are tiny PNG crops of the real marks in "Akuyaku Kizoku to shite Hitsuyou na Sore" (pilot originals):
  marks_scans_plain / _plain_b  RESET-SCANS.CO on a plain white margin (ch6 p2, p17; blankable)
  marks_scans_scaled            RESET-SCANS.CO at a larger scale (ch7 p6; blankable)
  marks_scans_busy              RESET-SCANS.CO next to panel art (ch6 p1; ring too busy)
  marks_scan_co                 RESET-SCAN.CO on screentone-ish art (ch10 p3)
  marks_scan_org                RESET-SCAN.ORG (ch15 p3)
  marks_screentone              RESET-SCAN.ORG on dark screentone (ch15 p11; ring refused)
Each crop is pasted on a white 1200 x 1723 canvas (the pilot's page size), so the page is gray except for the badge.
Synthetic pages (a solid badge on a 200 x 200 white page) pin the documented thresholds by hand calculation."""

import re

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import images
from manga_pipeline.junk import marks

from tests import pages as fx
from tests.timing import budget

MARK_AT = (100, 100)


def mark_crop(name):
    return np.asarray(Image.open(fx.PAGE_FIX / name).convert("RGB"))


def mark_page(name, at=MARK_AT, size=(1200, 1723)):
    """The crop pasted on a white page of the pilot's size: gray except for the badge."""
    canvas = np.full((size[1], size[0], 3), 255, np.uint8)
    c = mark_crop(name)
    canvas[at[1] : at[1] + c.shape[0], at[0] : at[0] + c.shape[1]] = c
    return canvas


def mark_box(rgb):
    return images.gray_mark_box(rgb, images.COLOUR_BLOCK, images.MARK_LIMITS)


def found_box(rgb):
    box = mark_box(rgb)
    assert box is not None, "the page has a gray-mark box"
    return box


def measured(rgb, box):
    m = marks.measure(rgb, box)
    assert m is not None, "a mark is measured in the box"
    return m


def mark_of(name):
    """(page, marks.Mark) of a badge crop on its mark_page."""
    rgb = mark_page(name)
    return rgb, measured(rgb, found_box(rgb))


AK = "Akuyaku Kizoku to shite Hitsuyou na Sore"
AT = MARK_AT
NAMES = [
    "marks_scans_plain.png",
    "marks_scans_plain_b.png",
    "marks_scans_scaled.png",
    "marks_scans_busy.png",
    "marks_scan_co.png",
    "marks_scan_org.png",
    "marks_screentone.png",
]


def badge_page(x0, y0, x1, y1, colour=(255, 0, 0), *, size=200):
    """A white size x size page with a solid `colour` badge in the box (x0, y0, x1, y1)."""
    rgb = np.full((size, size, 3), 255, np.uint8)
    rgb[y0:y1, x0:x1] = colour
    return rgb


def solid(rgb, h=20, w=40, mask=None):
    """A template of one colour, every pixel coloured unless `mask` says otherwise."""
    return marks.Template(np.full((h, w, 3), rgb, np.uint8), np.ones((h, w), bool) if mask is None else mask)


@pytest.fixture(scope="module")
def templates():
    """The templates of the 7 pilot crops, in NAMES order (immutable, shared by the module). Measuring the 7
    1200x1723 pages takes ~0.1 s, charged to the first test that uses it: those tests carry a 0.5 s budget."""
    return [mark_of(n)[1].template for n in NAMES]


# Detection


@budget(load="pure")
def test_one_badge_is_found_by_parts_b_mark_box():
    """RESET-SCANS.CO pasted at (100, 100): Part B's mark box covers the badge's start and its 159 px width."""
    x0, y0, x1, _y1 = found_box(mark_page("marks_scans_plain.png"))
    assert x0 <= AT[0] + 40, "box must start at the badge's left"
    assert y0 <= AT[1] + 40, "box must start at the badge's top"
    assert x1 > AT[0] + 150, "box must reach past the coloured text"


@budget(load="pure")
def test_gray_page_has_no_mark():
    """A plain gray page has no mark box."""
    assert mark_box(np.full((1723, 1200, 3), 200, np.uint8)) is None


@budget(load="pure")
def test_two_badges_are_no_gray_mark():
    """Two badges make a colour page (Part B): no gray-mark box, nothing to measure."""
    rgb = mark_page("marks_scans_plain.png")
    c = mark_crop("marks_scans_plain.png")
    rgb[900 : 900 + c.shape[0], 700 : 700 + c.shape[1]] = c
    assert mark_box(rgb) is None


@budget(load="pure")
def test_tight_box_is_the_cyan_text_inside_the_padded_box():
    """Ch6 p2: the tight box is the pill's ~159 x 19 coloured text (JPEG noise trimmed), strictly inside the box."""
    _, m = mark_of("marks_scans_plain.png")
    tw, th = m.tight[2] - m.tight[0], m.tight[3] - m.tight[1]
    assert (tw, th) == m.template.size, "the template is the tight box"
    assert tw == pytest.approx(159, abs=3)
    assert th == pytest.approx(19, abs=2)
    assert (m.box[0] < m.tight[0], m.box[1] < m.tight[1], m.box[2] > m.tight[2], m.box[3] > m.tight[3]) == (
        True,
        True,
        True,
        True,
    ), "the padded box surrounds the tight box on every side"


@budget(load="pure")
def test_box_without_strong_colour_has_no_mark():
    """A mid-gray box has no strongly coloured pixel: no Mark."""
    assert marks.measure(np.full((100, 100, 3), 128, np.uint8), (0, 0, 50, 50)) is None


@budget(load="pure")
def test_specks_with_no_solid_overlap_have_no_mark():
    """Before the Real Heroine Steps In, Eva_Chapter 52 p031: 116 specks whose solid rows and columns don't meet."""
    # mark box (128, 0, 184, 96) on the 800x2000 page; building the template from them failed the series
    crop = np.asarray(Image.open(fx.PAGE_FIX / "marks_heroine_ch52_p031.png").convert("RGB"))
    assert marks.measure(crop, (0, 0, crop.shape[1], crop.shape[0])) is None


@budget(load="pure")
def test_solid_badge_is_padded_by_three_quarters_of_its_height():
    """A 40 x 20 red badge at (80, 90): tight box is the badge, padded by round(0.75 * 20) = 15; white ring, plain."""
    m = measured(badge_page(80, 90, 120, 110), (60, 60, 140, 140))
    assert (m.tight, m.box, m.fill, m.ring_frac, m.blankable) == (
        (80, 90, 120, 110),
        (65, 75, 135, 125),
        (255, 255, 255),
        1.0,
        True,
    )
    assert (m.template.size, bool(m.template.mask.all())) == ((40, 20), True)


@budget(load="pure")
@pytest.mark.parametrize(
    ("badge", "bbox", "box"),
    [
        ((0, 0, 40, 20), (0, 0, 60, 60), (0, 0, 55, 35)),
        ((160, 180, 200, 200), (140, 140, 200, 200), (145, 165, 200, 200)),
    ],
    ids=["top-left", "bottom-right"],
)
def test_padded_box_is_clamped_to_the_page(badge, bbox, box):
    """A badge at a page corner: the 15 px padding stops at the page edge."""
    assert measured(badge_page(*badge), bbox).box == box


@budget(load="pure")
@pytest.mark.parametrize(("spread", "found"), [(41, True), (40, False)], ids=["spread-41", "spread-40"])
def test_strong_pixels_have_a_channel_spread_over_40(spread, found):
    """A badge coloured (255, 255 - spread, 255): strongly coloured only when the spread is > 40."""
    m = marks.measure(badge_page(80, 90, 120, 110, (255, 255 - spread, 255)), (60, 60, 140, 140))
    assert (m is not None) is found


@budget(load="pure")
@pytest.mark.parametrize(
    ("speck", "tight"), [(3, (80, 90, 131, 110)), (2, (80, 90, 120, 110))], ids=["15pct-kept", "10pct-trimmed"]
)
def test_columns_under_15_percent_of_the_peak_are_trimmed(speck, tight):
    """Badge columns hold 20 strong pixels; a speck column at x = 130 with 3 (15%) stays in the box, 2 are noise."""
    rgb = badge_page(80, 90, 120, 110)
    rgb[95 : 95 + speck, 130] = (255, 0, 0)
    assert measured(rgb, (60, 60, 140, 140)).tight == tight


# Ring


@budget(0.5, load="pure")
@pytest.mark.parametrize("name", ["marks_scans_plain.png", "marks_scans_plain_b.png", "marks_scans_scaled.png"])
def test_plain_ring_is_blankable_and_filled_with_its_median(name):
    """Plain-margin RESET-SCANS.CO (1200x1723 page): blankable, white fill, only the box changes, the page goes gray."""
    rgb, m = mark_of(name)
    before = rgb.copy()
    out = images.blank(rgb, m.box, m.fill)
    x0, y0, x1, y1 = m.box
    outside = np.ones(rgb.shape[:2], bool)
    outside[y0:y1, x0:x1] = False
    assert m.blankable, "a plain white margin is blankable"
    assert m.fill[0] >= 240, "the fill is the white margin's median"
    assert (out[y0:y1, x0:x1] == m.fill).all(), "the whole box takes the fill"
    assert (out[outside] == rgb[outside]).all(), "nothing outside the box changes"
    assert (rgb == before).all(), "input must not be modified"
    assert mark_box(out) is None, "no mark is left"
    assert images.page_class(out, images.COLOUR_BLOCK, images.TINT_P99, images.MARK_LIMITS) == images.GRAY


@budget(load="pure")
@pytest.mark.parametrize(
    "name", ["marks_scans_busy.png", "marks_scan_co.png", "marks_scan_org.png", "marks_screentone.png"]
)
def test_busy_ring_is_not_blankable(name):
    """Badges next to art or on screentone are never blanked."""
    assert not mark_of(name)[1].blankable, f"{name}: the ring is not plain"


@budget(load="pure")
def test_blank_takes_the_planned_fill():
    """Execution fills what the plan measured (ch6 p2 RESET-SCANS.CO): any given fill, nothing re-measured."""
    rgb, m = mark_of("marks_scans_plain.png")
    out = images.blank(rgb, m.box, (10, 20, 30))
    x0, y0, x1, y1 = m.box
    assert (out[y0:y1, x0:x1] == (10, 20, 30)).all()


@budget(load="pure")
@pytest.mark.parametrize(
    ("box", "fill", "msg"),
    [
        (
            (1100, 1700, 1300, 1800),
            (255, 255, 255),
            re.escape("blank box (1100, 1700, 1300, 1800) is not inside the 1200x1723 page"),
        ),
        ((-1, 0, 10, 10), (255, 255, 255), "is not inside"),
        ((10, 10, 10, 20), (255, 255, 255), "is not inside"),
        ((10, 20, 30, 20), (255, 255, 255), "is not inside"),
        ((0, 0, 10, 10), (255, 255), re.escape("blank fill (255, 255) is not an RGB colour")),
        ((0, 0, 10, 10), (256, 0, 0), "is not an RGB colour"),
        ((0, 0, 10, 10), (0, -1, 0), "is not an RGB colour"),
    ],
    ids=["outside-page", "negative", "empty-width", "empty-height", "two-channels", "over-255", "negative-fill"],
)
def test_blank_refuses_a_bad_box_or_fill(box, fill, msg):
    """A box outside the 1200 x 1723 page or empty, or a fill that is not one RGB colour, raises ValueError."""
    with pytest.raises(ValueError, match=msg):
        images.blank(np.full((1723, 1200, 3), 255, np.uint8), box, fill)


@budget(load="pure")
@pytest.mark.parametrize("fill", [(0, 0, 0), (255, 255, 255)], ids=["black", "white"])
def test_blank_accepts_the_whole_page_and_the_channel_limits(fill):
    """The box may span the whole page and the fill reach 0 and 255."""
    assert (images.blank(np.full((20, 30, 3), 128, np.uint8), (0, 0, 30, 20), fill) == fill).all()


@budget(load="pure")
def test_ring_frame_fraction():
    """A 10 px ring around (80, 80, 120, 120): 2,000 pixels; a black dot on every other pixel blackens 500 (0.75)."""
    rgb = np.full((200, 200, 3), 250, np.uint8)
    box = (80, 80, 120, 120)
    assert marks.ring_stats(rgb, box) == ((250, 250, 250), 1.0)
    rgb[70:130:2, 70:130:2] = 0
    rgb[80:120, 80:120] = 250
    assert marks.ring_stats(rgb, box) == ((250, 250, 250), 0.75), "the box's inside is not part of the ring"


@budget(load="pure")
def test_box_inside_is_not_part_of_the_ring():
    """A black 60 x 20 badge filling the box (70, 90, 130, 110) in a plain 250 margin: the ring is all margin."""
    rgb = np.full((200, 200, 3), 250, np.uint8)
    rgb[90:110, 70:130] = 0
    assert marks.ring_stats(rgb, (70, 90, 130, 110)) == ((250, 250, 250), 1.0)


@budget(load="pure")
@pytest.mark.parametrize(("v", "frac"), [(240, 1.0), (239, 0.7)], ids=["10-off", "11-off"])
def test_ring_pixels_within_10_of_the_median_pass(v, frac):
    """The ring's top band (600 of 2,000 pixels) at v, the rest at the median 250: 10 off passes, 11 does not."""
    rgb = np.full((200, 200, 3), 250, np.uint8)
    rgb[70:80, 70:130] = v
    assert marks.ring_stats(rgb, (80, 80, 120, 120)) == ((250, 250, 250), frac)


@budget(load="pure")
def test_ring_of_a_box_filling_the_page_is_empty():
    """A box covering the whole page leaves no ring pixel: fill (0, 0, 0), frac 0 (never blankable)."""
    assert marks.ring_stats(np.full((50, 50, 3), 255, np.uint8), (0, 0, 50, 50)) == ((0, 0, 0), 0.0)


@budget(load="pure")
@pytest.mark.parametrize(("frac", "want"), [(0.95, True), (0.9499, False)], ids=["at-95pct", "below"])
def test_a_mark_is_blankable_from_95_percent_plain_ring(frac, want):
    """blankable is ring_frac >= 0.95."""
    assert marks.Mark((0, 0, 1, 1), (0, 0, 1, 1), (255, 255, 255), frac, solid((255, 0, 0))).blankable is want


# Variants


@budget(0.5, load="pure")
def test_reset_scans_variants_cluster_into_3(templates):
    """RESET-SCANS.CO at 2 scales and on busy art is one variant; SCAN.CO and SCAN.ORG (2 crops) are the others."""
    assert marks.cluster(templates) == [[0, 1, 2, 3], [4], [5, 6]]


@budget(0.5, load="pure")
def test_same_variant_is_symmetric_for_different_scales(templates):
    """RESET-SCANS.CO ch6 p2 and its larger ch7 p6 scale match both ways."""
    assert (marks.same_variant(templates[0], templates[2]), marks.same_variant(templates[2], templates[0])) == (
        True,
        True,
    )


@budget(0.5, load="pure")
def test_png_round_trip_keeps_the_match(templates):
    """RESET-SCAN.CO stored as RGBA PNG comes back with its size, mask, match and review id."""
    t = templates[4]
    back = marks.Template.from_png(t.to_png())
    assert back.size == t.size
    assert (back.mask == t.mask).all()
    assert marks.same_variant(t, back)
    assert back.review_id() == t.review_id()


@budget(0.5, load="pure")
def test_representatives_cover_every_member_of_a_variant(templates):
    """What a review decision records: a few rows (the first template first) that decide every page of the variant."""
    group = templates[:4]  # RESET-SCANS.CO at 2 scales and on busy art
    reps = marks.representatives(group)
    assert len(reps) <= len(group)
    assert reps[0] is group[0]
    assert [any(marks.same_variant(r, t) for r in reps) for t in group] == [True] * 4, (
        "every member must match a representative"
    )


@budget(0.5, load="pure")
def test_review_id_is_stable_and_distinct(templates):
    """Every template's id is 'm' + 8 hex, the same on every call, and distinct across the 7 crops."""
    ids = [t.review_id() for t in templates]
    assert ids == [t.review_id() for t in templates]
    assert [i for i in ids if not re.fullmatch("m[0-9a-f]{8}", i)] == []
    assert len(set(ids)) == len(ids)


@budget(load="pure")
def test_template_without_coloured_pixels_is_refused():
    """A template needs at least one coloured pixel."""
    with pytest.raises(ValueError, match="template has no coloured pixels"):
        marks.Template(np.zeros((5, 5, 3), np.uint8), np.zeros((5, 5), bool))


@budget(load="pure")
@pytest.mark.parametrize(("v", "want"), [(160, True), (159, False)], ids=["delta-40", "delta-41"])
def test_variants_match_up_to_mean_delta_40(v, want):
    """Gray 200 against gray v on every coloured pixel: mean |dRGB| 40 is the same badge, 41 is not."""
    assert marks.same_variant(solid((200,) * 3), solid((v,) * 3)) == want


@budget(load="pure")
@pytest.mark.parametrize(("n", "want"), [(70, True), (69, False)], ids=["iou-0.70", "iou-0.69"])
def test_variants_need_mask_iou_of_0_7(n, want):
    """A full 10 x 10 mask against one with the first n pixels coloured: IoU n / 100 must be >= 0.7."""
    part = (np.arange(100) < n).reshape(10, 10)
    assert marks.same_variant(solid((0,) * 3, 10, 10), solid((0,) * 3, 10, 10, part)) == want


@budget(load="pure")
@pytest.mark.parametrize(
    ("a", "b", "want"),
    [
        ((50, 100), (50, 110), True),
        ((50, 100), (50, 112), False),
        ((50, 100), (50, 90), True),
        ((40, 50), (40, 45), True),
    ],
    ids=["2.0-vs-2.2", "2.0-vs-2.24", "2.0-vs-1.8", "exactly-10pct"],
)
def test_variants_match_aspect_within_10_percent(a, b, want):
    """Badges of (h, w) a and b, b resized to compare: |a - b| <= 0.1 * max, inclusive (1.25 vs 1.125)."""
    assert marks.same_variant(solid((0,) * 3, *a), solid((0,) * 3, *b)) == want


@budget(load="pure")
def test_cluster_is_single_link():
    """Grays 200, 120, 160: 160 matches both (delta 40), 200 and 120 do not (80): one variant of all three."""
    assert marks.cluster([solid((200,) * 3), solid((120,) * 3), solid((160,) * 3)]) == [[0, 1, 2]]


@budget(load="pure")
def test_representatives_skip_templates_an_earlier_one_covers():
    """Grays 200, 160, 120: 160 is covered by 200, 120 is not: representatives are 200 and 120."""
    ts = [solid((200,) * 3), solid((160,) * 3), solid((120,) * 3)]
    assert [t is r for t, r in zip([ts[0], ts[2]], marks.representatives(ts), strict=True)] == [True, True]
    assert len(marks.representatives(ts)) == 2


# Variant rule (marks.resolve): one logo variant (e.g. Reset Scans' RESET-SCANS.CO, 63 pilot pages, several
# representatives) decided from its representatives' deciding rows.
def row(row_id, verdict, series_key):
    """A store row as a lookup returns it."""
    return {"id": row_id, "verdict": verdict, "series_key": series_key}


A, G = row(1, "junk", ""), row(2, "junk", AK)
K, GK = row(3, "keep", AK), row(4, "keep", "")


@budget(load="pure")
@pytest.mark.parametrize(
    ("ds", "keep"), [([A, K], K), ([None, K], K), ([A, GK], GK)], ids=["after-approval", "after-pending", "global-keep"]
)
def test_a_keep_on_any_representative_wins(ds, keep):
    """A keep on any representative decides the variant, a global one too (plan L5: a candidate a decision covers
    never leaves its variant undecided)."""
    assert marks.resolve(ds) == keep


@budget(load="pure")
def test_one_shared_decision_decides():
    """Two approvals: the first representative's decides."""
    assert marks.resolve([A, G]) == A


@budget(load="pure")
@pytest.mark.parametrize("ds", [[None, A, None], [None, A]], ids=["among-pending", "after-pending"])
def test_an_approval_on_any_representative_decides(ds):
    """LikeManga.io: one of How to Survive...'s 28 representatives matched an approval."""
    # approved globally in Can We Become Family_ (m9e9298da); How to Survive...'s variant mceea7fb4 was asked again
    # in batch 6
    assert marks.resolve(ds) == A


@budget(load="pure")
@pytest.mark.parametrize("ds", [[None, None], []], ids=["undecided", "no-representatives"])
def test_undecided_representatives_are_pending(ds):
    """No decision on any representative: pending (None)."""
    assert marks.resolve(ds) is None


@budget(load="pure")
def test_a_templates_key_text_round_trips_and_still_matches(templates):
    """A logo decision's key is the template's RGBA PNG in base64: it comes back as the same template."""
    t = templates[0]
    back = marks.Template.from_key(t.key_text())
    assert back.size == t.size, "the key gives the template back"
    assert marks.same_variant(back, t), "the key gives the template back"
