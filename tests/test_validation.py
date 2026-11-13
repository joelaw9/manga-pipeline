"""validation.check (SPEC "Validation"): each defect of a download and its outcome, from fixture CBZs."""

import zipfile

import pytest
from PIL import Image

from manga_pipeline import images, measures, validation

from tests.pages import ZERO_BYTE, cbz, jpeg, manga_page, png
from tests.timing import budget

KEY = (None, "", "5")


def pages(n=3, seed=0, size=(300, 420)):
    return {f"{i:03}.jpg": jpeg(manga_page(size=size, seed=seed + i)) for i in range(1, n + 1)}


def measured(tmp_path, members, name="m.cbz"):
    """Published-file measurements of a CBZ built from members."""
    path = cbz(tmp_path / name, members)
    m = measures.summary(images.measure_pages(path))
    return {"page_phashes": m["page_phashes"], "strip_length": m["strip_length"]}


@budget(load="fs")
def test_a_sound_download_passes_with_its_measurements(tmp_path):
    verdict = validation.check(cbz(tmp_path / "c.cbz", pages()), KEY, [])
    assert verdict.kind == "ok" and verdict.notes == []
    assert [p["name"] for p in verdict.pages] == ["001.jpg", "002.jpg", "003.jpg"], "every page measured, in order"
    assert all(p["width"] == 300 and p["format"] == images.JPEG for p in verdict.pages)


def _truncated(tmp_path):
    data = cbz(tmp_path / "t.cbz", pages()).read_bytes()
    path = tmp_path / "truncated.cbz"
    path.write_bytes(data[: len(data) // 2])
    return path


def _crc_bad(tmp_path):
    path = cbz(tmp_path / "crc.cbz", {"001.jpg": jpeg(manga_page(seed=1))}, method=zipfile.ZIP_STORED)
    data = bytearray(path.read_bytes())
    data[200] ^= 0xFF  # inside the stored page: its bytes no longer match the CRC
    path.write_bytes(bytes(data))
    return path


def _encrypted(tmp_path):
    path = cbz(tmp_path / "enc.cbz", {"001.jpg": jpeg(manga_page(seed=1))}, method=zipfile.ZIP_STORED)
    data = bytearray(path.read_bytes())
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):  # set the "encrypted" flag in the local and central headers
        at = data.find(sig)
        flag = at + (6 if sig == b"PK\x03\x04" else 8)
        data[flag] |= 0x01
    path.write_bytes(bytes(data))
    return path


@budget(load="fs")
@pytest.mark.parametrize(
    "make",
    [
        _truncated,
        _crc_bad,
        _encrypted,
        lambda t: cbz(t / "empty.cbz", {}),
        lambda t: cbz(t / "zero.cbz", {"001.jpg": jpeg(manga_page(seed=1)), "002.jpg": ZERO_BYTE}),
        lambda t: cbz(t / "undecodable.cbz", {"001.jpg": jpeg(manga_page(seed=1))[:400]}),
    ],
    ids=["truncated", "crc", "encrypted", "no pages", "zero-byte page", "undecodable page"],
)
def test_a_bad_download_is_bad(tmp_path, make):
    verdict = validation.check(make(tmp_path), KEY, [])
    assert verdict.kind == "bad", f"a bad download is re-downloaded, not handed to a person: {verdict}"


@budget(load="fs")
@pytest.mark.parametrize(
    "page",
    [b"\x00\x01\x02 not an image at all", b"\xff\x0a jxl codestream"],
    ids=["unknown", "jxl"],
)
def test_a_page_of_a_format_the_pipeline_cannot_read_needs_a_human(tmp_path, page):
    verdict = validation.check(cbz(tmp_path / "f.cbz", {"001.jpg": jpeg(manga_page(seed=1)), "002.jpg": page}), KEY, [])
    assert verdict.kind == "human", "another source would not change an unknown format: a person decides"


@budget(load="fs")
def test_a_page_with_an_exif_rotation_needs_a_human(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6
    rotated = jpeg(manga_page(seed=1), exif=exif.tobytes())
    verdict = validation.check(cbz(tmp_path / "r.cbz", {"001.jpg": rotated}), KEY, [])
    assert verdict.kind == "human" and "orientation 6" in verdict.reason


@budget(load="fs")
def test_duplicate_pages_are_kept_and_noted(tmp_path):
    page = jpeg(manga_page(seed=1))
    members = {"001.jpg": page, "002.jpg": jpeg(manga_page(seed=2)), "003.jpg": page}
    verdict = validation.check(cbz(tmp_path / "d.cbz", members), KEY, [])
    assert verdict.kind == "ok" and len(verdict.pages) == 3, "a duplicate page is kept"
    assert verdict.notes == ["duplicate pages: 001.jpg, 003.jpg"]


def _series(tmp_path, n=3):
    """n published files of other chapters, each of 3 distinct pages."""
    return [
        {"key": (None, "", str(10 + i)), **measured(tmp_path, pages(seed=100 * (i + 1)), f"p{i}.cbz")} for i in range(n)
    ]


@budget(load="fs")
def test_a_download_repeating_another_published_chapter_is_mislabelled(tmp_path):
    series = _series(tmp_path)
    copy = cbz(tmp_path / "copy.cbz", pages(seed=200))  # the pages of the 2nd published file
    verdict = validation.check(copy, KEY, series)
    assert verdict.kind == "human" and verdict.reason.startswith("mislabelled")


@budget(load="fs")
def test_the_mislabel_check_waits_for_3_measured_published_files_and_skips_the_same_chapter(tmp_path):
    series = _series(tmp_path)
    copy = cbz(tmp_path / "copy.cbz", pages(seed=200))
    assert validation.check(copy, KEY, series[:2]).kind == "ok", "2 measured files are too few to judge"
    unmeasured = [*series[:2], {"key": (None, "", "99"), "page_phashes": None, "strip_length": None}]
    assert validation.check(copy, KEY, unmeasured).kind == "ok", "a file with no stored measurements does not count"
    assert validation.check(copy, (None, "", "11"), series).kind == "ok", (
        "a copy of the same chapter (same season, side label and number) is not mislabelled"
    )


@budget(load="fs")
def test_a_short_chapter_is_noted_and_passes(tmp_path):
    def strip(seed, height):
        return {f"{i:03}.png": png(Image.new("L", (100, height), (seed + i) % 256)) for i in range(1, 4)}

    series = [{"key": (None, "", str(i)), **measured(tmp_path, strip(10 * i, 500), f"s{i}.cbz")} for i in range(3)]
    short = cbz(tmp_path / "short.cbz", strip(90, 150))
    verdict = validation.check(short, KEY, series)
    assert verdict.kind == "ok", "a short chapter is published"
    assert len(verdict.notes) == 1 and verdict.notes[0].startswith("short: strip length 4500 is 30%")


@budget(load="pure")
def test_chapter_key_is_season_side_label_and_number():
    assert validation.chapter_key('["grp", "2", "side", "5", "title"]') == ("2", "side", "5")


@budget(load="fs")
def test_the_first_page_with_a_defect_decides(tmp_path):
    members = {"001.jpg": b"\x00 unknown format", "002.jpg": ZERO_BYTE}
    assert validation.check(cbz(tmp_path / "a.cbz", members), KEY, []).kind == "human", "page 1 decides: unknown"
    members = {"001.jpg": ZERO_BYTE, "002.jpg": b"\x00 unknown format"}
    assert validation.check(cbz(tmp_path / "b.cbz", members), KEY, []).kind == "bad", "page 1 decides: empty"


@budget(load="fs")
def test_an_exif_orientation_of_0_reads_as_upright(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 0
    page = jpeg(manga_page(seed=1), exif=exif.tobytes())
    assert validation.check(cbz(tmp_path / "o.cbz", {"001.jpg": page}), KEY, []).kind == "ok"
