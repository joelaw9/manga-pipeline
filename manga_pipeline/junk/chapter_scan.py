"""A chapter's junk scan and decision on its extracted pages (SPEC "Junk decisions"), writing nothing.

The junk stage scans a chapter (scan_strip or page_units.scan by the series' layout) and later decides it from the
stored scan (apply); a source review's sample (SPEC "Source review") runs both on a CBZ in hand. Every input is given:
the pages, the series' decisions, known watermarks, page aspects and the OCR device. The caller stores what it keeps.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from manga_pipeline import images, strip
from manga_pipeline.convert import plan
from manga_pipeline.junk import candidates, logo_units, ocr, page_units, store, strip_units

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


class StoredStripScan(strip_units.StripScan):
    """A strip chapter's scan as junk_scan holds it: strip_units.scan's, with its content pages' logos.

    A scan an older version stored also holds "content" (the content pages' download indexes), which nothing reads.
    """

    marks: list[logo_units.StripLogo]


type Scan = StoredStripScan | page_units.PageScan | page_units.SplitScan  # junk_scan.scan, told apart by its layout


class Extracted(NamedTuple):
    """A chapter's pages written to disk in archive order: their paths, archive names and (width, height)."""

    paths: list[Path]
    names: list[str]
    sizes: list[strip.Size]


def scan_strip(pages: Extracted, decisions: store.Decisions, device: str) -> StoredStripScan:
    """Scan a strip chapter's content pages: its units, OCR on the edge blocks no decision covers, its logos."""
    content = strip.content_indexes(pages.names)
    cpaths = [pages.paths[i] for i in content]
    rows: list[NDArray[np.bool_]]
    logos: list[logo_units.Logo]
    rows, logos = [], []
    for i, path in zip(content, cpaths, strict=True):
        im = strip.open_page(path)
        rows.append(strip.page_rows(im))
        logo = logo_units.measure(i, im)
        if logo is not None:
            logos.append(logo)
    st = strip.make_strip(cpaths, [pages.sizes[i] for i in content], rows)
    placed = dict(zip(content, st.pages, strict=True))
    marks = [_strip_logo(logo, placed[logo["page"]]) for logo in logos]
    units = strip_units.scan(
        st,
        lambda im: ocr.text(im, device),
        lambda sig: decisions.decide("block", sig) is None,
    )
    return {**units, "marks": marks}


def scan_pages(pages: Extracted, uniform: Sequence[bool], aspects: Sequence[float]) -> Scan:
    """Scan a pages chapter: page_units.scan over all its pages (uniform: a page with no junk signature)."""
    return page_units.scan(pages.paths, pages.names, pages.sizes, uniform, aspects)


def seen_units(sc: Scan) -> list[tuple[str, str]]:
    """Return the units [(kind, key)] a scan records as seen in its chapter (junk_seen)."""
    if sc["layout"] == "strip":
        return [("block", b["sig"]) for b in sc["blocks"]]
    if sc["split"] is True:
        return []
    out: list[tuple[str, str]] = []
    for u in sc["edges"]:
        if u["frame"]:
            out.append(("frame", u["frame"]))
        if u["sig"]:
            out.append(("page", u["sig"]))
    out += [("band", b["sig"]) for found in sc["bands"].values() for b in found]
    return out


def _strip_logo(logo: logo_units.Logo, page: strip.LaidPage) -> logo_units.StripLogo:
    """Return a logo with its tight box's rows in the strip (its page laid there), end exclusive."""
    _, y0, _, y1 = logo["tight"]
    top, th, h = page["top"], page["th"], page["h"]
    return {**logo, "rows": [top + y0 * th // h, top + -(-y1 * th // h)]}


class Applied(NamedTuple):
    """What decisions make of a chapter's scan: its edit, watermarks to remember, undecided units, refusal.

    Its chapter edit, the watermark keys to remember, the units left undecided (the layout's, then the logos'; the ones
    candidates.select would offer are the review's candidates), and why the edit is refused (strip_units'
    kept_problem; None when it is not).
    """

    edit: plan.ChapterEdit
    watermarks: list[str]
    undecided: Sequence[candidates.Unit]
    problem: str | None


def apply(sc: Scan, decide_unit: store.DecideUnit, watermarks: Sequence[str]) -> Applied:
    """Apply the decisions on a scan: its layout's, then the logos' on what that leaves.

    A strip's layout decisions take the series' known watermarks (junk_watermark keys); a refused strip edit stops
    before the logos.
    """
    edit: plan.ChapterEdit = {
        "drop_pages": [],
        "bands": [],
        "join": [],
        "split": False,
        "strip_removed": [],
        "blank": [],
    }
    found: list[str] = []
    undecided: list[candidates.Unit]
    if sc["layout"] == "strip":
        got = strip_units.decide(sc, decide_unit, [images.sig_from_str(k) for k in watermarks])
        if got["kept_problem"]:
            return Applied(edit, [], [], got["kept_problem"])
        edit["strip_removed"] = got["removed"]
        found = list(got["watermarks"])
        logos = logo_units.decide(sc["marks"], logo_units.strip_survives(edit["strip_removed"]), decide_unit)
        undecided = [*got["undecided"], *logos["undecided"]]
    else:
        got = page_units.decide(sc, decide_unit)
        edit = {**edit, **got["edit"]}
        logos = logo_units.decide(sc["marks"], logo_units.page_survives(edit), decide_unit)
        undecided = [*got["undecided"], *logos["undecided"]]
    edit["blank"] = logos["blank"]
    return Applied(edit, found, undecided, None)
