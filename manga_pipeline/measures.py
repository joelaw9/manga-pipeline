"""A chapter's page measurements in summary (SPEC "Validation", "Library copies").

What the short, mislabel and library-copy rules compare. Pure.
"""

import statistics
from collections.abc import Iterable, Sequence
from typing import ReadOnly, TypedDict


class PageSize(TypedDict):
    """A page's width and height in px (read only: a measured page, images.MeasuredPage, is one)."""

    width: ReadOnly[int]
    height: ReadOnly[int]


class SummaryPage(PageSize):
    """A page's size and pHash, None for a blank page (images.measure_pages)."""

    phash: ReadOnly[str | None]


class StoredPage(PageSize):
    """A page's size and its size in bytes."""

    bytes: ReadOnly[int]


class Summary(TypedDict):
    """A chapter's page measurements in summary; every value None for no pages."""

    pages_in: int | None
    median_page_width: int | None
    strip_length: int | None
    page_phashes: list[str] | None


class RemovedRows(TypedDict):
    """Strip rows [start, end) an edit removed, and why ("join": the gutter rule's)."""

    start: ReadOnly[int]
    end: ReadOnly[int]
    why: ReadOnly[str]


class JunkEdit(TypedDict):
    """What junk reads of an edit: its dropped pages (only counted) and its removed strip rows."""

    drop_pages: ReadOnly[Sequence[object]]
    strip_removed: ReadOnly[Sequence[RemovedRows]]


def strip_length(pages: Iterable[PageSize]) -> int:
    """Sum of height x 1000 / width over pages ({width, height}): a strip's length at a width of 1000."""
    return round(sum(p["height"] * 1000 / p["width"] for p in pages))


def summary(pages: Sequence[SummaryPage]) -> Summary:
    """Return {pages_in, median_page_width, strip_length, page_phashes} of a chapter's pages.

    pages: measure_pages dicts (or stored page rows read as them), in order. page_phashes leaves blank pages out (a
    blank page, whose pHash is None, matches any other). Every value None for no pages.
    """
    if not pages:
        return {"pages_in": None, "median_page_width": None, "strip_length": None, "page_phashes": None}
    return {
        "pages_in": len(pages),
        "median_page_width": round(statistics.median(p["width"] for p in pages)),
        "strip_length": strip_length(pages),
        "page_phashes": [h for p in pages if (h := p["phash"]) is not None],
    }


def bits_per_pixel(pages: Sequence[StoredPage]) -> float:
    """Return the download's bits per pixel: 8 times its pages' bytes over their pixels (pages: stored page rows)."""
    pixels = sum(p["width"] * p["height"] for p in pages)
    return 8 * sum(p["bytes"] for p in pages) / pixels if pixels else 0.0


def junk(edit: JunkEdit, layout: str, width: int) -> float:
    """Return how much junk an edit removes (SPEC "Library copies").

    Pages dropped (layout pages), or strip rows removed per 1000 px of the page width (layout strip; the gutter
    rule's join rows left out).
    """
    if layout == "pages":
        return len(edit["drop_pages"])
    rows = sum(r["end"] - r["start"] for r in edit["strip_removed"] if r["why"] != "join")
    return rows * 1000 / width if width else 0.0
