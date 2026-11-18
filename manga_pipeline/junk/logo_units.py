"""Logo units of a chapter (SPEC "Junk decisions": units, candidates, the chapter edit's `blank`), both layouts.

- `measure(index, rgb)` at the scan: a gray-mark page's badge (images.classify with convert's constants, then
  marks.measure), as JSON: page, tight box, padded box, fill, ring share, template key text.
- `decide(found, survives, decide_unit)` with no decode: the blankable logos the rest of the edit leaves, grouped into
  variants (marks.cluster), each decided by its representatives (marks.resolve): junk gives 1 blank per member, keep
  nothing, undecided 1 unit (its representatives' keys) to offer.
- `review_images(page, logo)`: a logo candidate's 2 images.
"""

from collections.abc import Callable, Iterable, Sequence
from typing import Literal, TypedDict

import numpy as np
from PIL import Image

from manga_pipeline import images, strip
from manga_pipeline.convert import plan
from manga_pipeline.junk import marks, store

REASON = "logo badge"
UNIT_HEIGHT = 300  # px: the before / after crops of a logo candidate are shown at this height
GAP = 8  # px between the before and after crops


class Logo(TypedDict):
    """A gray-mark page's logo as the scan holds it (measure).

    Its page (download index), tight box and padded box ([x0, y0, x1, y1], end-exclusive), ring fill [r, g, b], ring
    share and template key text (marks.Template.key_text).
    """

    page: int
    tight: list[int]
    box: list[int]
    fill: list[int]
    ring_frac: float
    template: str


class StripLogo(Logo):
    """A logo of a strip chapter, with its tight box's rows [start, end) in the strip."""

    rows: list[int]


class LogoUnit(TypedDict):
    """An undecided logo variant: its first representative's key, every representative's key, its first member.

    The member's page and logo, and its candidate reasons.
    """

    kind: Literal["logo"]
    key: str
    keys: list[str]
    page: int
    logo: Logo
    reasons: list[str]


class LogoDecision(TypedDict):
    """What decisions make of a scan's logos: the chapter edit's blank entries and the undecided variants."""

    blank: list[plan.Blank]
    undecided: list[LogoUnit]


def measure(index: int, rgb: Image.Image) -> Logo | None:
    """Return the logo of download page `index` (RGB array), or None when not gray-mark or its mark is faint."""
    a = np.asarray(rgb)
    cls, box = images.classify(a, images.COLOUR_BLOCK, images.TINT_P99, images.MARK_LIMITS)
    if cls != images.GRAY_MARK or box is None:
        return None
    m = marks.measure(a, box)
    if m is None:
        return None
    return {
        "page": index,
        "tight": [int(v) for v in m.tight],
        "box": [int(v) for v in m.box],
        "fill": [int(v) for v in m.fill],
        "ring_frac": m.ring_frac,
        "template": m.template.key_text(),
    }


def page_survives(edit: plan.PageLayoutEdit) -> Callable[[Logo], bool]:
    """Return whether a logo survives a page-layout edit.

    Its page is not dropped, and any of its tight box lies inside the page's kept band crop.
    """
    drops = set(edit["drop_pages"])
    crops = {b["page"]: b["box"] for b in edit["bands"]}

    def survives(logo: Logo) -> bool:
        if logo["page"] in drops:
            return False
        crop = crops.get(logo["page"])
        x0, y0, x1, y1 = logo["tight"]
        return crop is None or (max(x0, crop[0]) < min(x1, crop[2]) and max(y0, crop[1]) < min(y1, crop[3]))

    return survives


def strip_survives(removed: Iterable[strip.RowRange]) -> Callable[[StripLogo], bool]:
    """Whether a logo survives a strip edit: any of its rows in the strip (`rows`, end exclusive) is not removed."""
    ranges = sorted((r["start"], r["end"]) for r in removed)

    def survives(logo: StripLogo) -> bool:
        at, end = logo["rows"]
        for start, stop in ranges:
            if start > at:
                break
            at = max(at, stop)
        return at < end

    return survives


def decide[L: Logo](found: Sequence[L], survives: Callable[[L], bool], decide_unit: store.DecideUnit) -> LogoDecision:
    """Decide the scan's logos by variant: the edit's blank entries and the undecided units.

    found: the scan's logos; survives(logo) -> bool; decide_unit(kind, key text) -> the deciding row or None. Returns
    {"blank": the edit's blank entries, "undecided": [{"kind": "logo", "key", "keys", "page", "logo", "reasons"}]}, 1
    undecided unit per undecided variant (its first member's page and logo).
    """
    live = [g for g in found if marks.blankable(g["ring_frac"]) and survives(g)]
    templates = [marks.Template.from_key(g["template"]) for g in live]
    blank: list[plan.Blank]
    undecided: list[LogoUnit]
    blank, undecided = [], []
    for group in marks.cluster(templates):
        reps = [t.key_text() for t in marks.representatives([templates[i] for i in group])]
        row = marks.resolve([decide_unit("logo", k) for k in reps])
        members = [live[i] for i in group]
        if row is None:
            first = members[0]
            undecided.append(
                {
                    "kind": "logo",
                    "key": reps[0],
                    "keys": reps,
                    "page": first["page"],
                    "logo": first,
                    "reasons": [REASON],
                }
            )
        elif row["verdict"] == "junk":
            blank.extend({"page": g["page"], "box": g["box"], "fill": g["fill"]} for g in members)
    return {"blank": sorted(blank, key=lambda b: b["page"]), "undecided": undecided}


def review_images(page: Image.Image, logo: Logo) -> tuple[Image.Image, Image.Image]:
    """Return (unit image, context image) of a logo candidate.

    The badge's surroundings before and after the blank, side by side; the page. page: the source page (PIL RGB).
    """
    a = np.asarray(page)
    x0, y0, x1, y1 = logo["box"]
    pad = y1 - y0
    h, w = a.shape[:2]
    area = (max(0, x0 - 2 * pad), max(0, y0 - pad), min(w, x1 + 2 * pad), min(h, y1 + pad))
    before = page.crop(area)
    after = Image.fromarray(images.blank(a, logo["box"], logo["fill"])).crop(area)
    scale = UNIT_HEIGHT / before.height
    size = (max(1, round(before.width * scale)), UNIT_HEIGHT)
    before, after = before.resize(size), after.resize(size)
    unit = Image.new("RGB", (2 * size[0] + GAP, UNIT_HEIGHT), "white")
    unit.paste(before, (0, 0))
    unit.paste(after, (size[0] + GAP, 0))
    return unit, page
