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
