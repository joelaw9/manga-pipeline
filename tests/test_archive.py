"""CBZ archives (archive; contracts section 1, SPEC "Bad downloads", "Publishing").

Kavita's cover rule, natural page order, page and ComicInfo members, sha1, the pages digest, and the store-mode writer
with its member replace and test. Ported from the bulk converter's tests/test_formats.py and tests/test_record.py
(pages digest).
"""

import errno
import hashlib
import zipfile

import pytest

from manga_pipeline import archive

from tests import pages as fx
from tests.timing import budget

# cover names (Kavita's Parser.CoverImageRegex)


@budget(load="pure")
@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("cover.jpg", True),
        ("000_cover.avif", True),
        ("folder.png", True),
        ("Chapter 1/cover.webp", True),
        ("Chapter 1\\cover.webp", True),
        ("01-cover.jpg", True),
        ("!cover.jpg", True),
        ("001.jpg", False),
        ("covers.jpg", False),
        ("back_cover.jpg", False),
        ("backcover.jpg", False),
        ("(back cover).jpg", False),
        ("discover.png", False),
        ("coverage.jpg", False),
        ("cover/001.jpg", False),
    ],
    ids=str,
)
def test_kavita_cover_rule(name, want):
    assert archive.is_cover_name(name) is want, (
        "a page named cover / folder (whole word, any prefix but back) is the cover; only the file name counts"
    )


@budget(load="pure")
def test_volume_cover_stem_matches_kavitas_rule():
    assert archive.is_cover_name(archive.COVER_STEM + ".avif"), "the inserted volume cover page is Kavita's cover"


# natural order


@budget(load="pure")
def test_natural_order_matches_reader():
    assert sorted(["10.jpg", "2.jpg", "1.jpg", "000_cover.avif"], key=archive.natural_key) == [
        "000_cover.avif",
        "1.jpg",
        "2.jpg",
        "10.jpg",
    ], "unpadded names must not sort 1, 10, 2"


@budget(load="pure")
def test_extension_is_compared_last():
    assert sorted(["2.jpg", "1.5.JPG", "1.jpg", "1.png"], key=archive.natural_key) == [
        "1.jpg",
        "1.png",
        "1.5.JPG",
        "2.jpg",
    ], '"1.jpg" < "1.5.jpg" < "2.jpg": the extension is not part of the number, compared without case'


@budget(load="pure")
def test_decimal_chapter_files_sort_by_number():
    """Nise Seijo Kuso of the Year: split parts and break notices between the chapters."""
    names = [
        "Flaemes TL_Vol. 2, Ch. 10.1.cbz",
        "Flaemes TL_Vol. 2, Ch. 9.5_ Break Notice Illustration.cbz",
        "Flaemes TL_Vol. 2, Ch. 9.2.cbz",
        "Flaemes TL_Vol. 2, Ch. 9.1.cbz",
    ]
    assert sorted(names, key=archive.natural_key) == [
        "Flaemes TL_Vol. 2, Ch. 9.1.cbz",
        "Flaemes TL_Vol. 2, Ch. 9.2.cbz",
        "Flaemes TL_Vol. 2, Ch. 9.5_ Break Notice Illustration.cbz",
        "Flaemes TL_Vol. 2, Ch. 10.1.cbz",
    ], "decimal chapter files sort by number"


@budget(load="pure")
def test_folder_sorts_before_name():
    assert sorted(["b/1.jpg", "a/10.jpg", "a/9.jpg"], key=archive.natural_key) == ["a/9.jpg", "a/10.jpg", "b/1.jpg"], (
        "pages sort by folder first, then by number"
    )


# archive members


@budget(load="pure")
def test_archive_pages_are_images_in_reading_order_without_junk():
    names = [
        "10.jpg",
        "2.png",
        "ComicInfo.xml",
        "__MACOSX/._2.png",
        "__MACOSX/3.jpg",
        "._1.jpg",
        "Thumbs.db",
        "sub/",
        "notes.txt",
        "sub/1.5.webp",
        "4.JPEG",
    ]
    assert archive.archive_pages(names) == ["2.png", "4.JPEG", "10.jpg", "sub/1.5.webp"], (
        "image extensions only, natural order; directories, junk and ComicInfo.xml dropped"
    )


@budget(load="pure")
def test_archive_pages_keep_backslash_names_as_stored():
    assert archive.archive_pages(["b\\10.jpg", "b\\2.jpg"]) == ["b\\2.jpg", "b\\10.jpg"], (
        "backslash members are ordered as paths but returned unchanged"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("names", "want"),
    [
        (["sub/ComicInfo.xml", "comicinfo.XML"], "comicinfo.XML"),
        (["sub/ComicInfo.xml"], "sub/ComicInfo.xml"),
        (["xComicInfo.xml", "__MACOSX/ComicInfo.xml"], None),
    ],
    ids=["root-first-any-case", "subfolder", "none"],
)
def test_comicinfo_member(names, want):
    assert archive.comicinfo_member(names) == want, (
        "the exact basename, any case, outside junk folders, at the root when there is one there"
    )


# sha1, pages digest, readers


@budget(load="pure")
def test_sha1_of_bytes():
    assert archive.sha1(b"abc") == "a9993e364706816aba3e25717850c26c9cd0d89d", 'the FIPS 180 test vector "abc"'


@pytest.fixture
def page_set():
    return {f"{i:03d}.jpg": fx.jpeg(fx.blob(i)) for i in (1, 2, 3)}


@budget(1.0, load="fs")  # the first page encode of a run pays Pillow's codec import (0.55 s measured)
def test_a_rezipped_copy_matches_by_pages_not_bytes(tmp_path, page_set):
    """A re-download Suwayomi re-zipped (deflated -> stored, ComicInfo rewritten)."""
    a = fx.cbz(tmp_path / "a.cbz", page_set, fx.suwayomi_comicinfo())
    b = fx.cbz(
        tmp_path / "b.cbz", page_set, fx.suwayomi_comicinfo(title="Chapter 1 (fixed)"), method=zipfile.ZIP_STORED
    )
    assert a.read_bytes() != b.read_bytes(), "precondition: the archives differ byte-wise"
    assert archive.pages_digest(a) == archive.pages_digest(b), "same images in the same order give the same digest"


@budget(load="fs")
def test_a_changed_page_changes_the_pages_digest(tmp_path, page_set):
    a = fx.cbz(tmp_path / "a.cbz", page_set, fx.suwayomi_comicinfo())
    c = fx.cbz(tmp_path / "c.cbz", dict(page_set, **{"003.jpg": fx.jpeg(fx.blob(9))}), fx.suwayomi_comicinfo())
    assert archive.pages_digest(a) != archive.pages_digest(c), "page 3 replaced by another image changes the digest"


@budget(load="fs")
def test_pages_digest_is_sha256_of_the_ordered_page_sha1s(tmp_path):
    cbz = fx.cbz(tmp_path / "a.cbz", {"10.jpg": b"ten", "2.jpg": b"two"}, b"<ComicInfo/>")
    want = hashlib.sha256(f"{archive.sha1(b'two')}\n{archive.sha1(b'ten')}".encode()).hexdigest()
    assert archive.pages_digest(cbz) == want, "sha256 of the page sha1s in reading order, newline-joined"


@budget(load="fs")
def test_pages_are_page_members_in_reading_order(tmp_path):
    cbz = fx.cbz(tmp_path / "a.cbz", {"10.jpg": b"ten", "2.jpg": b"two"}, b"<ComicInfo/>")
    assert archive.pages(cbz) == [("2.jpg", b"two"), ("10.jpg", b"ten")], "pages: (name, bytes) in reading order"


@budget(load="fs")
@pytest.mark.parametrize("comicinfo", [b"<ComicInfo/>", None], ids=["present", "absent"])
def test_read_comicinfo(tmp_path, comicinfo):
    cbz = fx.cbz(tmp_path / "a.cbz", {"1.jpg": b"one"}, comicinfo)
    assert archive.read_comicinfo(cbz) == comicinfo, "the ComicInfo.xml bytes, None when the archive has none"


# write, replace_member, test

MEMBERS = [("002.jpg", b"page two"), ("001.jpg", b"page one"), ("ComicInfo.xml", b"<ComicInfo/>")]


@budget(load="fs")
def test_write_is_byte_identical_for_the_same_members(tmp_path):
    a, b = tmp_path / "a.cbz", tmp_path / "b.cbz"
    archive.write(a, MEMBERS)
    archive.write(b, MEMBERS)
    assert a.read_bytes() == b.read_bytes(), "the same members give byte-identical archives"


@budget(load="fs")
def test_write_stores_members_in_the_given_order(tmp_path):
    dest = tmp_path / "a.cbz"
    size = archive.write(dest, MEMBERS)
    with zipfile.ZipFile(dest) as z:
        infos = z.infolist()
        assert [(i.filename, z.read(i)) for i in infos] == MEMBERS, "members are stored in the given order"
    assert {i.compress_type for i in infos} == {zipfile.ZIP_STORED}, "every member is stored, not deflated"
    assert size == dest.stat().st_size, "write returns the archive's size"


@budget(load="fs")
def test_write_leaves_no_part_file_and_replaces_dest(tmp_path):
    dest = tmp_path / "a.cbz"
    dest.write_bytes(b"old")
    archive.write(dest, MEMBERS)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.cbz"], "the .part file is renamed over dest"
    assert archive.pages(dest) == [("001.jpg", b"page one"), ("002.jpg", b"page two")], "dest holds the new members"


@budget(load="fs")
def test_a_write_that_fails_leaves_no_part_file_and_dest_untouched(tmp_path):
    def members():
        yield MEMBERS[0]
        raise OSError(errno.ENOSPC, "No space left on device")

    dest = tmp_path / "a.cbz"
    dest.write_bytes(b"old")
    with pytest.raises(OSError, match="No space left on device") as e:
        archive.write(dest, members())
    assert e.value.errno == errno.ENOSPC, "the write's own error reaches the caller"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.cbz"], "a failed write removes its .part file"
    assert dest.read_bytes() == b"old", "dest is left as it was"


@budget(load="fs")
def test_replace_member_replaces_without_case_and_stores_it_last(tmp_path):
    cbz = tmp_path / "a.cbz"
    archive.write(cbz, [("001.jpg", b"one"), ("comicinfo.XML", b"<old/>"), ("002.jpg", b"two")])
    size = archive.replace_member(cbz, "ComicInfo.xml", b"<new/>")
    with zipfile.ZipFile(cbz) as z:
        got = [(n, z.read(n)) for n in z.namelist()]
    assert got == [("001.jpg", b"one"), ("002.jpg", b"two"), ("ComicInfo.xml", b"<new/>")], (
        "the member is replaced whatever its case and stored last; the others keep their bytes and order"
    )
    assert size == cbz.stat().st_size, "replace_member returns the new size"


@budget(load="fs")
def test_replace_member_adds_a_missing_member(tmp_path):
    cbz = tmp_path / "a.cbz"
    archive.write(cbz, [("001.jpg", b"one")])
    archive.replace_member(cbz, "ComicInfo.xml", b"<new/>")
    assert archive.read_comicinfo(cbz) == b"<new/>", "a member the archive lacks is added last"


@budget(load="fs")
def test_test_refuses_a_size_mismatch(tmp_path):
    cbz = tmp_path / "a.cbz"
    size = archive.write(cbz, MEMBERS)
    with pytest.raises(OSError, match=f"is {size} bytes, expected {size + 1}"):
        archive.test(cbz, size + 1)


@budget(load="fs")
def test_test_refuses_a_crc_failure(tmp_path):
    cbz = tmp_path / "a.cbz"
    size = archive.write(cbz, [("001.jpg", b"page one")])
    data = cbz.read_bytes()
    cbz.write_bytes(data.replace(b"page one", b"page 0ne"))  # same size, member bytes no longer match their CRC
    with pytest.raises(zipfile.BadZipFile, match=r"001\.jpg"):
        archive.test(cbz, size)


@budget(load="fs")
def test_test_passes_a_sound_archive(tmp_path):
    cbz = tmp_path / "a.cbz"
    size = archive.write(cbz, MEMBERS)
    archive.test(cbz, size)
