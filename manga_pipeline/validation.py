"""Validation of a download (SPEC "Validation"): what a CBZ's pages say about it, against the series' published files.
Reads the file; writes nothing.
"""

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field

from manga_pipeline import archive, images, measures

MISLABEL_HAMMING = 6  # pHash bits: the old integrity.flag_mislabelled threshold
MISLABEL_SHARE = 0.90  # of a chapter's non-blank pages
SHORT_RATIO = 0.40  # of the series' median strip length (old integrity.short_chapter_ratio)
MIN_PUBLISHED = 3  # published files with measurements before the series-wide checks run
UNDECODABLE_FORMATS = {images.UNKNOWN, images.JXL}  # no decoder here: an unknown format to a person, not a bad copy
ROTATED = range(2, 9)  # EXIF orientations that turn or mirror a page; 1 is upright, 0 and others read as upright


@dataclass(frozen=True)
class Verdict:
    """kind: "ok" (pages and notes hold), "bad" (a bad download: re-download), "human" (needs a person)."""

    kind: str
    reason: str = ""
    pages: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def check(cbz, key, published, same=lambda _p: True):
    """The verdict on a download. key: its (season, side label, number); published: the series' published files as
    [{key, page_phashes (list or None), strip_length (int or None), ...}]; same(p): whether a published file of the
    download's key is the same chapter (chapter_names.same_chapter), asked only once the pages have passed."""
    try:
        members = archive.pages(cbz)
    except archive.UNREADABLE as e:
        return Verdict("bad", f"the archive cannot be read: {type(e).__name__}: {e}")
    if not members:
        return Verdict("bad", "the archive holds no pages")
    # In this order (SPEC "Validation"): each page's bytes and format, page by page; then every page decoded; then
    # every page's orientation. The first defect found decides.
    for name, data in members:
        if not data:
            return Verdict("bad", f"page {name} is empty (0 bytes)")
        fmt = images.detect(data)
        if fmt in UNDECODABLE_FORMATS:
            return Verdict("human", f"page {name} is in a format the pipeline cannot read ({fmt})")
    try:
        pages = images.measure_pages(cbz)
    except ValueError as e:
        return Verdict("bad", str(e))
    for name, data in members:
        orientation = images.exif_orientation(data)
        if orientation in ROTATED:
            return Verdict("human", f"page {name} has EXIF orientation {orientation} (not upright)")
    mislabel = _mislabelled(pages, key, published, same)
    if mislabel:
        return Verdict("human", mislabel)
    return Verdict("ok", pages=pages, notes=_duplicates(pages) + _short(pages, published))


def _hamming(a, b):
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _mislabelled(pages, key, published, same):
    """Why the pages look like another chapter's, or None."""
    measured = [p for p in published if p["page_phashes"] is not None]
    if len(measured) < MIN_PUBLISHED:
        return None
    # ponytail: compares every pair of pages, O(pages x series pages); a band index (old _HammingIndex) if it gets slow
    others = [h for p in measured if not (p["key"] == key and same(p)) for h in p["page_phashes"]]
    ours = measures.summary(pages)["page_phashes"]
    if not ours or not others:
        return None
    matched = sum(any(_hamming(h, o) <= MISLABEL_HAMMING for o in others) for h in ours)
    if matched < MISLABEL_SHARE * len(ours):
        return None
    return f"mislabelled: {matched} of {len(ours)} non-blank pages repeat pages of other published chapters"


def _duplicates(pages):
    counts = Counter(p["sha1"] for p in pages)
    repeated = [p["name"] for p in pages if counts[p["sha1"]] > 1]
    return [f"duplicate pages: {', '.join(repeated)}"] if repeated else []


def _short(pages, published):
    lengths = [p["strip_length"] for p in published if p["strip_length"] is not None]
    if len(lengths) < MIN_PUBLISHED:
        return []
    ours, median = measures.strip_length(pages), statistics.median(lengths)
    if ours >= SHORT_RATIO * median:
        return []
    return [f"short: strip length {ours} is {ours / median:.0%} of the series' median {median:g}"]


def chapter_key(identity):
    """(season, side label, number) of an identity (names.identity's JSON): what tells chapters apart across
    sources and scanlators."""
    _, season, side, number, _ = json.loads(identity)
    return (season, side, number)
