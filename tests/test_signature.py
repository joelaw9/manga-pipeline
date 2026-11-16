"""Page signatures and the one page measurement (images: signature, matches, sig_to_str / sig_from_str, phash,
chroma, measure_pages; contracts section 1). The signature texts and pHashes pinned here were computed by the bulk
converter's own optimizer.junk.signature and optimizer.integrity.phash on the real pages in tests/fixtures_pages/: the
stored junk decisions compare only while these stay equal. Ported from the bulk converter's tests/test_restitch.py
(text round trip), tests/test_junk_single.py (near rule) and tests/test_image.py (chroma)."""

import io
import zipfile

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import images

from tests import pages as fx
from tests.timing import budget

# signature texts computed by the bulk converter's optimizer.junk.signature (sig_to_str) on Image.open(file)
FILE_SIGNATURES = {
    "endpage_akuyaku_ch17_p39.jpg": "d05a692d2f1e1e36:ddb1668c9b4e0c71|1.5466666666666666",
    "endpage_akuyaku_ch18_p37.jpg": "d05a792d2f06163f:c499b3c65f394cc4|1.5466666666666666",
    "endpage_akuyaku_ch22_p34.jpg": "d05a692d2d1f1e36:c4b171ce5e6199e4|1.5466666666666666",
    "marks_heroine_ch52_p031.png": "b3ec2ea01e301ff4:b3b3e24c3fb3104c|1.7142857142857142",
    "marks_scan_co.png": "bf1c941fc02b6879|0.32954545454545453",
    "marks_scan_org.png": "aa7fd56895859780|0.25066666666666665",
    "marks_scans_busy.png": "ff70c0216a1e7857|0.40756302521008403",
    "marks_scans_plain.png": "ead684d06ad52ad5|0.4092827004219409",
    "marks_scans_plain_b.png": "bf78d4002a2c7c7e|0.4092827004219409",
    "marks_scans_scaled.png": "fe30813177397a31|0.41697416974169743",
    "marks_screentone.png": "ff80d5d59594682a|0.3283582089552239",
    "strip_bring_the_love_ch87_p001_head.png": "b5047b434c5aeb39|0.8333333333333334",
}
# ... on the 48x72 shrunk real edge pages of frames_pages.npz (Image.fromarray(small[i]))
NPZ_SIGNATURES = {
    "akuyaku_ch17_p39": "d05a692d2f1e1e36:ddb1668c9b4e0c71|1.5",
    "akuyaku_ch18_p37": "d05a712d2f16163f:c499b3c65f390ec4|1.5",
    "akuyaku_ch22_p34": "d05a69252d1f1e37:c4b1718e7e6199e4|1.5",
    "akuyaku_ch23_p39": "d05a792d2d171e36:c590706f9b7865d2|1.5",
    "akuyaku_ch24_p34": "c96ea74a52b525ac:b3fb440c0b653b33|1.5",
    "akuyaku_ch24_p1": "d4266b470fd5c496:b44c33d12ef10cb7|1.5",
    "blacksmith_ch13_p62": "c96ee71b52a435a4:b3fb440c0b653f32|1.5",
    "blacksmith_ch16_p63": "d4266b470fd5c496:b44c33d12ef10cb7|1.5",
    "ennead_ch150_p1": "8c9c7cb068c3e3f2:8f2fee989c1ce026|1.5",
    "ennead_ch150_p17": "8c9c7cb068c3e3f2:8f2fee989c1ce026|1.5",
    "neverland_ch1_p2": "adf39691692c94b2:ec6cb43e91892b95|1.5",
    "yumekui_ch7_p17": "e0faafcae0c885ca:8d716687b49a619d|1.5",
}
# 64-bit pHashes computed by the bulk converter's optimizer.integrity.phash on its _decode thumbnail of each file
FILE_PHASHES = {
    "endpage_akuyaku_ch17_p39.jpg": "d0725adb64d93525",
    "endpage_akuyaku_ch18_p37.jpg": "c4d35a6af16c2d25",
    "endpage_akuyaku_ch22_p34.jpg": "d4705ada71ce310f",
    "marks_heroine_ch52_p031.png": "b3b34cb3a24cb34c",
    "marks_scan_co.png": "bf1c941fc02b6879",
    "marks_scan_org.png": "aa7fd568958587c0",
    "marks_scans_busy.png": "ff70c0216a1e7857",
    "marks_scans_plain.png": "aad684d06bd52ad5",
    "marks_scans_plain_b.png": "bf78d4002a2c7c7e",
    "marks_scans_scaled.png": "fe30813177397a31",
    "marks_screentone.png": "ff80d5d59594682a",
    "strip_bring_the_love_ch87_p001_head.png": "b5047b434c5aeb39",
}


def stretched(im, drift):
    """im with its height grown by `drift` (a re-upload of the same credit at another aspect)."""
    return im.resize((im.width, round(im.height * (1 + drift))))


def flipped(sig, bits):
    """sig with the low `bits` bits of every tile flipped: a tile Hamming distance of exactly `bits`."""
    tiles, aspect = sig
    return tuple(t ^ ((1 << bits) - 1) for t in tiles), aspect


def near(a, b):
    return images.matches(a, b, images.NEAR_THRESHOLD, images.NEAR_ASPECT_TOLERANCE)


# ---------------------------------------------------------------- pinned real signatures


@budget(load="fs")
@pytest.mark.parametrize("name", sorted(FILE_SIGNATURES))
def test_a_real_page_signature_is_the_bulk_converters(name):
    """The signature of a real page is the text the bulk converter's junk.signature gave it."""
    assert images.sig_to_str(images.signature(Image.open(fx.PAGE_FIX / name))) == FILE_SIGNATURES[name], (
        "signature must stay equal to the bulk converter's, or stored decisions stop matching"
    )


@budget(load="fs")
def test_the_shrunk_real_edge_page_signatures_are_the_bulk_converters():
    """12 real edge pages shrunk to 48x72: every signature is the text the bulk converter's junk.signature gave it."""
    d = np.load(fx.PAGE_FIX / "frames_pages.npz")
    got = {
        str(n): images.sig_to_str(images.signature(Image.fromarray(im)))
        for n, im in zip(d["names"], d["small"], strict=True)
    }
    assert got == NPZ_SIGNATURES, "signature must stay equal to the bulk converter's, or stored decisions stop matching"


@budget(load="fs")
@pytest.mark.parametrize("name", sorted(FILE_PHASHES))
def test_a_real_page_phash_is_the_bulk_converters(name):
    """phash of a real page's 64x64 luma thumbnail is the bulk converter's integrity.phash of it."""
    with Image.open(fx.PAGE_FIX / name) as im:
        thumb = np.asarray(im.convert("L").resize(images.THUMB))
    assert f"{images.phash(thumb):016x}" == FILE_PHASHES[name], "phash must stay equal to the bulk converter's"


# ---------------------------------------------------------------- signature rules


@budget(load="pure")
def test_sig_text_round_trip():
    """A signature survives its text form exactly (64-bit tiles, aspect)."""
    sig = ((0x1234, 0xFFFFFFFFFFFFFFFF), 2.0123456789)
    assert images.sig_from_str(images.sig_to_str(sig)) == sig, "text form round-trips exactly"


@budget(load="pure")
@pytest.mark.parametrize(
    ("size", "tiles"),
    [((300, 100), 1), ((300, 449), 1), ((300, 451), 2), ((300, 1200), 4)],
    ids=["wide", "1.5-", "1.5+", "4"],
)
def test_one_tile_per_roughly_square_slice(size, tiles):
    """Tile count is max(1, round(h / w)); the aspect is h / w."""
    sig = images.signature(fx.credit(size))
    assert (len(sig[0]), sig[1]) == (tiles, size[1] / size[0]), "max(1, round(h / w)) tiles, aspect h / w"


@budget(load="pure")
@pytest.mark.parametrize(("bits", "want"), [(6, True), (7, False)], ids=["d6", "d7"])
def test_the_live_rule_matches_up_to_6_bits_per_tile(bits, want):
    """matches' default rule: every tile within PHASH_THRESHOLD 6 bits."""
    sig = images.signature(fx.credit((300, 600)))
    assert images.matches(sig, flipped(sig, bits)) is want, "PHASH_THRESHOLD 6"


@budget(load="pure")
@pytest.mark.parametrize(("aspect", "want"), [(1.0309, True), (1.031, False)], ids=["within-3%", "over-3%"])
def test_the_live_rule_allows_3_percent_aspect_of_the_larger(aspect, want):
    """matches' default rule: aspect difference within ASPECT_TOLERANCE 3 % of the larger aspect (1 / 0.97 =
    1.03093 is the limit above 1.0)."""
    assert images.matches(((1,), 1.0), ((1,), aspect)) is want, "ASPECT_TOLERANCE 3 % of the larger aspect"


@budget(load="pure")
def test_signatures_of_other_tile_counts_never_match():
    """A 1-tile and a 2-tile signature never match, whatever the threshold."""
    assert images.matches(((1,), 1.0), ((1, 1), 1.0), 64, 1.0) is False, "tile counts must be equal"


@budget(load="pure")
@pytest.mark.parametrize("drift", [0.04, 0.06, 0.09])
def test_a_credit_variant_at_another_aspect_is_near_but_not_live(drift):
    """Bring The Love: the Daeva credit re-uploaded 4-9 % taller matches no row at the live 3 % aspect gate, but is
    near (d <= NEAR_THRESHOLD 8, aspect <= NEAR_ASPECT_TOLERANCE 10 %)."""
    credit = fx.credit((300, 600))
    sig, variant = images.signature(credit), images.signature(stretched(credit, drift))
    assert not images.matches(sig, variant), "the live rule misses the variant"
    assert near(sig, variant), "the near rule finds it"


@budget(load="pure")
def test_an_aspect_drift_over_the_near_tolerance_is_not_near():
    """A credit 12 % taller (aspect difference 10.7 %) is past NEAR_ASPECT_TOLERANCE 0.10."""
    credit = fx.credit((300, 600))
    assert not near(images.signature(credit), images.signature(stretched(credit, 0.12))), "10.7 % is not near"


@budget(load="pure")
@pytest.mark.parametrize(("bits", "want"), [(8, True), (9, False), (12, False)], ids=["d8-near", "d9", "d12-story"])
def test_near_stops_at_the_threshold(bits, want):
    """The measurement's d12 blocks were story panels, d0-8 all junk: NEAR_THRESHOLD is 8."""
    sig = images.signature(fx.credit((300, 600)))
    assert near(sig, flipped(sig, bits)) is want, "NEAR_THRESHOLD 8"


# ---------------------------------------------------------------- chroma


def gray_jpeg():
    """A single-component (mode L) JPEG ("Akuyaku Kizoku to shite Hitsuyou na Sore" Vol.1 Ch.4.cbz 001.jpg)."""
    buf = io.BytesIO()
    Image.new("L", (64, 64), 128).save(buf, "JPEG")
    return buf.getvalue()


def jpeg_440():
    """A 4:4:0 JPEG (first component sampled 1x2): no avifenc --yuv name here."""
    jpeg = bytearray(fx.jpeg(fx.webtoon_page((64, 64)), subsampling=0))
    sof = jpeg.find(bytes([0xFF, 0xC0]))  # SOF0 marker
    jpeg[sof + 11] = 0x12
    return bytes(jpeg)


@budget(load="pure")
@pytest.mark.parametrize(
    ("data", "fmt", "want"),
    [
        (fx.jpeg(fx.webtoon_page((64, 64)), subsampling=0), images.JPEG, "444"),
        (fx.jpeg(fx.webtoon_page((64, 64)), subsampling=1), images.JPEG, "422"),
        (fx.jpeg(fx.webtoon_page((64, 64)), subsampling=2), images.JPEG, "420"),
        (gray_jpeg(), images.JPEG, "400"),  # one component: no chroma (used to be 524 false flags)
        (jpeg_440(), images.JPEG, "444"),  # never below the source's chroma
        (fx.png(fx.webtoon_page((64, 64))), images.PNG, "444"),
        (fx.webp(fx.webtoon_page((64, 64)), lossless=True), images.WEBP_LOSSLESS, "444"),
        (fx.encode(fx.webtoon_page((64, 64)), "GIF"), images.GIF, "444"),
        (fx.encode(fx.webtoon_page((64, 64)), "BMP"), images.BMP, "444"),
        (fx.webp(fx.webtoon_page((64, 64))), images.WEBP_LOSSY, "420"),
        (b"", images.JXL, "420"),  # anything else: header not read
    ],
    ids=[
        "jpeg-444",
        "jpeg-422",
        "jpeg-420",
        "jpeg-gray",
        "jpeg-440",
        "png",
        "webp-lossless",
        "gif",
        "bmp",
        "webp-lossy",
        "other",
    ],
)
def test_chroma_follows_the_source(data, fmt, want):
    """A source keeps its chroma (pilot volume covers scored 82.9-85.1 at 444)."""
    assert images.chroma(data, fmt) == want, "chroma follows the source"


# ---------------------------------------------------------------- measure_pages


def chapter(path):
    """A CBZ stored out of reading order with a ComicInfo.xml and a junk member: 10.png (gray art), 2.jpg (a real
    page), 1.png (white), __MACOSX/._1.png."""
    pages = {
        "10.png": fx.png(fx.manga_page((200, 300))),
        "2.jpg": (fx.PAGE_FIX / "endpage_akuyaku_ch17_p39.jpg").read_bytes(),
        "1.png": fx.png(Image.new("RGB", (100, 150), "white")),
        "__MACOSX/._1.png": b"junk",
    }
    return fx.cbz(path, pages, comicinfo=b"<ComicInfo/>")


@budget(load="fs")
def test_measure_pages_measures_every_page_in_reading_order(tmp_path):
    """One dict per page in archive_pages order with every field; ComicInfo and junk members are no pages."""
    got = images.measure_pages(chapter(tmp_path / "c.cbz"))
    assert [(p["index"], p["name"]) for p in got] == [(0, "1.png"), (1, "2.jpg"), (2, "10.png")], "reading order"
    assert all(tuple(p) == images.PAGE_FIELDS for p in got), "every page has exactly PAGE_FIELDS, in order"
    white, real, art = got
    with zipfile.ZipFile(tmp_path / "c.cbz") as z:
        data = z.read("2.jpg")
    assert (real["bytes"], real["sha1"], real["width"], real["height"]) == (
        len(data),
        images.archive.sha1(data),
        300,
        464,
    ), "size, sha1 and dimensions of the page's bytes"
    assert (real["format"], real["chroma"], real["blank"]) == (images.JPEG, images.chroma(data, images.JPEG), False), (
        "format and chroma from the header"
    )
    assert real["junk_signature"] == FILE_SIGNATURES["endpage_akuyaku_ch17_p39.jpg"], "the bulk converter's signature"
    assert real["phash"] == FILE_PHASHES["endpage_akuyaku_ch17_p39.jpg"], "the bulk converter's pHash"
    assert (white["blank"], white["phash"], white["junk_signature"]) == (True, None, None), (
        "a white page is blank (no pHash) and uniform (no signature)"
    )
    assert (white["colour_class"], art["colour_class"], art["format"], art["chroma"]) == (
        images.GRAY,
        images.GRAY,
        images.PNG,
        "444",
    ), "gray pages classed gray"


@budget(load="fs")
def test_a_colour_page_is_classed_colour(tmp_path):
    """colour_class uses colour_block 22 / tint_p99 6 and the integrity mark limits: smooth colour art is colour."""
    cbz = fx.cbz(tmp_path / "c.cbz", {"001.png": fx.png(fx.webtoon_page((200, 600)))})
    assert images.measure_pages(cbz)[0]["colour_class"] == images.COLOUR, "colour art is colour"


@budget(load="fs")
def test_known_fields_are_kept_and_a_fully_known_page_is_not_decoded(tmp_path, monkeypatch):
    """A page whose fields are all known is not decoded; a page missing one decoded field is decoded and gets only
    the missing ones."""
    cbz = chapter(tmp_path / "c.cbz")
    first = images.measure_pages(cbz)
    decoded = []
    real = images._measure_decoded
    monkeypatch.setattr(images, "_measure_decoded", lambda data: decoded.append(data) or real(data))
    partial = {k: v for k, v in first[2].items() if k != "phash"}
    known = {0: first[0], 1: dict(first[1], colour_class="kept as known"), 2: partial}
    got = images.measure_pages(cbz, known)
    assert len(decoded) == 1, "only the page missing a decoded field is decoded"
    assert got[1]["colour_class"] == "kept as known", "a known field is never re-measured"
    assert got[2] == first[2], "the missing field is filled"
    assert got[0] == first[0], "a fully known page comes back as known"


@budget(load="fs")
def test_a_known_sha1_that_is_not_the_pages_is_refused(tmp_path):
    """Known fields of other bytes are stale: refused, never mixed in."""
    cbz = chapter(tmp_path / "c.cbz")
    with pytest.raises(ValueError, match=r"page 0 \(1.png\) known sha1 0+ is not"):
        images.measure_pages(cbz, {0: {"sha1": "0" * 40}})


@budget(load="fs")
def test_an_undecodable_page_is_refused_by_name(tmp_path):
    """A page that is no image raises ValueError naming it."""
    cbz = fx.cbz(tmp_path / "c.cbz", {"001.jpg": fx.zero_filled()})
    with pytest.raises(ValueError, match=r"page 0 \(001.jpg\) cannot be measured"):
        images.measure_pages(cbz)
