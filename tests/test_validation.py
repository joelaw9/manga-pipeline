"""validation.check (SPEC "Validation"): each defect of a download and its outcome, from fixture CBZs."""

import zipfile

import pytest
from PIL import Image

from manga_pipeline import images, measures, validation

from tests.pages import ZERO_BYTE, cbz, crc_bad_comicinfo, jpeg, jxl_of_jpeg, manga_page, png
from tests.timing import budget

KEY = (None, "", "5")


def pages(n=3, seed=0, size=(300, 420)):
    return {f"{i:03}.jpg": jpeg(manga_page(size=size, seed=seed + i)) for i in range(1, n + 1)}


def measured(tmp_path, key, members, name="m.cbz") -> validation.Published:
    """Return a published file of chapter key, with the measurements of a CBZ built from members."""
    path = cbz(tmp_path / name, members)
    m = measures.summary(images.measure_pages(path))
    return {"key": key, "page_phashes": m["page_phashes"], "strip_length": m["strip_length"]}


@budget(load="fs")
def test_a_sound_download_passes_with_its_measurements(tmp_path):
    verdict = validation.check(cbz(tmp_path / "c.cbz", pages()), KEY, [])
    assert verdict.kind == "ok"
    assert verdict.notes == []
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
        lambda t: cbz(t / "jxl.cbz", {"001.jpg": jpeg(manga_page(seed=1)), "002.jxl": b"\xff\x0a jxl codestream"}),
    ],
    ids=["truncated", "crc", "encrypted", "no pages", "zero-byte page", "undecodable page", "undecodable jxl page"],
)
def test_a_bad_download_is_bad(tmp_path, make):
    verdict = validation.check(make(tmp_path), KEY, [])
    assert verdict.kind == "bad", f"a bad download is re-downloaded, not handed to a person: {verdict}"


@budget(load="fs")
@pytest.mark.parametrize(
    "page",
    [b"\x00\x01\x02 not an image at all"],
    ids=["unknown"],
)
def test_a_page_of_a_format_the_pipeline_cannot_read_needs_a_human(tmp_path, page):
    verdict = validation.check(cbz(tmp_path / "f.cbz", {"001.jpg": jpeg(manga_page(seed=1)), "002.jpg": page}), KEY, [])
    assert verdict.kind == "human", "another source would not change an unknown format: a person decides"


@budget(load="fs")
def test_a_download_of_jxl_pages_is_read_like_any_other(tmp_path):
    pages = {f"00{i}.jxl": jxl_of_jpeg(jpeg(manga_page(seed=i))) for i in (1, 2)}
    verdict = validation.check(cbz(tmp_path / "f.cbz", pages), KEY, [])
    assert verdict.kind == "ok", f"JPEG XL pages are decoded and checked, not held: {verdict}"


@budget(load="fs")
def test_a_page_with_an_exif_rotation_needs_a_human(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6
    rotated = jpeg(manga_page(seed=1), exif=exif.tobytes())
    verdict = validation.check(cbz(tmp_path / "r.cbz", {"001.jpg": rotated}), KEY, [])
    assert verdict.kind == "human"
    assert "orientation 6" in verdict.reason


@budget(load="fs")
def test_duplicate_pages_are_kept_and_noted(tmp_path):
    page = jpeg(manga_page(seed=1))
    members = {"001.jpg": page, "002.jpg": jpeg(manga_page(seed=2)), "003.jpg": page}
    verdict = validation.check(cbz(tmp_path / "d.cbz", members), KEY, [])
    assert verdict.kind == "ok", "a duplicate page is kept"
    assert len(verdict.pages) == 3, "a duplicate page is kept"
    assert verdict.notes == ["duplicate pages: 001.jpg, 003.jpg"]


def _series(tmp_path, n=3):
    """Write n published files of other chapters, each of 3 distinct pages."""
    return [measured(tmp_path, (None, "", str(10 + i)), pages(seed=100 * (i + 1)), f"p{i}.cbz") for i in range(n)]


@budget(load="fs")
def test_a_download_repeating_another_published_chapter_is_mislabelled(tmp_path):
    series = _series(tmp_path)
    copy = cbz(tmp_path / "copy.cbz", pages(seed=200))  # the pages of the 2nd published file
    verdict = validation.check(copy, KEY, series)
    assert verdict.kind == "human"
    assert verdict.reason.startswith("mislabelled")


@budget(load="fs")
def test_the_mislabel_check_waits_for_3_measured_published_files_and_skips_the_same_chapter(tmp_path):
    series = _series(tmp_path)
    copy = cbz(tmp_path / "copy.cbz", pages(seed=200))
    assert validation.check(copy, KEY, series[:2]).kind == "ok", "2 measured files are too few to judge"
    unmeasured: list[validation.Published] = [
        *series[:2],
        {"key": (None, "", "99"), "page_phashes": None, "strip_length": None},
    ]
    assert validation.check(copy, KEY, unmeasured).kind == "ok", "a file with no stored measurements does not count"
    assert validation.check(copy, (None, "", "11"), series).kind == "ok", (
        "a copy of the same chapter (same season, side label and number) is not mislabelled"
    )


@budget(load="fs")
def test_a_short_chapter_is_noted_and_passes(tmp_path):
    def strip(seed, height):
        return {f"{i:03}.png": png(manga_page((100, height), seed=seed + i)) for i in range(1, 4)}  # not blank

    series = [measured(tmp_path, (None, "", str(i)), strip(10 * i, 500), f"s{i}.cbz") for i in range(3)]
    short = cbz(tmp_path / "short.cbz", strip(90, 150))
    verdict = validation.check(short, KEY, series)
    assert verdict.kind == "ok", "a short chapter is published"
    assert len(verdict.notes) == 1
    assert verdict.notes[0].startswith("short: strip length 4500 is 30%")


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


# ---------------------------------------------------------------- R16 notes


def blank_page(shade):
    """Return a page of 1 shade at manga_page's default size (900 x 1300): blank, no width outlier, no duplicate."""
    return png(Image.new("RGB", (900, 1300), (shade, shade, shade)))


@budget(load="fs")
def test_blank_pages_are_noted_by_name_and_a_cover_named_one_is_left_out(tmp_path):
    pages = {"000_cover.png": blank_page(255), "001.jpg": jpeg(manga_page(seed=1)), "002.png": blank_page(254)}
    pages["003.png"] = blank_page(253)
    verdict = validation.check(cbz(tmp_path / "b.cbz", pages), KEY, [])
    assert verdict.kind == "ok"
    assert verdict.notes == ["blank pages: 002.png, 003.png"], "a blank page is noted; a cover is no page of its own"
    plain = validation.check(cbz(tmp_path / "p.cbz", {"001.jpg": jpeg(manga_page(seed=1))}), KEY, [])
    assert plain.notes == [], "no blank page, no note"


@budget(load="fs")
@pytest.mark.parametrize(
    ("width", "noted"), [(720, True), (1080, True), (810, False)], ids=["20 % narrower", "20 % wider", "10 % narrower"]
)
def test_a_page_far_from_the_median_width_is_noted(tmp_path, width, noted):
    pages = {f"00{i}.jpg": jpeg(manga_page(seed=i)) for i in (1, 2, 3)}
    pages["004.jpg"] = jpeg(manga_page((width, 1300), seed=4))
    pages["000_cover.jpg"] = jpeg(manga_page((600, 1300), seed=5))  # a cover-named page is left out
    verdict = validation.check(cbz(tmp_path / "w.cbz", pages), KEY, [])
    want = ["width outliers: 1 of 4 pages more than 15% from the median width 900"] if noted else []
    assert verdict.notes == want, "more than 15 % from the median width is noted, the cover not counted"


@budget(load="fs")
def test_a_download_of_only_a_cover_named_page_has_no_page_note(tmp_path):
    verdict = validation.check(cbz(tmp_path / "c.cbz", {"cover.jpg": jpeg(manga_page(seed=1))}), KEY, [])
    assert (verdict.kind, verdict.notes) == ("ok", []), "no page left to measure: no note, no error"


@budget(load="fs")
@pytest.mark.parametrize("capped", [True, False])
def test_a_short_capped_strip_says_its_length_cannot_be_trusted(tmp_path, monkeypatch, capped):
    def strip(seed, height):
        return {f"{i:03}.png": png(manga_page((100, height), seed=seed + i)) for i in range(1, 4)}

    series = [measured(tmp_path, (None, "", str(i)), strip(10 * i, 500), f"s{i}.cbz") for i in range(3)]
    if capped:
        monkeypatch.setattr(validation, "WEBP_MAX_SIDE", 150)  # the short chapter's pages reach the limit
    (note,) = validation.check(cbz(tmp_path / "short.cbz", strip(90, 150)), KEY, series).notes
    assert note.startswith("short: strip length 4500 is 30%")
    assert ("so the length cannot be trusted" in note) == capped, "only a page at the limit makes the length unsure"


@budget(load="fs")
@pytest.mark.parametrize(
    ("xml", "why"),
    [
        (b"<ComicInfo><Number>5</Num", "ComicInfo.xml does not parse"),
        (b"", "ComicInfo.xml does not parse"),
        (b'<?xml version="1.0" encoding="bogus"?><ComicInfo/>', "ComicInfo.xml does not parse"),
    ],
    ids=["broken", "empty", "bogus encoding"],
)
def test_a_comicinfo_that_does_not_parse_is_noted_and_kept(tmp_path, xml, why):
    path = cbz(tmp_path / "f.cbz", {"001.jpg": jpeg(manga_page(seed=1))}, comicinfo=xml)
    verdict = validation.check(path, KEY, [])
    assert verdict.kind == "ok", "its fields are ignored; the comicinfo stage writes a new one"
    (note,) = verdict.notes
    assert note.startswith(why)
    assert note.endswith("its fields are ignored")


@budget(load="fs")
def test_a_comicinfo_member_that_fails_its_crc_is_noted_and_kept(tmp_path):
    path = crc_bad_comicinfo(tmp_path / "f.cbz", {"001.jpg": jpeg(manga_page(seed=1))})
    verdict = validation.check(path, KEY, [])
    assert verdict.kind == "ok"
    (note,) = verdict.notes
    assert note.startswith("ComicInfo.xml cannot be read")


@budget(load="fs")
def test_a_comicinfo_with_schema_errors_only_is_not_noted(tmp_path):
    xml = b"<ComicInfo><Unknown>x</Unknown><Number>5</Number></ComicInfo>"
    verdict = validation.check(cbz(tmp_path / "f.cbz", {"001.jpg": jpeg(manga_page(seed=1))}, comicinfo=xml), KEY, [])
    assert (verdict.kind, verdict.notes) == ("ok", []), "it parses: schema errors are the comicinfo stage's"
