"""CBZ archives (contracts section 1; SPEC "Bad downloads", "Publishing").

Which members are pages and in what order, the content digest, and the deterministic store-mode writer. Copied from the
bulk converter's optimizer/formats.py
(archive half), pipeline.repack and record.pages_digest.

A file on the share is written as <name>.part, renamed over its target, then tested (size and zip test), so a reader
never sees half a file.
"""

import hashlib
import re
import struct
import zipfile
import zlib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from manga_pipeline import errors

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif", ".jxl"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
JUNK_DIRS = {"__macosx"}
COMICINFO = "ComicInfo.xml"
COVER_STEM = "000_cover"  # the volume cover page the bulk converter inserts
FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # every member's date: the same members give byte-identical archives
# What reading an archive that is broken, truncated, CRC-bad or encrypted raises (SPEC "Validation"): such a download
# is a bad download, not a failure of the stage reading it.
UNREADABLE = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, ValueError, struct.error)
# Kavita's Parser.CoverImageRegex: a page named like this is the archive's cover
COVER_NAME = re.compile(
    r"(?<!back[\s_-])(?<!\(back )(?<!back)(?:^|[^a-zA-Z0-9])(!?cover|folder)(?![a-zA-Z0-9]|s\b)", re.IGNORECASE
)


def is_cover_name(name: str) -> bool:
    """Return whether Kavita would treat this page as the archive's cover (cover.jpg, 000_cover.png, folder.png)."""
    return bool(COVER_NAME.search(PurePosixPath(name.replace("\\", "/")).name))


def natural_key(path: str) -> list[int | str]:
    """Return the natural sort key over a full archive path (matches Kavita's page ordering).

    The extension is compared last, so "1.jpg" < "1.5.jpg" < "2.jpg".
    """
    folder, _, name = path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    base = f"{folder}/{stem}" if folder else stem
    return [int(t) if t.isdecimal() else t.lower() for t in re.split(r"(\d+)", base)] + [ext.lower()]


def is_junk(name: str) -> bool:
    """Return whether an archive member is junk: a junk file name (Thumbs.db), under __MACOSX/, or a ._* file."""
    p = PurePosixPath(name.replace("\\", "/"))
    return (
        p.name.lower() in JUNK_NAMES
        or any(part.lower() in JUNK_DIRS for part in p.parts[:-1])
        or p.name.startswith("._")
    )


def _posix(name: str) -> str:
    return name.replace("\\", "/")


def archive_pages(names: Iterable[str]) -> list[str]:
    """Return an archive's page members (names as stored) in natural reading order.

    Image extensions only; directories, junk (__MACOSX/, ._*, Thumbs.db) and ComicInfo.xml dropped. Every reader of an
    archive's pages uses this, so page indexes agree everywhere.
    """
    pages = [
        n
        for n in names
        if not n.endswith("/") and not is_junk(n) and PurePosixPath(_posix(n)).suffix.lower() in IMAGE_EXTS
    ]
    return sorted(pages, key=lambda n: natural_key(_posix(n)))


def comicinfo_member(names: Iterable[str]) -> str | None:
    """Return the archive's ComicInfo.xml member (exact basename, any case, not under junk like __MACOSX/).

    Prefers one at the root; None if absent.
    """
    hits = [n for n in names if PurePosixPath(_posix(n)).name.lower() == "comicinfo.xml" and not is_junk(n)]
    return next((n for n in hits if "/" not in _posix(n)), hits[0] if hits else None)


def sha1(data: bytes) -> str:
    """Return the hex sha1 digest of data."""
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def pages_digest(cbz: Path) -> str:
    r"""Return the content digest (SPEC "Bad downloads"): sha256 of the ordered page sha1 list, "\n"-joined.

    The bulk converter's record.pages_digest exactly, so its records' `pages_digest` compare with it.
    """
    with zipfile.ZipFile(cbz) as z:
        sha1s = [sha1(z.read(n)) for n in archive_pages(z.namelist())]
    return hashlib.sha256("\n".join(sha1s).encode()).hexdigest()


def digest_or_none(cbz: Path) -> str | None:
    """pages_digest, or None for an archive that cannot be read or is gone."""
    try:
        return pages_digest(cbz)
    except (*UNREADABLE, FileNotFoundError) as e:
        if errors.dropped_share(e):
            raise
        return None


def read_comicinfo(cbz: Path) -> bytes | None:
    """Return the bytes of the archive's ComicInfo.xml, or None when it has none."""
    with zipfile.ZipFile(cbz) as z:
        member = comicinfo_member(z.namelist())
        return None if member is None else z.read(member)


def write(dest: Path, members: Iterable[tuple[str, bytes]]) -> int:
    """Write a store-mode CBZ of members ([(name, bytes)], stored in that order) at dest; return dest's size.

    Fixed date, attributes and system, so the same members give byte-identical archives. Written as dest.part, renamed
    over dest, then tested; a write that raises before the rename removes dest.part.
    """
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as z:
            for name, data in members:
                info = zipfile.ZipInfo(name, FIXED_TIME)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 0
                info.external_attr = 0
                z.writestr(info, data)
        size = part.stat().st_size
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    test(dest, size)
    return size


def pages(cbz: Path) -> list[tuple[str, bytes]]:
    """[(name, bytes)] of an archive's pages in reading order (archive_pages), bytes untouched."""
    with zipfile.ZipFile(cbz) as z:
        return [(n, z.read(n)) for n in archive_pages(z.namelist())]


def replace_member(cbz: Path, name: str, data: bytes) -> int:
    """Rewrite cbz with member `name` (compared without case) stored last with data; return the new size.

    Replaces any member of that name; every other member keeps its bytes and order. Through write, so a crash leaves the
    old archive whole.
    """
    with zipfile.ZipFile(cbz) as z:
        members = [(n, z.read(n)) for n in z.namelist() if n.lower() != name.lower()]
    return write(cbz, [*members, (name, data)])


def test(cbz: Path, size: int) -> None:
    """Raise when cbz is not `size` bytes or fails the zip test (a CRC or a member it cannot read)."""
    actual = Path(cbz).stat().st_size
    if actual != size:
        msg = f"{cbz} is {actual} bytes, expected {size}"
        raise OSError(msg)
    with zipfile.ZipFile(cbz) as z:
        bad = z.testzip()
    if bad is not None:
        msg = f"{cbz}: member {bad} fails its CRC"
        raise zipfile.BadZipFile(msg)
