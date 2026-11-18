"""Page images (images; contracts section 1).

The formats present in the library (JPEG, PNG, lossy/lossless WebP, AVIF, GIF), header-only dimensions, open_image's
decode rules, real alpha (open_flat), to_gray, save_png and the page classes (gray-mark included).
Ported from the bulk converter's tests/test_formats.py and tests/test_alpha_pages.py.
"""

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms

from manga_pipeline import images

from tests import pages as fx
from tests.builders import present
from tests.timing import budget

WINDOWS_CMYK = Path(r"C:\Windows\System32\spool\drivers\color\RSWOP.icm")
# the bulk converter's integrity section: colour_block_spread 22, tint_p99_spread 6, mark caps 1% / 35% / 8% / 5000 px2
LIMITS = {
    "colour_block_spread": 22,
    "tint_p99_spread": 6,
    "mark_max_frac": 0.01,
    "mark_max_w": 0.35,
    "mark_max_h": 0.08,
    "mark_min_area": 5000,
}
BADGE = (900, 1650, 260, 40)  # Reset Scans badge (x, y, w, h) on Akuyaku Kizoku's 1200 x 1723 pages
W, H = 120, 400  # alpha pages


def riff(*chunks):
    """Return a RIFF/WEBP stream of (fourcc, payload) chunks, each padded to an even size as the container requires."""
    body = b"".join(c + len(p).to_bytes(4, "little") + p + b"\0" * (len(p) & 1) for c, p in chunks)
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def badge_page(*badges, fill=(0, 200, 220)):
    """Return a 1200 x 1723 gray manga page with solid colour boxes (x, y, w, h).

    The page: Akuyaku Kizoku to shite Hitsuyou na Sore.
    """
    a = np.asarray(fx.manga_page(fx.PILOT)).copy()
    for x, y, w, h in badges:
        a[y : y + h, x : x + w] = fill
    return a


def page_class(rgb, limits=LIMITS):
    """images.page_class with the bulk converter's thresholds."""
    return images.page_class(rgb, limits["colour_block_spread"], limits["tint_p99_spread"], limits)


# detect


@budget(load="fs")
@pytest.mark.parametrize(
    ("make", "want"),
    [
        (fx.jpeg, images.JPEG),
        (fx.png, images.PNG),
        (fx.webp, images.WEBP_LOSSY),
        (lambda p: fx.webp(p, lossless=True), images.WEBP_LOSSLESS),
        (lambda p: fx.encode(p, "AVIF", quality=60), images.AVIF),
        (lambda p: fx.encode(p.convert("P"), "GIF"), images.GIF),
        (lambda p: fx.encode(p, "BMP"), images.BMP),
    ],
    ids=["jpeg", "png", "webp-lossy", "webp-lossless", "avif", "gif", "bmp"],
)
def test_library_formats(make, want):
    assert images.detect(make(fx.webtoon_page((200, 300)))) == want, "each format is told apart by its own bytes"


@budget(load="pure")
@pytest.mark.parametrize(
    "head",
    [b"\xff\x0a\xff\x07", b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a\x00\x00\x00\x14ftypjxl "],
    ids=["codestream", "container"],
)
def test_jxl_signatures(head):
    assert images.detect(head) == images.JXL, "both JPEG XL signatures (codestream, ISO-BMFF container) are JXL"


@budget(load="pure")
@pytest.mark.parametrize(
    "head",
    [
        b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00mif1avifmiaf",
        b"\x00\x00\x00\x1cftypavis\x00\x00\x00\x00msf1miafMA1B",
    ],
    ids=["avif-compatible-brand", "avis-major-brand"],
)
def test_avif_brand_anywhere_in_ftyp(head):
    assert images.detect(head) == images.AVIF, "avif or avis as major or compatible brand is AVIF"


@budget(load="pure")
def test_heif_without_an_avif_brand_is_unknown():
    assert images.detect(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic") == images.UNKNOWN, (
        "a HEIF ftyp box carrying neither avif nor avis (a HEIC photo) is not AVIF"
    )


@budget(load="fs")
def test_animated_webp():
    """6 chapters on the share carry one animated WebP page."""
    frames = [fx.webtoon_page((200, 300), seed=s) for s in (1, 2)]
    data = fx.encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=200, loop=0)
    assert images.detect(data) == images.WEBP_ANIMATED, "an ANIM chunk makes the page animated"


@pytest.fixture
def extended():
    """VP8X WebP pages (29 sample chapters): (lossy, lossless)."""
    page = fx.webtoon_page((200, 300))
    return fx.webp(page, extended=True), fx.webp(page, lossless=True, extended=True)


@budget(load="fs")
def test_extended_webp_fixtures_are_vp8x(extended):
    assert (extended[0][12:16], extended[1][12:16]) == (b"VP8X", b"VP8X"), (
        "precondition: both fixtures start with the VP8X chunk"
    )


@budget(load="fs")
def test_extended_webp_reports_inner_codec(extended):
    assert (images.detect(extended[0]), images.detect(extended[1])) == (images.WEBP_LOSSY, images.WEBP_LOSSLESS), (
        "detect walks past VP8X to the real codec chunk"
    )


@budget(load="fs")
def test_extended_webp_needs_the_whole_file(extended):
    assert images.detect(extended[0][:64]) != images.WEBP_LOSSY, "the lossy kind sits past the first 64 bytes"


@budget(load="pure")
@pytest.mark.parametrize(
    ("data", "want"),
    [
        (riff((b"VP8X", bytes(10)), (b"ICCP", bytes(3)), (b"VP8L", bytes(5))), images.WEBP_LOSSLESS),
        (riff((b"VP8X", bytes(10)), (b"ANIM", bytes(6))), images.WEBP_ANIMATED),
        (riff((b"VP8X", bytes(10)))[:30] + b"VP8L" + bytes(4), images.WEBP_LOSSLESS),
        (riff((b"VP8X", bytes(10))), images.UNKNOWN),
    ],
    ids=["odd-chunk-padded", "anim", "kind-header-at-the-end", "no-kind-chunk"],
)
def test_webp_chunk_walk(data, want):
    assert images.detect(data) == want, (
        "chunks are stepped over by size plus the pad byte; a header ending at the data's end is read; "
        "no codec chunk after VP8X is UNKNOWN"
    )


@budget(load="pure")
@pytest.mark.parametrize("data", [fx.ZERO_BYTE, fx.zero_filled()], ids=["zero-byte", "zero-filled"])
def test_broken_downloads_are_unknown(data):
    assert images.detect(data) == images.UNKNOWN, "pages written with no data or with zeros are UNKNOWN"


# dimensions


@budget(2.0, load="fs")  # measured 1.3 s: a lossless WebP encode of a 1125 x 1600 page
@pytest.mark.parametrize(
    "make",
    [
        fx.webp,
        lambda p: fx.webp(p, lossless=True),
        lambda p: fx.webp(p, extended=True),
        fx.jpeg,
        fx.png,
    ],
    ids=["webp-lossy", "webp-lossless", "webp-vp8x", "jpeg", "png"],
)
def test_dimensions_from_header_only(make):
    assert images.dimensions(make(fx.webtoon_page((1125, 1600)))[:4096]) == (1125, 1600), (
        "4 KB of a page's head gives its size"
    )


@budget(load="fs")
@pytest.mark.parametrize(
    ("make", "n"),
    [
        (fx.webp, 30),
        (lambda p: fx.webp(p, extended=True), 30),
        (lambda p: fx.webp(p, lossless=True), 25),
    ],
    ids=["webp-lossy-30", "webp-vp8x-30", "webp-lossless-25"],
)
def test_dimensions_from_the_shortest_webp_head(make, n):
    assert images.dimensions(make(fx.webtoon_page((301, 449)))[:n]) == (301, 449), (
        "the size fields end at byte 30 (VP8, VP8X) or 25 (VP8L)"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    "head",
    [b"RIFF\x00\x00\x00\x00WEBPVP8 " + bytes(20), fx.zero_filled()],
    ids=["webp-without-start-code", "zero-filled"],
)
def test_dimensions_of_an_unreadable_header_is_none(head):
    assert images.dimensions(head) is None, "a header dimensions() can't read gives None instead of raising"


@budget(load="fs")
def test_file_dimensions_reads_a_path(tmp_path):
    p = tmp_path / "p.webp"
    p.write_bytes(fx.webp(fx.webtoon_page((200, 300))))
    assert images.file_dimensions(p) == (200, 300), "file_dimensions takes a page path"


@budget(load="fs")
def test_file_dimensions_reads_an_open_file(tmp_path):
    p = tmp_path / "p.webp"
    p.write_bytes(fx.webp(fx.webtoon_page((200, 300))))
    with Path(p).open("rb") as f:
        assert images.file_dimensions(f) == (200, 300), "file_dimensions takes an open binary file"


@budget(load="fs")
def test_page_size_from_the_header(tmp_path):
    p = tmp_path / "p.webp"
    p.write_bytes(fx.webp(fx.webtoon_page((200, 300))))
    assert images.page_size(p) == (200, 300), "page_size of a page whose header is readable"


@budget(load="fs")
def test_page_size_decodes_when_the_header_is_past_the_head(tmp_path):
    data = fx.jpeg(fx.webtoon_page((120, 90)))
    p = tmp_path / "p.jpg"
    p.write_bytes(data[:2] + (b"\xff\xfe\xff\xff" + bytes(65533)) * 5 + data[2:])  # 5 COM segments of 64 KB
    assert (images.file_dimensions(p), images.page_size(p)) == (None, (120, 90)), (
        "a JPEG whose size marker sits after HEADER_BYTES is sized by decoding it"
    )


# open_image


@budget(load="fs")
def test_16bit_gray_png_is_scaled_not_clipped():
    a = np.asarray(fx.manga_page((300, 400)).convert("L")).astype(np.uint16) * 257
    im, alpha = images.open_image(fx.encode(Image.fromarray(a), "PNG"))
    assert (im.mode, alpha) == ("L", False), "16-bit gray decodes to 8-bit gray"
    assert np.asarray(im).mean() == pytest.approx(a.mean() / 257, abs=1), "16-bit values are shifted down 8 bits"


@budget(load="fs")
def test_palette_transparency_is_kept():
    p = Image.new("P", (40, 40), 0)
    p.putpalette([0, 0, 0, 255, 255, 255])
    p.info["transparency"] = 0
    p.paste(1, (10, 10, 30, 30))
    im, alpha = images.open_image(fx.encode(p, "PNG"))
    assert (im.mode, alpha) == ("RGBA", True), "a palette page with a tRNS colour in use decodes to RGBA with alpha"


@budget(load="fs")
def test_palette_without_transparency_is_rgb():
    im, alpha = images.open_image(fx.encode(fx.webtoon_page((50, 50)).convert("P"), "GIF"))
    assert (im.mode, alpha) == ("RGB", False), "a palette GIF without tRNS decodes to plain RGB"


@budget(load="fs")
def test_opaque_alpha_is_dropped():
    im, alpha = images.open_image(fx.encode(fx.webtoon_page((50, 50)).convert("RGBA"), "PNG"))
    assert (im.mode, alpha) == ("RGB", False), "a fully opaque alpha channel carries nothing"


@budget(load="fs")
def test_opaque_gray_alpha_is_dropped_to_gray():
    im, alpha = images.open_image(fx.encode(fx.manga_page((50, 50)).convert("LA"), "PNG"))
    assert (im.mode, alpha) == ("L", False), "a fully opaque LA page decodes to L"


@budget(load="fs")
def test_cmyk_without_a_profile_is_rgb():
    data = fx.encode(fx.webtoon_page((60, 80), seed=3).convert("CMYK"), "JPEG", quality=95)
    im, alpha = images.open_image(data)
    assert (im.mode, alpha, im.tobytes()) == ("RGB", False, Image.open(io.BytesIO(data)).convert("RGB").tobytes()), (
        "a CMYK JPEG without a profile decodes to RGB by Pillow's conversion"
    )


@budget(load="fs")
def test_open_image_takes_a_path(tmp_path):
    p = tmp_path / "p.png"
    p.write_bytes(fx.png(fx.webtoon_page((50, 40))))
    assert images.open_image(p)[0].size == (50, 40), "open_image decodes a page file as well as bytes"


@budget(load="fs")
@pytest.mark.parametrize(
    "name",
    ["icc_srgb_iec61966.icc", "icc_srgb_google.icc", "icc_srgb_gimp.icc"],
    ids=["iec61966", "google", "gimp"],
)
def test_srgb_profile_variants_are_srgb(name):
    """Every ICC profile in the 142,780 sample pages is one of these sRGB profiles."""
    assert images.is_srgb((fx.PAGE_FIX / name).read_bytes()) is True, f"{name} is an sRGB profile"


@budget(load="pure")
@pytest.mark.parametrize(
    ("icc", "want"),
    [(None, True), (b"", True), (b"junk sRGB junk", True), (b"junk", False)],
    ids=["none", "empty", "unparsable-naming-srgb", "unparsable"],
)
def test_is_srgb_without_a_readable_profile(icc, want):
    assert images.is_srgb(icc) is want, "no profile is sRGB; an unparsable one is sRGB only when its bytes name sRGB"


@budget(load="fs")
def test_gray_profile_is_not_srgb():
    assert images.is_srgb((fx.PAGE_FIX / "icc_gray_azumi.icc").read_bytes()) is False, (
        "Azumi's GRAY 'Dot Gain 20%' profile is not sRGB"
    )


@budget(load="fs")
def test_profile_of_another_colour_space_is_ignored():
    """Azumi Unknown_Chapter 100 p001 on: RGB JPEGs carrying the GRAY "Dot Gain 20%" profile."""
    data = fx.encode(
        fx.webtoon_page((60, 80), seed=3),
        "JPEG",
        quality=95,
        icc_profile=(fx.PAGE_FIX / "icc_gray_azumi.icc").read_bytes(),
    )
    im, alpha = images.open_image(data)
    assert (im.mode, alpha, im.tobytes()) == ("RGB", False, Image.open(io.BytesIO(data)).convert("RGB").tobytes()), (
        "a profile of another colour space is ignored: the pixels are taken as sRGB"
    )


def wide_icc():
    """Return a non-sRGB RGB profile: an sRGB profile renamed (no such profile in the sample)."""
    return (fx.PAGE_FIX / "icc_srgb_iec61966.icc").read_bytes().replace(b"sRGB IEC61966-2.1", b"Wide IEC61966-2.1")


@budget(load="fs")
def test_rgb_profile_conversion_keeps_alpha():
    src = fx.webtoon_page((40, 40)).convert("RGBA")
    src.putalpha(Image.linear_gradient("L").resize((40, 40)))
    im, alpha = images.open_image(fx.encode(src, "PNG", icc_profile=wide_icc()))
    assert (im.mode, alpha, images.is_srgb(im.info.get("icc_profile"))) == ("RGBA", True, True), (
        "an RGBA page in a non-sRGB RGB profile is converted to sRGB"
    )
    assert im.getchannel("A").tobytes() == src.getchannel("A").tobytes(), "the alpha channel passes through unchanged"


@budget(load="fs")
def test_rgb_profile_conversion_without_alpha():
    im, alpha = images.open_image(fx.encode(fx.webtoon_page((40, 40)), "PNG", icc_profile=wide_icc()))
    assert (im.mode, alpha, images.is_srgb(im.info.get("icc_profile"))) == ("RGB", False, True), (
        "an RGB page in a non-sRGB RGB profile decodes to RGB in sRGB, no alpha added"
    )


@pytest.mark.skipif(not WINDOWS_CMYK.exists(), reason="needs Windows' built-in CMYK profile")
@budget(load="fs")
def test_cmyk_pixels_in_a_cmyk_profile_go_straight_to_srgb():
    data = fx.encode(
        fx.webtoon_page((60, 80), seed=3).convert("CMYK"),
        "JPEG",
        quality=95,
        icc_profile=WINDOWS_CMYK.read_bytes(),
    )
    im, _ = images.open_image(data)
    raw = Image.open(io.BytesIO(data))
    want = present(
        ImageCms.profileToProfile(
            raw, ImageCms.ImageCmsProfile(str(WINDOWS_CMYK)), ImageCms.createProfile("sRGB"), outputMode="RGB"
        )
    )
    assert (im.mode, im.tobytes()) == ("RGB", want.tobytes()), "CMYK pixels go through their CMYK profile to sRGB"
    assert im.tobytes() != raw.convert("RGB").tobytes(), "the CMYK profile is applied, not Pillow's naive conversion"


# real alpha: open_flat (pages modelled on Caught by the Villain's edge-alpha slices, Protected by My
# Dragon Knight's fully transparent page, The Villainess Turns the Hourglass's all-255 alpha)


def rgba(a):
    return fx.webp(Image.fromarray(a, "RGBA"), lossless=True)


def edge_slice(seed=1):
    """Return a WebP RGBA strip slice, alpha 64 in column 0, opaque elsewhere."""
    a = np.dstack([np.asarray(fx.webtoon_page((W, H), seed=seed)), np.full((H, W), 255, np.uint8)])
    a[:, 0, 3] = 64
    return rgba(a)


def transparent_page():
    """Every pixel alpha 0 (stored black under it)."""
    return rgba(np.zeros((H, W, 4), np.uint8))


def opaque_rgba():
    """RGBA PNG whose alpha is 255 everywhere."""
    a = np.dstack([np.asarray(fx.manga_page((W, H), seed=4, noise=3)), np.full((H, W), 255, np.uint8)])
    return fx.png(Image.fromarray(a, "RGBA"))


@budget(load="fs")
@pytest.mark.parametrize(
    "data",
    [
        lambda: fx.jpeg(fx.webtoon_page((W, H))),
        lambda: fx.png(fx.manga_page((W, H)).convert("L")),
        opaque_rgba,
        lambda: fx.png(fx.webtoon_page((W, H)).quantize(32)),
    ],
    ids=["rgb-jpeg", "l-png", "opaque-rgba", "p-png"],
)
def test_a_page_without_real_alpha_comes_back_exactly_as_open_image_returns_it(data):
    d = data()
    a, b = images.open_image(d)[0], images.open_flat(d)[0]
    assert (a.mode, np.asarray(a).tobytes()) == (b.mode, np.asarray(b).tobytes()), "byte-identical: nothing re-measured"


@budget(load="fs")
@pytest.mark.parametrize("data", [edge_slice, transparent_page], ids=["edge-alpha", "fully-transparent"])
def test_real_alpha_is_composited_onto_white(data):
    d = data()
    im, real = images.open_flat(d)
    a = np.asarray(images.open_image(d)[0].convert("RGBA"), np.float32)
    want = np.rint(a[..., :3] * a[..., 3:] / 255 + 255 * (1 - a[..., 3:] / 255))
    assert real, "a page with any alpha under 255 has real alpha"
    assert float(np.abs(np.asarray(im, np.float32) - want).max()) <= 1, "real alpha is composited onto white"


# to_gray, save_png


@budget(load="fs")
def test_to_gray_drops_a_colour_profile():
    """Gunslinger Girl ch38-56: an sRGB-profiled colour JPEG classed gray."""
    data = fx.encode(
        fx.manga_page((40, 40)).convert("RGB"),
        "JPEG",
        icc_profile=(fx.PAGE_FIX / "icc_srgb_google.icc").read_bytes(),
    )
    im, alpha = images.open_image(data)
    g = images.to_gray(im, alpha=alpha)
    assert (g.mode, "icc_profile" in g.info) == ("L", False), "a gray version loses a colour profile"


@budget(load="fs")
def test_to_gray_keeps_a_gray_profile():
    im = fx.manga_page((40, 40)).convert("L")
    im.info["icc_profile"] = (fx.PAGE_FIX / "icc_gray_azumi.icc").read_bytes()
    assert "icc_profile" in images.to_gray(im, alpha=False).info, "a GRAY profile describes gray pixels and stays"


@budget(load="pure")
def test_to_gray_keeps_alpha():
    assert images.to_gray(Image.new("RGBA", (4, 4), (10, 20, 30, 0)), alpha=True).mode == "LA", (
        "a page with alpha is LA"
    )


@budget(load="fs")
def test_save_png_is_lossless(tmp_path):
    im = fx.webtoon_page((30, 20))
    images.save_png(im, tmp_path / "p.png")
    data = (tmp_path / "p.png").read_bytes()
    assert (images.detect(data), Image.open(io.BytesIO(data)).tobytes()) == (images.PNG, im.tobytes()), (
        "save_png writes a PNG holding exactly the page's pixels"
    )


# page classes: gray-mark (Akuyaku Kizoku to shite Hitsuyou na Sore, Reset Scans badge: 1200 x 1723 pages, one
# 260 x 40 badge, 0.13-0.45% of tiles; the 23 real colour pages of that series start at 2.56%)


@budget(load="fs")
def test_one_small_badge_is_gray_mark():
    assert page_class(badge_page(BADGE)) == images.GRAY_MARK, "one compact badge as the only colour is gray-mark"


@budget(load="fs")
def test_the_mark_box_is_the_dilated_badge():
    """The badge covers tiles x 112..144, y 206..211; 3 tiles of dilation give px (872, 1624)-(1184, 1720)."""
    assert images.classify(badge_page(BADGE), 22, 6, LIMITS) == (images.GRAY_MARK, (872, 1624, 1184, 1720)), (
        "the mark box is the dilated badge"
    )


@budget(load="fs")
def test_two_badges_are_colour():
    assert page_class(badge_page(BADGE, (40, 40, 260, 40))) == images.COLOUR, "two separate marks are not one mark"


@budget(load="fs")
def test_large_colour_area_is_colour():
    rgb = badge_page((100, 300, 400, 140))
    assert (np.ptp(rgb, axis=2) > 22).mean() >= 0.026, "precondition: patch as large as a real colour page"
    assert images.classify(rgb, 22, 6, LIMITS) == (images.COLOUR, None), "a large colour area is colour"


@budget(load="fs")
@pytest.mark.parametrize("box", [(100, 1650, 520, 24), (100, 100, 24, 200)], ids=["wide", "tall"])
def test_wide_or_tall_mark_is_colour(box):
    assert page_class(badge_page(box)) == images.COLOUR, "a mark wider than 35% or taller than 8% is colour"


@budget(load="fs")
@pytest.mark.parametrize(
    ("box", "want"),
    [
        ((400, 1648, 368, 40), images.GRAY_MARK),
        ((400, 1648, 376, 40), images.COLOUR),
        ((400, 400, 24, 88), images.GRAY_MARK),
        ((400, 400, 24, 96), images.COLOUR),
        ((896, 1600, 16, 16), images.COLOUR),
        ((896, 1600, 32, 32), images.GRAY_MARK),
    ],
    ids=[
        "width-under-cap",
        "width-over-cap",
        "height-under-cap",
        "height-over-cap",
        "under-min-area",
        "over-min-area",
    ],
)
def test_mark_size_limits(box, want):
    """Tile-aligned marks dilate by 3 tiles (48 px) per axis.

    Width 416 is within 35% of 1200 (420), 424 is not; height 136 is within 8% of 1723 (137.84), 144 is not; a 16 px
    square dilates to 64 x 64 (4096 px2 < 5000) and stays colour, a 32 px one to 80 x 80 (6400 px2) is gray-mark.
    """
    assert page_class(badge_page(box)) == want, "the mark's dilated box is held to the width, height and area caps"


@budget(load="fs")
@pytest.mark.parametrize(
    ("limit", "want"),
    [(0.0062, images.GRAY_MARK), (0.0061, images.COLOUR)],
    ids=["cap-above-mark", "cap-below-mark"],
)
def test_mark_tile_fraction_must_be_under_the_cap(limit, want):
    """The badge colours 198 of 32,250 tiles (0.614%)."""
    assert page_class(badge_page(BADGE), dict(LIMITS, mark_max_frac=limit)) == want, (
        "the mark's tile fraction must be under mark_max_frac"
    )


@budget(load="fs")
def test_gray_page_is_gray():
    assert images.classify(badge_page(), 22, 6, LIMITS) == (images.GRAY, None), "no colour and no cast is gray"


@budget(load="fs")
def test_tinted_page_is_tinted():
    assert page_class(np.asarray(fx.with_tint(fx.manga_page(fx.PILOT)))) == images.TINTED, (
        "a faint overall cast with no coloured patch is tinted"
    )


@budget(load="fs")
def test_lone_faint_speck_keeps_colour():
    """For My Derelict Favorite Official_Chapter 74 p084: a tiny near-threshold speck, not a badge."""
    rgb = badge_page((900, 1650, 16, 16), fill=(100, 140, 100))
    assert page_class(rgb) == images.COLOUR, "a speck under mark_min_area keeps the page colour"


@budget(load="fs")
def test_mark_limits_come_from_the_caller():
    rgb = badge_page(BADGE)
    narrow = dict(LIMITS, mark_max_w=0.10)
    assert (page_class(rgb), page_class(rgb, narrow)) == (images.GRAY_MARK, images.COLOUR), (
        "the caller's mark caps decide gray-mark vs colour"
    )


@budget(load="pure")
@pytest.mark.parametrize(("threshold", "want"), [(99, images.COLOUR), (100, images.GRAY)], ids=["below", "at"])
def test_colour_strength_is_the_strongest_tile_mean(threshold, want):
    """An 8 x 8 tile half filled with spread 200 has mean 100; the patch covers 1 of 4 tiles, never a gray mark."""
    rgb = np.zeros((16, 16, 3), np.uint8)
    rgb[0:4, 8:16] = (200, 0, 0)
    assert images.classify(rgb, threshold, 1000, LIMITS)[0] == want, "the strongest tile mean is the strength"


@budget(load="pure")
@pytest.mark.parametrize("shape", [(4, 4, 3), (4, 64, 3)], ids=["both-sides", "height-only"])
def test_page_smaller_than_a_tile_uses_its_max_spread(shape):
    rgb = np.zeros(shape, np.uint8)
    rgb[0, 0] = (0, 90, 30)
    assert [images.classify(rgb, t, 1000, LIMITS) for t in (89, 90)] == [
        (images.COLOUR, None),
        (images.GRAY, None),
    ], "the strongest pixel (spread 90) is the strength"


@budget(load="fs")
@pytest.mark.parametrize(
    ("box", "want"),
    [((940, 1683, 260, 40), (912, 1656, 1200, 1720)), ((0, 0, 260, 40), (0, 0, 288, 64))],
    ids=["bottom-right", "top-left"],
)
def test_mark_in_a_page_corner(box, want):
    assert images.classify(badge_page(box), 22, 6, LIMITS) == (images.GRAY_MARK, want), (
        "a badge's dilation stops at the page edge"
    )


@budget(load="fs")
def test_mark_pieces_joined_by_dilation_are_one_mark():
    """Two 24 x 64 bars 136 px apart joined by a 184 x 16 bar form one U-shaped mark."""
    rgb = badge_page((400, 800, 24, 64), (560, 800, 24, 64), (400, 848, 184, 16))
    assert images.classify(rgb, 22, 6, LIMITS) == (images.GRAY_MARK, (376, 776, 608, 888)), (
        "the box is the dilated whole"
    )


@budget(load="fs")
def test_tile_spread_at_the_threshold_is_not_colour():
    rgb = badge_page(BADGE)
    rgb[96:112, 96:112] = (100, 122, 100)
    box = (872, 1624, 1184, 1720)
    assert images.classify(rgb, 22, 6, LIMITS) == (images.GRAY_MARK, box), (
        "a patch of spread exactly 22 beside the badge is no second mark (coloured tiles have spread > 22)"
    )


@budget(load="fs")
def test_page_whose_strongest_tile_is_at_the_threshold_is_gray():
    rgb = badge_page()
    rgb[96:112, 96:112] = (100, 122, 100)
    assert images.classify(rgb, 22, 6, LIMITS) == (images.GRAY, None), "spread exactly 22 is not colour"


@budget(load="fs")
@pytest.mark.parametrize(
    ("limits", "want"),
    [
        (dict(LIMITS, mark_max_w=0.26), images.GRAY_MARK),
        (dict(LIMITS, mark_max_h=96 / 1723), images.GRAY_MARK),
        (dict(LIMITS, mark_min_area=29952), images.GRAY_MARK),
        (dict(LIMITS, mark_max_frac=198 / 32250), images.COLOUR),
    ],
    ids=["width-at-cap", "height-at-cap", "area-at-min", "tiles-at-frac"],
)
def test_mark_limits_at_equality(limits, want):
    """The badge's dilated box is 312 x 96 (29,952 px2) over 198 of 32,250 tiles."""
    assert page_class(badge_page(BADGE), limits) == want, (
        "width, height and area equal to their limits pass; a tile fraction equal to its cap does not"
    )


@budget(load="fs")
@pytest.mark.parametrize(("spread", "want"), [(6, images.GRAY), (7, images.TINTED)], ids=["at-threshold", "above"])
def test_tint_threshold(spread, want):
    rgb = np.full((1723, 1200, 3), 128, np.uint8)
    rgb[..., 1] = 128 + spread
    assert page_class(rgb) == want, "tinted when the p99 spread is above tint_p99 (6)"


@budget(load="fs")
def test_tint_is_the_99th_percentile_not_the_max():
    rgb = np.full((1723, 1200, 3), 128, np.uint8)
    rgb[:16, :, 1] = 138
    assert page_class(rgb) == images.GRAY, "a cast on under 1% of the page leaves its p99 spread at 0"
