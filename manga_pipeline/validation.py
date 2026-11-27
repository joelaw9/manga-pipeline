"""Validation of a download (SPEC "Validation"): what a CBZ's pages say about it, against the series' published files.

Reads the file; writes nothing.
"""

import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict

from manga_pipeline import archive, comicinfo, images, measures, names

MISLABEL_HAMMING = 6  # pHash bits: the old integrity.flag_mislabelled threshold
MISLABEL_SHARE = 0.90  # of a chapter's non-blank pages
SHORT_RATIO = 0.40  # of the series' median strip length (old integrity.short_chapter_ratio)
MIN_PUBLISHED = 3  # published files with measurements before the series-wide checks run
UNDECODABLE_FORMATS = {images.UNKNOWN}  # no decoder here: an unknown format to a person, not a bad copy
ROTATED = range(2, 9)  # EXIF orientations that turn or mirror a page; 1 is upright, 0 and others read as upright
WIDTH_OUTLIER = 0.15  # a page width this far from the chapter's median is noted (old integrity.WIDTH_OUTLIER)
WEBP_MAX_SIDE = 16383  # a page this tall was stitched to WebP's limit and scaled down: its length is unreliable
BLANK_NOTE = "blank pages:"  # the 2 routine notes; status counts them apart for done chapters
WIDTH_NOTE = "width outliers:"


type ChapterKey = tuple[str | None, str | None, str | None]
"""A chapter's (season, side label, number): chapter_key's."""


class Published(TypedDict):
    """What the checks read of a published file of the series: its chapter key and its measurements."""

    key: ChapterKey
    page_phashes: list[str] | None
    strip_length: int | None


@dataclass(frozen=True)
class Verdict:
    """kind: "ok" (pages and notes hold), "bad" (a bad download: re-download), "human" (needs a person)."""

    kind: Literal["ok", "bad", "human"]
    reason: str = ""
    pages: list[images.MeasuredPage] = field(default_factory=list[images.MeasuredPage])
    notes: list[str] = field(default_factory=list[str])


def check[P: Published](
    cbz: Path, key: ChapterKey, published: Sequence[P], same: Callable[[P], bool] = lambda _p: True
) -> Verdict:
    """Return the verdict on a download.

    key: its (season, side label, number); published: the series' published files as [{key, page_phashes (list or
    None), strip_length (int or None), ...}]; same(p): whether a published file of the download's key is the same
    chapter (chapter_names.same_chapter), asked only once the pages have passed.
    """
    try:
        members = archive.pages(cbz)
    except archive.UNREADABLE as e:
        return Verdict("bad", f"the archive cannot be read: {type(e).__name__}: {e}")
    # In this order (SPEC "Validation"): each page's bytes and format, page by page; then every page decoded; then
    # every page's orientation. The first defect found decides.
    defect = _page_bytes(members)
    if defect:
        return defect
    try:
        pages = images.measure_pages(cbz)
    except ValueError as e:
        return Verdict("bad", str(e))
    held = _rotated(members) or _mislabelled(pages, key, published, same)
    if held:
        return Verdict("human", held)
    _, comicinfo_problem = comicinfo.download_xml(cbz)
    ignored = [f"{comicinfo_problem}: its fields are ignored"] if comicinfo_problem else []
    notes = ignored + _duplicates(pages) + _blank(pages) + _widths(pages) + _short(pages, published)
    return Verdict("ok", pages=pages, notes=notes)


def _page_bytes(members: list[tuple[str, bytes]]) -> Verdict | None:
    """Return the verdict on the first page that is empty (bad) or in a format nothing here decodes (human), else None.

    Bad for an archive with no pages.
    """
    if not members:
        return Verdict("bad", "the archive holds no pages")
    for name, data in members:
        if not data:
            return Verdict("bad", f"page {name} is empty (0 bytes)")
        fmt = images.detect(data)
        if fmt in UNDECODABLE_FORMATS:
            return Verdict("human", f"page {name} is in a format the pipeline cannot read ({fmt})")
    return None


def _rotated(members: list[tuple[str, bytes]]) -> str | None:
    """Why the first page whose EXIF orientation turns or mirrors it is not upright, or None."""
    for name, data in members:
        orientation = images.exif_orientation(data)
        if orientation in ROTATED:
            return f"page {name} has EXIF orientation {orientation} (not upright)"
    return None


def _hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _mislabelled[P: Published](
    pages: list[images.MeasuredPage], key: ChapterKey, published: Sequence[P], same: Callable[[P], bool]
) -> str | None:
    """Why the pages look like another chapter's, or None."""
    measured = [(p, phashes) for p in published if (phashes := p["page_phashes"]) is not None]
    if len(measured) < MIN_PUBLISHED:
        return None
    # ponytail: compares every pair of pages, O(pages x series pages); a band index (old _HammingIndex) if it gets slow
    others = [h for p, phashes in measured if not (p["key"] == key and same(p)) for h in phashes]
    ours = measures.summary(pages)["page_phashes"]
    if not ours or not others:
        return None
    matched = sum(any(_hamming(h, o) <= MISLABEL_HAMMING for o in others) for h in ours)
    if matched < MISLABEL_SHARE * len(ours):
        return None
    return f"mislabelled: {matched} of {len(ours)} non-blank pages repeat pages of other published chapters"


def _duplicates(pages: list[images.MeasuredPage]) -> list[str]:
    counts = Counter(p["sha1"] for p in pages)
    repeated = [p["name"] for p in pages if counts[p["sha1"]] > 1]
    return [f"duplicate pages: {', '.join(repeated)}"] if repeated else []


def _content(pages: list[images.MeasuredPage]) -> list[images.MeasuredPage]:
    """Return the pages that are not cover-named (a cover is no page of the chapter's own)."""
    return [p for p in pages if not archive.is_cover_name(p["name"])]


def _blank(pages: list[images.MeasuredPage]) -> list[str]:
    blank = [p["name"] for p in _content(pages) if p["blank"]]
    return [f"{BLANK_NOTE} {', '.join(blank)}"] if blank else []


def _widths(pages: list[images.MeasuredPage]) -> list[str]:
    widths = [p["width"] for p in _content(pages)]
    if not widths:
        return []
    median = statistics.median(widths)
    odd = sum(abs(w - median) / median > WIDTH_OUTLIER for w in widths)
    if not odd:
        return []
    return [f"{WIDTH_NOTE} {odd} of {len(widths)} pages more than {WIDTH_OUTLIER:.0%} from the median width {median:g}"]


def _short(pages: list[images.MeasuredPage], published: Sequence[Published]) -> list[str]:
    lengths = [length for p in published if (length := p["strip_length"]) is not None]
    if len(lengths) < MIN_PUBLISHED:
        return []
    ours, median = measures.strip_length(pages), statistics.median(lengths)
    if ours >= SHORT_RATIO * median:
        return []
    capped = any(p["height"] >= WEBP_MAX_SIDE for p in pages)
    why = f"; a page is at the {WEBP_MAX_SIDE:,} px height limit, so the length cannot be trusted" if capped else ""
    return [f"short: strip length {ours} is {ours / median:.0%} of the series' median {median:g}{why}"]


def chapter_key(identity: str) -> ChapterKey:
    """Return (season, side label, number) of an identity (names.identity's JSON).

    It is what tells chapters apart across sources and scanlators.
    """
    _, season, side, number, _ = names.read_identity(identity)
    return (season, side, number)
