"""Logo units of a chapter (SPEC "Junk decisions": units, candidates, the chapter edit's `blank`), both layouts.

- `measure(index, rgb)` at the scan: a gray-mark page's badge (images.classify with convert's constants, then
  marks.measure), as JSON: page, tight box, padded box, fill, ring share, template key text.
- `decide(found, survives, decide_unit)` with no decode: the blankable logos the rest of the edit leaves, grouped into
  variants (marks.cluster), each decided by its representatives (marks.resolve): junk gives 1 blank per member, keep
  nothing, undecided 1 unit (its representatives' keys) to offer.
- `review_images(page, logo)`: a logo candidate's 2 images.
"""

import numpy as np
from PIL import Image

from manga_pipeline import images
from manga_pipeline.junk import marks

REASON = "logo badge"
UNIT_HEIGHT = 300  # px: the before / after crops of a logo candidate are shown at this height
GAP = 8  # px between the before and after crops


def measure(index, rgb):
    """Return the logo of download page `index` (RGB array), or None when not gray-mark or its mark is faint."""
    a = np.asarray(rgb)
    cls, box = images.classify(a, images.COLOUR_BLOCK, images.TINT_P99, images.MARK_LIMITS)
    if cls != images.GRAY_MARK:
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


def page_survives(edit):
    """Return whether a logo survives a page-layout edit.

    Its page is not dropped, and any of its tight box lies inside the page's kept band crop.
    """
    drops = set(edit["drop_pages"])
    crops = {b["page"]: b["box"] for b in edit["bands"]}

    def survives(logo):
        if logo["page"] in drops:
            return False
        crop = crops.get(logo["page"])
        x0, y0, x1, y1 = logo["tight"]
        return crop is None or (max(x0, crop[0]) < min(x1, crop[2]) and max(y0, crop[1]) < min(y1, crop[3]))

    return survives


def strip_survives(removed):
    """Whether a logo survives a strip edit: any of its rows in the strip (`rows`, end exclusive) is not removed."""
    ranges = sorted((r["start"], r["end"]) for r in removed)

    def survives(logo):
        at, end = logo["rows"]
        for start, stop in ranges:
            if start > at:
                break
            at = max(at, stop)
        return at < end

    return survives


def decide(found, survives, decide_unit):
    """Decide the scan's logos by variant: the edit's blank entries and the undecided units.

    found: the scan's logos; survives(logo) -> bool; decide_unit(kind, key text) -> the deciding row or None. Returns
    {"blank": the edit's blank entries, "undecided": [{"kind": "logo", "key", "keys", "page", "logo", "reasons"}]}, 1
    undecided unit per undecided variant (its first member's page and logo).
    """
    live = [g for g in found if marks.blankable(g["ring_frac"]) and survives(g)]
    templates = [marks.Template.from_key(g["template"]) for g in live]
    blank, undecided = [], []
    for group in marks.cluster(templates):
        reps = [t.key_text() for t in marks.representatives([templates[i] for i in group])]
        row = marks.resolve([decide_unit("logo", k) for k in reps])
        members = [live[i] for i in group]
        if row is None:
            first = members[0]
            unit = {"kind": "logo", "key": reps[0], "keys": reps, "page": first["page"], "logo": first}
            undecided.append({**unit, "reasons": [REASON]})
        elif row["verdict"] == "junk":
            blank += [{"page": g["page"], "box": g["box"], "fill": g["fill"]} for g in members]
    return {"blank": sorted(blank, key=lambda b: b["page"]), "undecided": undecided}


def review_images(page, logo):
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
