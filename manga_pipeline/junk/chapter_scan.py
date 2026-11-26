"""A chapter's junk scan and decision on its extracted pages (SPEC "Junk decisions"), writing nothing.

The junk stage scans a chapter (scan_strip or page_units.scan by the series' layout) and later decides it from the
stored scan (apply); a source review's sample (SPEC "Source review") runs both on a CBZ in hand. Every input is given:
the pages, the series' decisions, known watermarks, page aspects (page_aspects reads them) and the OCR device and
workers. The
caller stores what it keeps.
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from PIL import Image

from manga_pipeline import db, images, strip
from manga_pipeline.convert import plan
from manga_pipeline.junk import candidates, logo_units, ocr, page_units, store, strip_units

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


def page_aspects(conn: db.Connection, series_id: int, sizes: Sequence[strip.Size]) -> tuple[list[float], bool]:
    """Return the series' page aspect candidates and whether they are stored; writes nothing.

    Stored (series_conversion.page_aspect), else computed from this chapter's page sizes (empty when none is page-like).
    """
    row = db.one_row(conn, "SELECT page_aspect FROM series_conversion WHERE series_id = %s", (series_id,))
    if row["page_aspect"] is not None:
        return plan.read_page_aspect(series_id, row["page_aspect"]), True
    return strip.page_aspect([sizes]) or [], False


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


def scan_strip(pages: Extracted, decisions: store.Decisions, device: str, workers: int) -> StoredStripScan:
    """Scan a strip chapter's content pages: its units, OCR on the edge blocks no decision covers, its logos.

    OCR runs on `device`, `workers` units at once (ocr.texts).
    """
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
        lambda ims: ocr.texts(ims, device, workers),
        lambda sig: decisions.decide("block", sig) is None,
    )
    return {**units, "marks": marks}


def renderer(pages: Extracted) -> Callable[[int, int], Image.Image]:
    """Return render(start, end): strip rows [start, end) over the content pages, laid as scan_strip lays them.

    The matcher's pixels (SPEC "Junk decisions": chains and cuts); decoded pages are cached as render_rows keeps them.
    """
    content = strip.content_indexes(pages.names)
    width, laid, _ = strip.strip_layout([pages.sizes[i] for i in content])
    placed: list[strip.StripPage] = [{**p, "path": str(pages.paths[i])} for p, i in zip(laid, content, strict=True)]
    cache: dict[str, Image.Image] = {}

    def render(start: int, end: int) -> Image.Image:
        return strip.render_rows(placed, width, start, end, cache)

    return render


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
    """What decisions make of a chapter's scan: its edit, watermarks to remember, undecided units, refusal, W5 gaps.

    Its chapter edit, the watermark keys to remember, the units left undecided (the layout's, then the logos'; the ones
    candidates.select would offer are the review's candidates), why the edit is refused (the chains that should have
    matched and did not, else strip_units' kept_problem; None when it is not), and a strip's remnant gaps
    (strip_units.rest_members' input; none for pages or a refused edit).
    """

    edit: plan.ChapterEdit
    watermarks: list[str]
    undecided: Sequence[candidates.Unit]
    problem: str | None
    rest: list[strip_units.RestGap]


def apply(
    sc: Scan,
    decide_unit: store.DecideUnit,
    watermarks: Sequence[str],
    cuts: Sequence[db.Row] = (),
    render: Callable[[int, int], Image.Image] | None = None,
) -> Applied:
    """Apply the decisions on a scan: its layout's, then the logos' on what that leaves.

    A strip's layout decisions take the series' known watermarks (junk_watermark keys), its cut decisions
    (store.Decisions.cuts) and render (renderer; called only when a chain or a cut decision is tried); a refused strip
    edit stops before the logos.
    """
    edit: plan.ChapterEdit = {
        "drop_pages": [],
        "bands": [],
        "join": [],
        "split": False,
        "split_pages": None,
        "strip_removed": [],
        "blank": [],
    }
    found: list[str] = []
    undecided: list[candidates.Unit]
    rest: list[strip_units.RestGap] = []
    if sc["layout"] == "strip":
        got = strip_units.decide(sc, decide_unit, [images.sig_from_str(k) for k in watermarks], cuts, render)
        if got["misses"]:
            return Applied(edit, [], [], "; ".join(got["misses"]), [])
        if got["kept_problem"]:
            return Applied(edit, [], [], got["kept_problem"], [])
        edit["strip_removed"] = got["removed"]
        found = list(got["watermarks"])
        logos = logo_units.decide(sc["marks"], logo_units.strip_survives(edit["strip_removed"]), decide_unit)
        undecided = [*got["undecided"], *logos["undecided"]]
        rest = got["rest"]
    else:
        got = page_units.decide(sc, decide_unit)
        edit = {**edit, **got["edit"]}
        logos = logo_units.decide(sc["marks"], logo_units.page_survives(edit), decide_unit)
        undecided = [*got["undecided"], *logos["undecided"]]
    edit["blank"] = logos["blank"]
    return Applied(edit, found, undecided, None, rest)
