"""Template frames (SPEC "Junk decisions", units; manga_pipeline/junk/frames.py): the frame signature, the content
filter and the match rule. Ported from the bulk converter's tests/test_frames.py (its cluster, decision table and
cross-series index tests are not: that code is not ported).

fixtures_pages/frames_pages.npz holds the 48x72 RGB shrunk pages (and original sizes) of real edge pages:
  akuyaku_ch17_p39 / ch18_p37 / ch22_p34 / ch23_p39   Nerissa sign-offs, "Akuyaku Kizoku to shite Hitsuyou na Sore"
                                                        (same template, different middle text; 1200x1855)
  akuyaku_ch24_p34 / blacksmith_ch13_p62               Silas "Special thanks" across series ("A Female Blacksmith ...")
  akuyaku_ch24_p1 / blacksmith_ch16_p63                Silas commission page across the same two series
  ennead_ch150_p1 / _p17                               MangaBuddy / MangaJinx banners, ENNEAD
  neverland_ch1_p2                                     white-margin official page, The Promised Neverland
  yumekui_ch7_p17                                      near-black page, Yumekui Merry"""

import re

import numpy as np
import pytest
from PIL import Image

from manga_pipeline.junk import frames

from tests import pages as fx
from tests.timing import budget

_NPZ = np.load(fx.PAGE_FIX / "frames_pages.npz")
PAGES = {
    str(n): (s, tuple(int(v) for v in z)) for n, s, z in zip(_NPZ["names"], _NPZ["small"], _NPZ["sizes"], strict=True)
}


def frame(name):
    small, (w, h) = PAGES[name]
    return frames.frame_of_small(small, h / w)


def tone(v, aspect=1.4):
    """A frame whose every cell is the gray tone v (distances between two tones are exactly their difference)."""
    return frames.Frame(np.full(frames.CELLS * 3, v, np.uint8), aspect)


def cells(*runs, aspect=1.4):
    """A frame built from (rgb, count) runs of cells; the counts add up to frames.CELLS (1,056)."""
    return frames.Frame(np.concatenate([np.tile(np.array(rgb, np.uint8), n) for rgb, n in runs]), aspect)


# ---------------------------------------------------------------- signature


@budget(load="pure")
def test_frame_keeps_only_the_outer_margin_cells():
    """48x72 cells, 4 columns / 6 rows a side: 1,056 cells, 3,168 bytes, the middle masked out."""
    small = np.zeros((frames.H, frames.W, 3), np.uint8)
    small[frames.MARGIN_Y : -frames.MARGIN_Y, frames.MARGIN_X : -frames.MARGIN_X] = 255
    f = frames.frame_of_small(small, 1.5)
    assert (frames.CELLS, f.vec.size, int(f.vec.max()), f.aspect) == (1056, 3168, 0, 1.5), (
        "the middle (white here) must not reach the frame"
    )


@budget(load="pure")
def test_shrunk_page_of_another_shape_is_refused():
    """frame_of_small takes only an (H, W, 3) array: a 48x48 page raises naming both shapes."""
    with pytest.raises(ValueError, match=re.escape("shrunk page must be (72, 48, 3), got (48, 48, 3)")):
        frames.frame_of_small(np.zeros((48, 48, 3), np.uint8), 1.0)


@budget(load="pure")
def test_frame_of_a_draft_decode_takes_the_aspect_of_the_original_size():
    """`size` is the original page's (width, height): a 480x720 page given size (1200, 1855) has aspect 1855 / 1200."""
    f = frames.frame(Image.new("RGB", (480, 720), (90, 90, 90)), size=(1200, 1855))
    assert f.aspect == pytest.approx(1855 / 1200), "the aspect is height / width of `size`, not of the decode"


@budget(load="pure")
def test_frame_of_a_page_defaults_to_its_own_size():
    """No `size`: aspect is the image's height / width (480x720 -> 1.5)."""
    assert frames.frame(Image.new("RGB", (480, 720), (90, 90, 90))).aspect == 1.5, "aspect = height / width"


@budget(load="pure")
def test_stored_frame_of_another_geometry_fails_fast():
    """A frame stored under other W / H / MARGIN_X / MARGIN_Y (1,000 cells) raises a clear error, not numpy's."""
    with pytest.raises(ValueError, match=re.escape("stored frame has 3000 bytes, expected 3168")):
        frames.from_bytes(bytes(1000 * 3), 1.5)
    f = frames.from_bytes(frame("akuyaku_ch17_p39").vec.tobytes(), 1.5)
    assert (f.vec.size, f.aspect) == (frames.CELLS * 3, 1.5), "a vector of this geometry loads as stored"


@budget(load="pure")
def test_key_text_round_trips():
    """A frame's decision key gives back the same vector and aspect."""
    f = frame("akuyaku_ch17_p39")
    back = frames.from_key(frames.key_text(f))
    assert (back.vec.tobytes(), back.aspect) == (f.vec.tobytes(), f.aspect), "from_key inverts key_text"


# ---------------------------------------------------------------- content filter


@budget(load="pure")
@pytest.mark.parametrize(
    "name", ["akuyaku_ch17_p39", "akuyaku_ch24_p34", "blacksmith_ch16_p63", "ennead_ch150_p1", "ennead_ch150_p17"]
)
def test_template_pages_have_content(name):
    """Real scanlator template pages (Nerissa, Silas, MangaBuddy banners) pass the content filter."""
    assert frames.has_content(frame(name)), f"{name} is a scanlator template page"


@budget(load="pure")
def test_white_margin_official_page_has_no_content():
    """The Promised Neverland ch1 p2: a paper-white margin with sparse line art is not template content."""
    assert not frames.has_content(frame("neverland_ch1_p2")), "a white margin is no template"


@budget(load="pure")
def test_near_black_page_has_no_content():
    """Yumekui Merry REM 7 p17: a near-black margin is not template content."""
    assert not frames.has_content(frame("yumekui_ch7_p17")), "a near-black margin is no template"


@budget(load="pure")
@pytest.mark.parametrize("v", [0, 255, 128], ids=["black", "white", "mid-gray"])
def test_plain_tone_has_no_content(v):
    """One plain tone is never content, even a mid-tone gray that would count as ink."""
    assert not frames.has_content(tone(v)), "one plain tone is never content"


@budget(load="pure")
@pytest.mark.parametrize("n, want", [(897, True), (898, False)], ids=["84.9pct-not-plain", "85.0pct-plain"])
def test_plain_share_at_85_percent_makes_a_frame_plain(n, want):
    """n of 1,056 cells at luma 128, the rest at 60 (all mid-tone ink): 898 / 1,056 >= 85% is one plain tone."""
    assert frames.has_content(cells(((128,) * 3, n), ((60,) * 3, 1056 - n))) is want, "plain from 85% of the cells"


@budget(load="pure")
@pytest.mark.parametrize("v, want", [(136, False), (137, True)], ids=["8-from-median", "9-from-median"])
def test_cells_within_8_luma_of_the_median_count_as_the_same_tone(v, want):
    """600 cells at 128 (the median) and 456 at v: 8 luma away is the same tone (plain), 9 is not."""
    assert frames.has_content(cells(((128,) * 3, 600), ((v,) * 3, 456))) is want, "same tone within 8 luma"


@budget(load="pure")
@pytest.mark.parametrize(
    "v, want", [(39, False), (40, True), (199, True), (200, False)], ids=["below-lo", "at-lo", "below-hi", "at-hi"]
)
def test_mid_tone_ink_is_luma_40_to_199(v, want):
    """Half the cells black, half at luma v: ink only when 40 <= v < 200 (half the cells, over the 30% share)."""
    assert frames.has_content(cells(((0,) * 3, 528), ((v,) * 3, 528))) is want, "mid-tone ink is 40 <= luma < 200"


@budget(load="pure")
@pytest.mark.parametrize("blue, want", [(231, False), (230, True)], ids=["spread-24", "spread-25"])
def test_coloured_cells_need_a_channel_spread_over_24(blue, want):
    """Half black, half light yellow (luma >= 200, not mid-tone): ink only when its spread 255 - blue is > 24."""
    assert frames.has_content(cells(((0,) * 3, 528), ((255, 255, blue), 528))) is want, "colour ink: spread > 24"


@budget(load="pure")
@pytest.mark.parametrize("ink, want", [(316, False), (317, True)], ids=["29.9pct", "30.0pct"])
def test_ink_share_of_30_percent_is_content(ink, want):
    """370 black, `ink` cells at luma 128, the rest white: 317 / 1,056 reaches the 30% ink share, 316 does not."""
    f = cells(((0,) * 3, 370), ((128,) * 3, ink), ((255,) * 3, 1056 - 370 - ink))
    assert frames.has_content(f) is want, "content from a 30% ink share"


# ---------------------------------------------------------------- matching


@budget(load="pure")
@pytest.mark.parametrize(
    "a, b",
    [
        ("akuyaku_ch17_p39", "akuyaku_ch18_p37"),
        ("akuyaku_ch17_p39", "akuyaku_ch22_p34"),
        ("akuyaku_ch17_p39", "akuyaku_ch23_p39"),
        ("akuyaku_ch24_p34", "blacksmith_ch13_p62"),
        ("akuyaku_ch24_p1", "blacksmith_ch16_p63"),
        ("ennead_ch150_p1", "ennead_ch150_p17"),
    ],
    ids=["nerissa-18", "nerissa-22", "nerissa-23", "silas-thanks", "silas-commission", "ennead-banners"],
)
def test_one_template_matches_across_chapters_and_series(a, b):
    """Nerissa sign-offs with different middle text, Silas pages across 2 series, ENNEAD's 2 banners: 1 template."""
    assert frames.matches(frame(a), frame(b)), f"{a} and {b} carry the same template"


@budget(load="pure")
@pytest.mark.parametrize(
    "a, b",
    [("akuyaku_ch17_p39", "ennead_ch150_p1"), ("akuyaku_ch24_p34", "akuyaku_ch24_p1")],
    ids=["nerissa-vs-banner", "silas-thanks-vs-commission"],
)
def test_different_templates_do_not_match(a, b):
    """Two different templates (also of the same scanlator) stay apart."""
    assert not frames.matches(frame(a), frame(b)), "different templates never match"


@budget(load="pure")
@pytest.mark.parametrize("v, want", [(28, True), (29, False)], ids=["rms-18", "rms-19"])
def test_frames_match_up_to_rms_18(v, want):
    """Tones 10 and v differ by v - 10 on every channel: RMS 18 matches, 19 does not."""
    assert (frames.distance(tone(10), tone(v)), frames.matches(tone(10), tone(v))) == (float(v - 10), want), (
        "match up to RMS 18"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    "k, want",
    [(1.10, True), (1.136, True), (1.137, False), (1.20, False)],
    ids=["10pct", "edge-in", "edge-out", "20pct"],
)
def test_aspect_ratios_match_within_12_percent(k, want):
    """Nerissa ch17 with its aspect x k: |a - b| <= 0.12 * max holds up to k = 1 / 0.88 = 1.1364."""
    f = frame("akuyaku_ch17_p39")
    assert frames.matches(f, frames.Frame(f.vec, f.aspect * k)) is want, "aspects within 12% of the larger"


@budget(load="pure")
def test_aspect_difference_of_exactly_12_percent_matches():
    """Aspects 1.0 and 0.88 differ by exactly 0.12 * max: the tolerance is inclusive."""
    assert frames.matches(tone(10, 1.0), tone(10, 0.88)), "|a - b| == 0.12 * max(a, b) is within tolerance"
