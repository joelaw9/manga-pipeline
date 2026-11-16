"""A chapter's page measurements in summary (SPEC "Validation", "Library copies"): what the short, mislabel and
library-copy rules compare. Pure."""

import statistics


def strip_length(pages):
    """Sum of height x 1000 / width over pages ({width, height}): a strip's length at a width of 1000."""
    return round(sum(p["height"] * 1000 / p["width"] for p in pages))


def summary(pages):
    """{pages_in, median_page_width, strip_length, page_phashes} of a chapter's pages (page rows or measure_pages
    dicts, in order); page_phashes leaves blank pages out (a blank page matches any other). Every value None for no
    pages."""
    if not pages:
        return {"pages_in": None, "median_page_width": None, "strip_length": None, "page_phashes": None}
    return {
        "pages_in": len(pages),
        "median_page_width": round(statistics.median(p["width"] for p in pages)),
        "strip_length": strip_length(pages),
        "page_phashes": [p["phash"] for p in pages if not p["blank"]],
    }


def bits_per_pixel(pages):
    """The download's bits per pixel: 8 × its pages' bytes over their pixels (pages: stored page rows)."""
    pixels = sum(p["width"] * p["height"] for p in pages)
    return 8 * sum(p["bytes"] for p in pages) / pixels if pixels else 0.0


def junk(edit, layout, width):
    """How much junk an edit removes (SPEC "Library copies"): pages dropped (layout pages), or strip rows removed per
    1000 px of the page width (layout strip; the gutter rule's join rows left out)."""
    if layout == "pages":
        return len(edit["drop_pages"])
    rows = sum(r["end"] - r["start"] for r in edit["strip_removed"] if r["why"] != "join")
    return rows * 1000 / width if width else 0.0
