"""Junk units of a page-format chapter (SPEC "Junk decisions": units, the chapter edit; junk/page_units.py):
credit bands (find_band, ink_box), the end-page size and text rules (page_candidates, text_features, end_text_page),
and decide() on small scans. The band, size and text tests are ported from the bulk converter's tests/test_restitch.py
and tests/test_segmentation.py; the page sizes are recorded from real chapters.
"""

import functools
from collections.abc import Mapping
from unittest import mock

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import images, strip
from manga_pipeline.junk import frames, page_units

from tests import pages as fx
from tests.builders import present
from tests.timing import budget

images.signature(Image.new("L", (8, 8)))  # pHash loads its FFT module on first use: here, not in a test's time
BAND = page_units.BAND
ASPECT = 1.42  # series shape 1.42, pages 600 wide -> one page is 852 px


@functools.cache
def art(h=852, seed=1):
    return fx.manga_page((600, h), seed=seed, noise=3)


def find_band(page, where, aspect=ASPECT, cfg: Mapping[str, float] = BAND):
    return page_units.find_band(page, aspect, where, cfg)


def sig_matches(sig, text):
    """The live signature rule between a signature and a band's signature text."""
    return images.matches(sig, images.sig_from_str(text))


# ---------------------------------------------------------------- credit bands


@budget(load="pure")
def test_top_band_cut_on_the_gutter():
    """Rescaled page (840, not 852): the naive cut falls in the gutter: cut on the gutter, box from the top."""
    top = present(find_band(fx.stack(fx.warning_banner(), (40, 600), art(840)), "top"))
    assert top["how"] == "gap", "the cut is on the gutter"
    assert 240 <= top["cut"] <= 280, top  # on the white gutter, not in band or art
    assert top["box"] == [0, 0, 600, top["cut"]], "a top band's box runs from the page top to the cut"


@budget(load="pure")
def test_bottom_band_cut_on_the_gutter():
    """The same band under the page: cut on the gutter, box to the page's bottom."""
    page = fx.stack(art(840), (40, 600), fx.warning_banner())
    bottom = present(find_band(page, "bottom"))
    assert 840 <= bottom["cut"] <= 880, bottom
    assert bottom["box"] == [0, bottom["cut"], 600, page.height], "a bottom band's box runs from the cut to the bottom"


@budget(load="pure")
@pytest.mark.parametrize("where", ["top", "bottom"])
def test_band_signed_on_its_ink_whatever_the_gutter_share(where):
    """The band's signature matches the bare banner's ink box signature."""
    ban = fx.warning_banner()
    page = fx.stack(ban, (40, 600), art(840)) if where == "top" else fx.stack(art(840), (40, 600), ban)
    b = present(find_band(page, where))
    assert sig_matches(images.signature(ban.crop(page_units.ink_box(ban))), b["sig"]), "the band is signed on its ink"


@budget(load="pure")
def test_rescaled_page_cut_on_the_seam_not_at_the_naive_height():
    """Art rescaled to 903 px: h - w x aspect = 291 would cut 51 px into the art; the seam is at 240."""
    a = art(903)
    page = fx.stack(fx.warning_banner(), a)
    b = present(find_band(page, "top"))
    assert page.height - round(600 * ASPECT) == 291
    assert (b["how"], b["cut"], b["height"], b["where"], b["box"]) == ("seam", 240, 240, "top", [0, 0, 600, 240]), (
        "cut on the seam, not at the naive height"
    )
    assert np.array_equal(np.asarray(page.crop((0, b["cut"], 600, page.height))), np.asarray(a)), "the art is whole"


@budget(load="pure")
@pytest.mark.parametrize(
    ("page", "where"),
    [
        (art, "top"),
        (lambda: art(858), "top"),
        (lambda: fx.stack((300, 600), art()), "top"),
        (lambda: fx.stack(art(), (6, 600), fx.credit((600, 12))), "bottom"),
    ],
    ids=["series-shape", "within-band-tolerance", "white-only-extra", "band-under-band_min_px"],
)
def test_no_band_on_ordinary_pages_or_blank_extra_margin(page, where):
    """No band: page of the series shape, within band_tolerance, blank extra margin, an 18 px band (ink 12)."""
    assert find_band(page(), where) is None, "no band"


@budget(load="pure")
def test_a_band_under_band_min_px_after_its_gutter_cut_is_no_band():
    """A 3 px gutter at 857 past the naive 852: the cut moves onto it, leaving 13 px (< band_min_px 16)."""
    assert find_band(fx.stack(art(857), (3, 600), fx.credit((600, 10))), "bottom") is None, "a band is >= 16 px"


@budget(load="pure")
def test_a_band_without_ink_is_no_band():
    """A faint gradient band (luma 200-230: not uniform, nothing 40 off its border median) is no band."""
    faint = Image.fromarray(np.tile(np.linspace(200, 230, 600).astype(np.uint8)[None, :, None], (240, 1, 3)))
    assert find_band(fx.stack(faint, (40, 600), art(840)), "top") is None, "a band carries ink"


@budget(load="pure")
def test_the_gutter_nearest_the_naive_cut_wins():
    """Gutters at 840-880 (holding the naive 852) and 910-950: the cut is the naive 852 on the nearer one."""
    page = fx.stack(art(840), (40, 600), fx.blob(3, (600, 30)), (40, 600), fx.warning_banner())
    b = present(find_band(page, "bottom"))
    assert (b["how"], b["cut"], b["box"]) == ("gap", 852, [0, 852, 600, page.height]), "the nearest gutter wins"


@budget(load="pure")
@pytest.mark.parametrize(("gutter", "found"), [(60, False), (61, True)], ids=["at-the-limit", "one-px-over"])
def test_only_a_page_taller_than_the_limit_is_band_checked(gutter, found):
    """Shape 1.0 at 600 px, band_tolerance 0.5: a 900 px page (band + gutter + page) is not examined, 901 px is."""
    page = fx.stack(fx.warning_banner(), (gutter, 600), art(600))
    b = find_band(page, "top", aspect=1.0, cfg=dict(BAND, band_tolerance=0.5))
    assert (b is not None) is found, "only a page strictly taller than the limit is band-checked"


@budget(load="pure")
def test_a_nearer_later_gutter_replaces_an_earlier_one():
    """Gutters at 200-240 and 270-310, the naive top cut 300 in the second: the cut is 300, not 240."""
    page = fx.stack(fx.warning_banner(height=200), (40, 600), fx.blob(3, (600, 30)), (40, 600), art(842))
    b = present(find_band(page, "top"))
    assert (b["how"], b["cut"]) == ("gap", 300), "the later, nearer gutter wins"


@budget(load="pure")
def test_the_seam_nearest_the_naive_cut_wins():
    """No gutter, seams at 200 (banner / strip) and 240 (strip / art), naive cut 240: cut on the nearer seam."""
    page = fx.stack(fx.warning_banner(height=200), fx.webtoon_page((600, 40), seed=3), fx.webtoon_page((600, 852)))
    b = present(find_band(page, "top"))
    assert (b["how"], b["cut"]) == ("seam", 240), "the nearest seam wins"


@budget(load="pure")
def test_a_seam_band_of_exactly_band_seam_min_px_counts():
    """A 100 px banner straight under smooth art: a seam cut band_seam_min_px tall is a band."""
    b = present(find_band(fx.stack(fx.webtoon_page((600, 852)), fx.warning_banner(height=100)), "bottom"))
    assert (b["how"], b["cut"], b["height"]) == ("seam", 852, 100), "a seam band of band_seam_min_px counts"


def footer(x):
    """A 2-line LHTranslation-style footer, its text at x."""
    b = Image.new("RGB", (600, 40), "white")
    b.paste(fx.credit((480, 30)), (x, 6))
    return b


def footer_page(x):
    """After Being Called a Hero: a footer under the last story page behind a 12 px gutter (960 x 1378 + 62 px, the
    line at another x per chapter: Ch 14 / Ch 21), here at 600 wide.
    """
    return fx.stack(fx.blob(1, (600, 852)), (12, 600), footer(x))


@budget(load="pure")
@pytest.mark.parametrize("x", [0, 60, 120])
def test_thin_footer_cut_on_the_gutter(x):
    """A 40 px footer under band_seam_min_px counts: it is cut on the gutter; nothing on top."""
    b = present(find_band(footer_page(x), "bottom"))
    assert (b["how"], b["where"]) == ("gap", "bottom"), "a thin footer is cut on its gutter"
    assert 852 <= b["cut"] <= 864, b  # on the gutter
    assert find_band(footer_page(x), "top") is None, "nothing on top"


@budget(load="pure")
@pytest.mark.parametrize("x", [60, 120])
def test_thin_footer_matched_wherever_its_text_sits(x):
    """The ink box signature: footers with the line at another x match (one cluster, not 44)."""
    b0, b = (present(find_band(footer_page(v), "bottom")) for v in (0, x))
    assert sig_matches(images.sig_from_str(b0["sig"]), b["sig"]), "the ink box signature ignores the text's x"


@budget(load="pure")
@pytest.mark.parametrize(("height", "want"), [(60, None), (240, "seam")], ids=["60-px", "240-px"])
def test_a_band_cut_on_a_seam_needs_band_seam_min_px(height, want):
    """Gunslinger Girl / Immortal Hounds: a seam cut under band_seam_min_px 100 is art or a scan line: never cut."""
    b = find_band(fx.stack(art(852, seed=2), fx.warning_banner(height=height)), "bottom")
    assert (b and b["how"]) == want, "a seam band is at least band_seam_min_px"


@budget(load="pure")
def test_find_band_uses_the_one_gap_row_classifier_unchanged():
    """A Thunder Scans style WARNING band over a rescaled page, cut on the gutter: the result is the one the bulk
    converter's find_band gave (pinned), and the gutter comes from strip.blank_rows.
    """
    page = fx.stack(fx.warning_banner(), (40, 600), fx.webtoon_page((600, 840), seed=1))
    with mock.patch.object(strip, "blank_rows", wraps=strip.blank_rows) as spy:
        b = find_band(page, "top")
    assert spy.call_count == 1, "the gutter rows come from the one classifier"
    assert b == {
        "where": "top",
        "cut": 268,
        "height": 268,
        "how": "gap",
        "sig": "80fe80ff81aa80ff|0.3730769230769231",
        "box": [0, 0, 600, 268],
    }, "the band is the bulk converter's"


# ---------------------------------------------------------------- ink box


@budget(load="pure")
@pytest.mark.parametrize(("delta", "want"), [(41, (10, 5, 11, 6)), (40, None)], ids=["41-off", "40-off"])
def test_ink_is_more_than_ink_delta_from_the_border_median(delta, want):
    """A white band with one pixel delta luma darker: ink only beyond INK_DELTA 40; the box is that pixel."""
    a = np.full((20, 50, 3), 255, np.uint8)
    a[5, 10] = 255 - delta
    assert page_units.ink_box(Image.fromarray(a)) == want, "ink is more than INK_DELTA off the border median"


@budget(load="pure")
def test_ink_box_on_a_dark_band():
    """The background is the border's median, dark here: a light pixel is the ink."""
    a = np.full((20, 50, 3), 20, np.uint8)
    a[12, 30] = 61
    assert page_units.ink_box(Image.fromarray(a)) == (30, 12, 31, 13), "ink is relative to the border tone"


@budget(load="pure")
def test_ink_box_spans_the_text():
    """A credit pasted at (100, 5) on a white band: the box lies within the pasted 300x30 credit."""
    b = Image.new("RGB", (600, 40), "white")
    b.paste(fx.credit((300, 30)), (100, 5))
    x0, y0, x1, y1 = present(page_units.ink_box(b))
    assert x0 >= 100, (x0, y0, x1, y1)
    assert x1 <= 400, (x0, y0, x1, y1)
    assert y0 >= 5, (x0, y0, x1, y1)
    assert y1 <= 35, (x0, y0, x1, y1)


@budget(load="pure")
def test_ink_box_of_a_plain_band_is_none():
    """No pixel off the background: no ink."""
    assert page_units.ink_box(Image.new("RGB", (600, 40), (200, 200, 200))) is None, "a plain band has no ink"


# ---------------------------------------------------------------- end-page size rule (page_candidates)

# Recorded page sizes (CBZ headers) of the pilot "Akuyaku Kizoku to shite Hitsuyou na Sore" originals and
# Gunslinger Girl; the pilot's series shape is 1723 / 1200.
AKUYAKU = [round(1723 / 1200, 3)]
STORY = (1200, 1723)
AK_CH8 = [STORY] * 29 + [(1200, 1726), (2133, 1277)]  # p31: Reset Scans patron page, wide
AK_CH12 = [STORY, (1200, 3418)] + [STORY] * 29 + [(1200, 3181)]  # p2 / p32: banded (band scan's pages)
AK_CH16 = [(1080, 1503)] * 2 + [STORY] * 23 + [(1080, 1551), (1080, 1536)]
AK_CH17 = [(1200, 1855), (1200, 1706)] + [STORY] * 36 + [(1200, 1855)]
AK_CH22 = [(512, 728)] + [STORY] * 32 + [(1200, 1855)]
GG_CH13 = [
    (800, h)
    for h in (
        1161,
        1172,
        1199,
        1124,
        1144,
        1136,
        1169,
        1177,
        1202,
        1166,
        1205,
        1192,
        1186,
        1134,
        1173,
        1136,
        1182,
        1147,
        1193,
        1121,
        1147,
        1129,
        1187,
        1159,
        1177,
        1121,
        1154,
        1130,
        1184,
        1222,
        1191,
        1130,
    )
]  # Gunslinger Girl ch13 (scanned volume)


def names(n):
    return [f"{i:03d}.jpg" for i in range(1, n + 1)]


def cands(sizes, aspects=AKUYAKU, pages=None, banded=(), joined=()):
    """page_candidates on a chapter's content pages, as 1-based page numbers of the download; banded and joined are
    download indexes.
    """
    content = strip.content_indexes(pages or names(len(sizes)))
    position = {i: k for k, i in enumerate(content)}
    found = page_units.page_candidates(
        [sizes[i] for i in content], aspects, {position[i] for i in banded}, {position[i] for i in joined}
    )
    return [(content[k] + 1, why) for k, why in found]


@budget(load="pure")
def test_other_width_pages_at_both_ends():
    """Akuyaku ch16: 1080-wide pages among 1200 -> p1, p2 (credit pages), p26 (another source's end page), p27."""
    assert cands(AK_CH16) == [(1, "width"), (2, "width"), (26, "width"), (27, "width")], "another width is listed"


@budget(load="pure")
def test_taller_signoffs_listed_volume_cover_inside_the_margin_not():
    """Akuyaku ch17: p1 / p39 Nerissa pages at 1855 listed; p2 (vol 3 cover, 1706: -1.0%) inside the 1% margin."""
    assert cands(AK_CH17) == [(1, "tall"), (39, "tall")], "taller sign-offs listed, a page inside the margin not"


@budget(load="pure")
def test_small_volume_cover_listed():
    """Akuyaku ch22: p1 vol 4 cover 512x728 (kept by default), p34 sign-off."""
    assert cands(AK_CH22) == [(1, "width"), (34, "tall")], "a small cover and a tall sign-off are listed"


@budget(load="pure")
def test_band_pages_excluded():
    """Akuyaku ch12: p2 (1200x3418) and p32 (1200x3181) carry Reset Scans bands: the band scan owns them."""
    assert cands(AK_CH12, banded={1, 31}) == [], "band pages are never size candidates"


@budget(load="pure")
def test_tall_pages_with_no_band_found_stay_listed():
    """The same pages when the band scan found nothing on them: still page items."""
    assert cands(AK_CH12) == [(2, "tall"), (32, "tall")], "a tall page with no band stays listed"


@budget(load="pure")
def test_wide_page_that_is_not_two_pages_is_listed():
    """P1: Akuyaku ch8 p31, the 2133x1277 Reset Scans patron page: 2 x its aspect is 1.20, not within 10% of 1.436."""
    assert cands(AK_CH8) == [(31, "width")], "a wide page that is no spread is listed"


@budget(load="pure")
def test_monsters_cant_clean_credits_page_is_no_spread():
    """P1: Monsters Can't Clean Ch.8's 1073x598 credits page after 728x1032 pages (2 x 0.557 = 1.11 vs 1.42)."""
    assert cands([(728, 1032)] * 35 + [(1073, 598)], [round(1032 / 728, 3)]) == [(36, "width")], "no spread"


@budget(load="pure")
def test_spread_excluded():
    """Two story pages side by side (2400x1723 among 1200x1723): 2 x its aspect is the story pages' aspect."""
    assert cands([STORY] * 29 + [(2400, 1723)]) == [], "a spread is never listed"


@budget(load="pure")
def test_variable_scan_heights_use_the_chapter_range():
    """Gunslinger Girl ch13 (heights 1121-1205 in the middle): only p30 (1222, +1.4% above the range) is listed."""
    assert cands(GG_CH13, aspects=[1.45]) == [(30, "tall")], "the middle pages' range, widened by 1%"


@budget(load="pure")
def test_cover_named_pages_and_joined_slices_never_listed():
    """An inserted 000_cover and a 1,500 + 100 slice at the end (Little Lord ch3) that slice repair joins."""
    sizes = [(700, 1000)] + [(1125, 1600)] * 6 + [(1125, 1500), (1125, 100)]
    assert cands(sizes, [1600 / 1125], ["000_cover.jpg", *names(8)], joined={7, 8}) == [], (
        "cover-named pages and joined slices are never listed"
    )


@budget(load="pure")
def test_a_joined_middle_slice_does_not_widen_the_middle_pages_range():
    """A 1,500 + 100 slice pair in the middle that slice repair joins: the 100 px sliver is no page size, so a short
    end page is still listed.
    """
    sizes = [(1125, 1600)] * 4 + [(1125, 1500), (1125, 100)] + [(1125, 1600)] * 4 + [(1125, 900)]
    assert cands(sizes, [1600 / 1125], joined={4, 5}) == [(11, "short")], "the sliver's aspect is left out"


@budget(load="pure")
def test_unjoined_sliver_is_listed():
    """P2: Outei Denka ondine Ch.10 p32, a 1350x877 credits page after 1350x1920 pages: a slice_pairs hit not joined."""
    sizes = [(1350, 1920)] * 31 + [(1350, 877)]
    assert strip.slice_pairs(sizes) == [31]
    assert cands(sizes, [round(1920 / 1350, 3)]) == [(32, "short")], "an unjoined sliver is listed"


@budget(load="pure")
def test_a_chapter_of_cover_named_pages_only_has_no_candidates():
    """No content page: nothing to list."""
    assert cands([(700, 1000)], pages=["cover.jpg"]) == [], "no content page, no candidate"


# ---------------------------------------------------------------- same-size text end page

SIZE = (300, 426)


def text_credits_page(size=SIZE):
    """A scanlator credits page the size of the story pages (Liz, the Magical Tool Maker Art Lapsa Ch.18 026.webp):
    black text lines on white and one small coloured logo, no shading.
    """
    w, h = size
    a = np.full((h, w, 3), 255, np.uint8)
    for k, y in enumerate(range(h // 6, h - h // 6, h // 14)):
        a[y : y + h // 40, w // 8 + (k % 3) * w // 20 : w - w // 8] = 0
    a[h // 20 : h // 20 + w // 7, w // 2 - w // 14 : w // 2 + w // 14] = (220, 40, 160)
    return Image.fromarray(a)


def gray_story_page(seed, size=SIZE):
    """A gray story page: screentone shading (mid-tones) and ink lines on white."""
    a = np.full((size[1], size[0]), 245, np.uint8)
    rng = np.random.default_rng(seed)
    for _ in range(4):
        x, y = int(rng.integers(0, size[0] - 120)), int(rng.integers(0, size[1] - 150))
        a[y : y + 150, x : x + 120] = int(rng.integers(90, 180))
    return Image.fromarray(a).convert("RGB")


def with_stamp(im):
    """Gray page with a small red stamp / coloured SFX."""
    a = np.asarray(im.convert("RGB")).copy()
    a[20:40, 20:60] = (210, 30, 30)
    return Image.fromarray(a)


def gray_tail(last):
    """text_features of a 10-page gray chapter's last 3 content pages: 2 gray story pages, then `last`."""
    tf = page_units.text_features
    return {7: tf(gray_story_page(1)), 8: tf(gray_story_page(2)), 9: last}


@budget(load="pure")
def test_colourful_text_page_closing_a_gray_chapter():
    """Liz, the Magical Tool Maker Art Lapsa Ch.18 026: a same-size credits page is the candidate."""
    texts = gray_tail(page_units.text_features(text_credits_page()))
    assert page_units.end_text_page([SIZE] * 10, texts) == 9, "a credits page closing a gray chapter"


@budget(load="pure")
def test_not_in_a_colour_chapter():
    """The other tail pages are coloured: no gray chapter, no candidate."""
    tf = page_units.text_features
    texts = {7: tf(fx.blob(1, SIZE)), 8: tf(fx.blob(2, SIZE)), 9: tf(text_credits_page())}
    assert page_units.end_text_page([SIZE] * 10, texts) is None, "only in a gray chapter"


@budget(load="pure")
def test_not_another_size():
    """A 300x520 last page is the size rule's, not this one's."""
    texts = gray_tail(page_units.text_features(text_credits_page()))
    assert page_units.end_text_page([SIZE] * 9 + [(300, 520)], texts) is None, "only a page of the modal size"


@budget(load="pure")
@pytest.mark.parametrize(
    "last",
    [lambda: with_stamp(gray_story_page(3)), lambda: text_credits_page().convert("L").convert("RGB")],
    ids=["shaded-story-page", "no-colour"],
)
def test_not_a_shaded_or_colourless_page(last):
    """A shaded story page with a stamp, or the credits page without its colour: no candidate."""
    texts = gray_tail(page_units.text_features(last()))
    assert page_units.end_text_page([SIZE] * 10, texts) is None, "mostly plain, little mid-tone, some colour"


@budget(load="pure")
def test_a_one_page_chapter_has_no_text_end_page():
    """Fewer than 2 content pages: no modal page to compare with."""
    texts = {0: page_units.text_features(text_credits_page())}
    assert page_units.end_text_page([SIZE], texts) is None, "no text end page under 2 pages"


@budget(load="pure")
@pytest.mark.parametrize(
    ("fill", "want"),
    [
        ((128, 128, 128), {"plain": 0.875, "mid": 0.125, "chroma": 0.0}),
        ((255, 0, 0), {"plain": 0.875, "mid": 0.0, "chroma": 0.125}),
    ],
    ids=["gray-block", "red-block"],
)
def test_text_features_shares(fill, want):
    """400x400 white page, a 100x200 block (1/8) away from the border: plain / mid-tone / coloured shares."""
    a = np.full((400, 400, 3), 255, np.uint8)
    a[100:300, 150:250] = fill
    assert page_units.text_features(Image.fromarray(a)) == pytest.approx(want), "shares of the page's pixels"


# ---------------------------------------------------------------- decide


def row(row_id, verdict, series_key="Series", decider="import", decided_at="2026-01-01T00:00:00+00:00"):
    return {
        "id": row_id,
        "verdict": verdict,
        "series_key": series_key,
        "key": f"k{row_id}",
        "decider": decider,
        "decided_at": decided_at,
    }


def decider(rows):
    """decide_unit over {(kind, key text): row}: exact keys stand in for the store's match rules."""
    return lambda kind, key: rows.get((kind, key))


def scan_of(tmp_path, ims, names=None, aspects=(ASPECT,)):
    """scan() of a chapter of these page images, saved as PNG."""
    names = names or [f"{i:03d}.png" for i in range(1, len(ims) + 1)]
    paths = []
    for name, im in zip(names, ims, strict=True):
        paths.append(tmp_path / name)
        im.save(paths[-1])
    return page_units.scan(paths, names, [im.size for im in ims], [images.is_uniform(im) for im in ims], list(aspects))


def edge(sc, page):
    return next(u for u in sc["edges"] if u["page"] == page)


def hand_scan(frame_on=None):
    """A 6-page scan of same-size pages (edges 0, 1, 3, 4, 5; no bands, no slices); page `frame_on` has a frame."""
    return {
        "layout": "pages",
        "split": False,
        "content": list(range(6)),
        "sizes": [[600, 852]] * 6,
        "edges": [{"page": i, "sig": f"s{i}", "frame": f"f{i}" if i == frame_on else None} for i in (0, 1, 3, 4, 5)],
        "white": [],
        "bands": {},
        "rows": {},
        "aspects": [ASPECT],
    }


@budget(2.0, load="fs")
def test_an_edge_page_decided_junk_is_dropped(tmp_path):
    """The last page's signature decided junk: it is dropped; the other edge pages stay undecided units."""
    sc = scan_of(tmp_path, [art(seed=s) for s in range(6)])
    out = page_units.decide(sc, decider({("page", edge(sc, 5)["sig"]): row(1, "junk")}))
    assert out["edit"] == {"split": False, "drop_pages": [5], "bands": [], "join": []}, "a junk page is dropped"
    assert [u["page"] for u in out["undecided"]] == [0, 1, 3, 4], "undecided edge pages are listed, the middle not"


@budget(2.0, load="fs")
@pytest.mark.parametrize(("all_white", "want"), [(False, [0, 5]), (True, [])], ids=["white-ends", "every-page-white"])
def test_white_end_pages_are_dropped_unless_every_page_is_white(tmp_path, all_white, want):
    """White pages at either end are dropped with no decision; a chapter of white pages only keeps them all."""
    white = Image.new("RGB", (600, 852), "white")
    ims = [white] * 6 if all_white else [white] + [art(seed=s) for s in range(4)] + [white]
    out = page_units.decide(scan_of(tmp_path, ims), decider({}))
    assert out["edit"]["drop_pages"] == want, "white end pages go, unless that would drop every page"
    assert all(u["page"] not in want for u in out["undecided"]), "a dropped page is no unit"


def banded_chapter(tmp_path, on):
    """6 art pages, a WARNING band stacked over page `on` behind a gutter."""
    ims = [art(seed=s) for s in range(6)]
    ims[on] = fx.stack(fx.warning_banner(), (40, 600), art(840, seed=on))
    return scan_of(tmp_path, ims)


@budget(3.0, load="fs")
def test_a_junk_band_is_cropped_to_its_kept_box(tmp_path):
    """A band on middle page 2 decided junk: the page is kept from the cut to its bottom."""
    sc = banded_chapter(tmp_path, 2)
    (b,) = sc["bands"]["2"]
    out = page_units.decide(sc, decider({("band", b["sig"]): row(1, "junk")}))
    assert out["edit"]["bands"] == [{"page": 2, "box": [0, b["cut"], 600, 1120]}], "the band is cropped off"
    assert out["edit"]["drop_pages"] == [], "a banded page is cropped, not dropped"


@budget(3.0, load="fs")
def test_an_undecided_band_on_an_edge_page_makes_the_page_a_band_candidate(tmp_path):
    """A band on edge page 0 that no decision covers: a band unit, and the page is undecided with reason 'band'."""
    sc = banded_chapter(tmp_path, 0)
    (b,) = sc["bands"]["0"]
    out = page_units.decide(sc, decider({}))
    (page,) = [u for u in out["undecided"] if u["page"] == 0 and u["kind"] != "band"]
    (band,) = [u for u in out["undecided"] if u["kind"] == "band"]
    assert page["reasons"] == ["band"], "the edge page's reason is its undecided band"
    assert (band["page"], band["key"], band["where"]) == (0, b["sig"], "top"), "the band is a unit"
    assert out["edit"]["bands"] == [], "an undecided band crops nothing"


@budget(load="pure")
def test_a_stitched_chapter_is_only_split():
    """Pages 1.9 x the series shape: the scan says split, decoding nothing; the edit is split and nothing else."""
    sizes = [(600, round(600 * ASPECT * 1.9))] * 4
    sc = page_units.scan([None] * 4, names(4), sizes, [False] * 4, [ASPECT])
    assert sc == {"layout": "pages", "split": True, "marks": []}, "a stitched chapter has no page or logo units"
    out = page_units.decide(sc, decider({}))
    assert out == {"edit": {"split": True, "drop_pages": [], "bands": [], "join": []}, "undecided": []}, (
        "a stitched chapter is only split"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("page", "frame", "dropped"),
    [
        (row(1, "keep"), row(2, "junk", ""), False),
        (row(1, "junk", ""), row(2, "keep"), False),
        (row(1, "keep", ""), row(2, "junk", ""), False),
        (row(1, "junk"), row(2, "keep", ""), True),
        (None, row(2, "junk", ""), True),
        (row(1, "keep"), row(2, "junk", decider="claude"), True),
        (
            row(1, "keep", decider="human"),
            row(2, "junk", decider="claude", decided_at="2026-02-01T00:00:00+00:00"),
            False,
        ),
    ],
    ids=[
        "series-page-keep",
        "series-frame-keep",
        "global-keep-first",
        "series-junk-beats-global-keep",
        "frame-only",
        "pipeline-beats-import",
        "pipeline-older-keep-first",
    ],
)
def test_the_page_and_frame_decision_ranking_first_decides(page, frame, dropped):
    """Page and frame decisions both apply in lookup order: the pipeline's (keep first), then imported ones: the
    series' before global, keep before junk within a scope.
    """
    rows = {("page", "s5"): page, ("frame", "f5"): frame}
    out = page_units.decide(hand_scan(frame_on=5), decider({k: v for k, v in rows.items() if v}))
    assert (5 in out["edit"]["drop_pages"]) is dropped, "the decision ranking first decides the page"


@budget(load="pure")
def test_on_a_tie_the_page_decision_wins():
    """A series junk page decision and a series junk frame decision rank alike: the page's row decides."""
    unit = {"page": 5, "sig": "s5", "frame": "f5"}
    rows = {("page", "s5"): row(1, "junk"), ("frame", "f5"): row(2, "junk")}
    assert present(page_units.page_decision(unit, decider(rows)))["id"] == 1, "the page's decision wins a tie"


@budget(load="pure")
def test_an_undecided_framed_page_is_offered_by_its_frame():
    """An undecided page with a frame is a frame unit; one without is a page unit."""
    out = page_units.decide(hand_scan(frame_on=5), decider({}))
    assert [(u["kind"], u["key"]) for u in out["undecided"]] == [
        ("page", "s0"),
        ("page", "s1"),
        ("page", "s3"),
        ("page", "s4"),
        ("frame", "f5"),
    ], "offered by its frame when it has one, else by the whole page"


@budget(3.0, load="fs")
def test_slice_joins_are_found_among_kept_pages(tmp_path):
    """A cover, a junk first page, then pages with a page cut into 700 + 152 px: the join names download indexes."""
    whole = fx.manga_page((600, 852), seed=9, noise=4)
    ims = (
        [art(seed=8)] + [art(seed=s) for s in range(4)] + [whole.crop((0, 0, 600, 700)), whole.crop((0, 700, 600, 852))]
    )
    ims.append(art(seed=7))
    sc = scan_of(tmp_path, ims, names=["000_cover.png", *names(len(ims) - 1)])
    out = page_units.decide(sc, decider({("page", edge(sc, 1)["sig"]): row(1, "junk")}))
    assert out["edit"]["drop_pages"] == [1], "the junk first page is dropped"
    assert out["edit"]["join"] == [[5, 6]], "the page and its sliver are joined, by download index"


def light_rows(h, row):
    """h rows of 400 px, each the given row of luma values (RGB)."""
    return Image.fromarray(np.repeat(np.tile(np.asarray(row, np.uint8), (h, 1))[..., None], 3, axis=2))


@budget(load="pure", seconds=0.5)
@pytest.mark.parametrize(
    ("row", "how"),
    [
        ([250] * 399 + [230], "seam"),
        ([250] * 399 + [231], "gap"),
        ([247, 253] * 200, "seam"),
        ([248, 252] * 200, "gap"),
    ],
    ids=["range-20", "range-19", "std-3", "std-2"],
)
def test_gutter_rows_have_std_under_3_and_range_under_20(row, how):
    page = fx.stack(fx.warning_banner(400, 160), light_rows(40, row), fx.manga_page((400, 568), seed=1, noise=3))
    b = present(page_units.find_band(page, 1.42, "top"))
    assert b["how"] == how, "at analysis width a near-white row is a gutter row only with std < 3 and range < 20"


@budget(2.0, load="fs")
@pytest.mark.parametrize("end_page", ["endpage_akuyaku_ch18_p37.jpg", "endpage_akuyaku_ch22_p34.jpg"])
def test_a_frame_decision_on_a_real_end_page_drops_it_in_later_chapters(tmp_path, end_page):
    """Akuyaku Kizoku ch17 / 18 / 22 end pages (real JPEGs): one template, other text, so their page signatures differ
    but their frames match. A frame decision on ch17's drops the end page of a later chapter, and only that page.
    """
    first = Image.open(fx.PAGE_FIX / "endpage_akuyaku_ch17_p39.jpg")
    later = Image.open(fx.PAGE_FIX / end_page)
    assert not images.matches(images.signature(first), images.signature(later)), "no page decision covers it"
    decided = frames.frame(first)
    story = [fx.manga_page((300, 464), seed=s, noise=3) for s in range(1, 9)]
    sc = scan_of(tmp_path, [*story, later.convert("RGB")], aspects=[464 / 300])

    def decide_unit(kind, key):
        return row(1, "junk") if kind == "frame" and frames.matches(frames.from_key(key), decided) else None

    out = page_units.decide(sc, decide_unit)
    assert out["edit"]["drop_pages"] == [8], out["edit"]


@budget(3.0, load="fs")
def test_an_undecided_band_on_a_middle_page_is_a_candidate_at_1_chapter(tmp_path):
    """D5.16: a band on middle page 2 that no decision covers is a unit with the per-chapter reason 'band'."""
    sc = banded_chapter(tmp_path, 2)
    (band,) = [u for u in page_units.decide(sc, decider({}))["undecided"] if u["kind"] == "band"]
    assert (band["page"], band["reasons"]) == (2, ["band"]), "offered by its own chapter, on any page"


@budget(3.0, load="fs")
def test_an_unsignable_band_is_no_band_and_its_edge_page_is_judged_as_a_page(tmp_path):
    """A band whose ink signs as a flat image: no decision could be written on it, so it is no unit, and its page is
    no band page: the edge-page size rule still offers the page.
    """
    sc = banded_chapter(tmp_path, 0)
    sc["bands"]["0"][0]["sig"] = images.sig_to_str(((0,), 2.5))
    out = page_units.decide(sc, decider({}))
    assert [u for u in out["undecided"] if u["kind"] == "band"] == [], "an unsignable band is no unit"
    (page,) = [u for u in out["undecided"] if u["page"] == 0]
    assert page["reasons"] == ["tall"], "its page is judged as a page"
