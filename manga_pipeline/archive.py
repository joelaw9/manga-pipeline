"""CBZ archives (contracts section 1; SPEC "Bad downloads", "Publishing"): which members are pages and in what order,
the content digest, and the deterministic store-mode writer. Copied from the bulk converter's optimizer/formats.py
(archive half), pipeline.repack and record.pages_digest.

A file on the share is written as <name>.part, renamed over its target, then tested (size and zip test), so a reader
never sees half a file.
"""

import hashlib
import re
import struct
import zipfile
import zlib
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


def is_cover_name(name):
    """True when Kavita would treat this page as the archive's cover (cover.jpg, 000_cover.png, folder.png)."""
    return bool(COVER_NAME.search(PurePosixPath(name.replace("\\", "/")).name))


def natural_key(path):
    """Natural sort key over a full archive path (matches Kavita's page ordering).
    The extension is compared last, so "1.jpg" < "1.5.jpg" < "2.jpg"."""
    folder, _, name = path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    base = f"{folder}/{stem}" if folder else stem
    return [int(t) if t.isdecimal() else t.lower() for t in re.split(r"(\d+)", base)] + [ext.lower()]


def is_junk(name):
    p = PurePosixPath(name.replace("\\", "/"))
    return (
        p.name.lower() in JUNK_NAMES
        or any(part.lower() in JUNK_DIRS for part in p.parts[:-1])
        or p.name.startswith("._")
    )


def _posix(name):
    return name.replace("\\", "/")


def archive_pages(names):
    """An archive's page members (names as stored) in natural reading order: image extensions only; directories,
    junk (__MACOSX/, ._*, Thumbs.db) and ComicInfo.xml dropped. Every reader of an archive's pages uses this, so
    page indexes agree everywhere."""
    pages = [
        n
        for n in names
        if not n.endswith("/") and not is_junk(n) and PurePosixPath(_posix(n)).suffix.lower() in IMAGE_EXTS
    ]
    return sorted(pages, key=lambda n: natural_key(_posix(n)))


def comicinfo_member(names):
    """The archive's ComicInfo.xml member (exact basename, any case, not under junk like __MACOSX/), preferring one
    at the root; None if absent."""
    hits = [n for n in names if PurePosixPath(_posix(n)).name.lower() == "comicinfo.xml" and not is_junk(n)]
    return next((n for n in hits if "/" not in _posix(n)), hits[0] if hits else None)


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def pages_digest(cbz):
    """The content digest (SPEC "Bad downloads"): sha256 of the ordered page sha1 list, "\\n"-joined. The bulk
    converter's record.pages_digest exactly, so its records' `pages_digest` compare with it."""
    with zipfile.ZipFile(cbz) as z:
        sha1s = [sha1(z.read(n)) for n in archive_pages(z.namelist())]
    return hashlib.sha256("\n".join(sha1s).encode()).hexdigest()


def digest_or_none(cbz):
    """pages_digest, or None for an archive that cannot be read or is gone."""
    try:
        return pages_digest(cbz)
    except (*UNREADABLE, FileNotFoundError) as e:
        if errors.dropped_share(e):
            raise
        return None


def read_comicinfo(cbz):
    """The bytes of the archive's ComicInfo.xml, or None when it has none."""
    with zipfile.ZipFile(cbz) as z:
        member = comicinfo_member(z.namelist())
        return None if member is None else z.read(member)


def write(dest, members):
    """A store-mode CBZ of members ([(name, bytes)], stored in that order) at dest: fixed date, attributes and
    system, so the same members give byte-identical archives. Written as dest.part, renamed over dest, then tested;
    a write that raises before the rename removes dest.part. Returns dest's size."""
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


def pages(cbz):
    """[(name, bytes)] of an archive's pages in reading order (archive_pages), bytes untouched."""
    with zipfile.ZipFile(cbz) as z:
        return [(n, z.read(n)) for n in archive_pages(z.namelist())]


def replace_member(cbz, name, data):
    """Rewrite cbz with member `name` (compared without case) stored last with data, replacing any member of that
    name; every other member keeps its bytes and order. Through write, so a crash leaves the old archive whole.
    Returns the new size."""
    with zipfile.ZipFile(cbz) as z:
        members = [(n, z.read(n)) for n in z.namelist() if n.lower() != name.lower()]
    return write(cbz, [*members, (name, data)])


def test(cbz, size):
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
