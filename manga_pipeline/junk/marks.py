"""Logo marks (SPEC "Junk decisions": units, keys): a gray-mark page's badge measured inside its mark box
(images.gray_mark_box), its template, the variant match, variants (cluster, representatives) and a variant's decision
from its representatives' (resolve). Ported from the bulk converter's optimizer/marks.py with its constants; the
blank itself is images.blank (convert applies it).

A mark is one small strongly coloured badge. Its tight box is the pixels with channel spread > STRONG inside the mark
box (rows/columns with < EDGE_FRAC of the peak count are trimmed as JPEG noise); the padded box (PAD_REL x its height
around it) is what a blank overwrites. The ring is a RING px frame around the padded box. A mark is blankable only
when the ring is plain: >= RING_FRAC of its pixels lie within RING_TOL of the ring median on every channel.

Two templates are the same variant when their box aspect ratios are within ASPECT_TOL (the template is resized), the
coloured-pixel masks overlap with IoU >= IOU_MIN and the mean |dRGB| over the template's coloured pixels is <=
DELTA_MAX. Pixels are RGB uint8 arrays (H, W, 3). Boxes are (x0, y0, x1, y1), end-exclusive.
"""

import base64
import hashlib
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

# Calibrated by the bulk converter on the 91 Akuyaku Kizoku pilot marks; fixed (stored decisions use them).
STRONG = 40  # pixel channel spread that marks a pixel as part of the badge
EDGE_FRAC = 0.15  # a row/column belongs to the badge when it has this share of the peak row/column's strong pixels
PAD_REL = 0.75  # tight box height x this = px added around it (the badge's grey pill)
RING = 10  # px width of the ring tested around the padded box
RING_TOL = 10  # ring pixels must be within this of the ring median on every channel
RING_FRAC = 0.95  # share of ring pixels that must pass
ASPECT_TOL = 0.10  # variants: box aspect ratios within this fraction (the same badge comes in several scales)
IOU_MIN = 0.7  # variants: coloured-pixel mask IoU
DELTA_MAX = 40  # variants: mean |dRGB| over the template's coloured pixels (same badge: median 25, max 50; others 53+)


# ---------------------------------------------------------------- template
class Template:
    """Tight-box RGB plus the mask of coloured pixels. Stored as an RGBA PNG (alpha = mask)."""

    def __init__(self, rgb, mask):
        self.rgb = np.where(mask[..., None], rgb, 0).astype(np.uint8)
        self.mask = np.asarray(mask, dtype=bool)
        if not self.mask.any():
            msg = "template has no coloured pixels"
            raise ValueError(msg)

    @property
    def size(self):
        return self.mask.shape[1], self.mask.shape[0]

    def to_png(self):
        rgba = np.dstack([self.rgb, self.mask.astype(np.uint8) * 255])
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG")
        return buf.getvalue()

    @classmethod
    def from_png(cls, data):
        a = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))
        return cls(a[..., :3], a[..., 3] > 127)

    def key_text(self):
        """The template as a decision key: its RGBA PNG, base64."""
        return base64.b64encode(self.to_png()).decode("ascii")

    @classmethod
    def from_key(cls, text):
        return cls.from_png(base64.b64decode(text))

    def review_id(self):
        """Stable id from the pixels (not the PNG encoding, not a position)."""
        h = hashlib.sha1(repr(self.size).encode() + self.mask.tobytes() + self.rgb.tobytes())
        return "m" + h.hexdigest()[:8]


def same_variant(a, b):
    """True when template b is the same badge as template a (see module docstring for the three tests)."""
    (aw, ah), (bw, bh) = a.size, b.size
    if abs(aw / ah - bw / bh) > ASPECT_TOL * max(aw / ah, bw / bh):
        return False
    if b.size != a.size:
        rgb = np.asarray(Image.fromarray(b.rgb).resize(a.size, Image.BILINEAR))
        mask = np.asarray(Image.fromarray(b.mask.astype(np.uint8) * 255).resize(a.size, Image.NEAREST)) > 127
    else:
        rgb, mask = b.rgb, b.mask
    if (a.mask & mask).sum() / (a.mask | mask).sum() < IOU_MIN:
        return False
    diff = np.abs(a.rgb[a.mask].astype(np.int16) - rgb[a.mask].astype(np.int16))
    return diff.mean() <= DELTA_MAX


def cluster(templates):
    """Group templates into variants (single link: a template joins every group holding a match). Each group is a
    sorted list of indices into `templates`; groups are ordered by their first index."""
    groups = []
    for i, t in enumerate(templates):
        joined = [g for g in groups if any(same_variant(templates[k], t) for k in g)]
        merged = sorted([i] + [k for g in joined for k in g])
        groups = [g for g in groups if g not in joined] + [merged]
    return sorted(groups)


def representatives(templates):
    """A covering subset of one variant's templates, in order: every template matches at least one of them
    (same_variant(representative, template), the direction Library.decision tests). What a review decision records:
    deciding these decides every member, in a few rows instead of one per page."""
    reps = []
    for t in templates:
        if not any(same_variant(r, t) for r in reps):
            reps.append(t)
    return reps


# ---------------------------------------------------------------- measuring and filling
def _pad(box, shape, n):
    x0, y0, x1, y1 = box
    h, w = shape[:2]
    return max(0, x0 - n), max(0, y0 - n), min(w, x1 + n), min(h, y1 + n)


def ring_stats(rgb, box):
    """(fill, frac) of the RING px frame around `box`: its per-channel median colour and the share of its pixels
    within RING_TOL of that median on every channel. A frame with no pixels gives frac 0."""
    a = np.asarray(rgb)
    x0, y0, x1, y1 = _pad(box, a.shape, RING)
    inside = np.ones((y1 - y0, x1 - x0), dtype=bool)
    inside[box[1] - y0 : box[3] - y0, box[0] - x0 : box[2] - x0] = False
    ring = a[y0:y1, x0:x1][inside].astype(np.int16)
    if not len(ring):
        return (0, 0, 0), 0.0
    med = np.median(ring, axis=0)
    frac = float((np.abs(ring - med).max(axis=1) <= RING_TOL).mean())
    return tuple(round(v) for v in med), frac


@dataclass(frozen=True)
class Mark:
    tight: tuple  # box of the strongly coloured pixels
    box: tuple  # tight box + PAD_REL x its height, clamped to the page: what a blank-out overwrites
    fill: tuple  # ring median RGB
    ring_frac: float
    template: Template

    @property
    def blankable(self):
        return self.ring_frac >= RING_FRAC


def _solid(counts):
    """(start, stop) of the profile entries holding at least EDGE_FRAC of the peak: JPEG chroma noise leaves sparse
    strong pixels outside the badge, which would otherwise stretch its box."""
    keep = np.nonzero(counts >= EDGE_FRAC * counts.max())[0]
    return int(keep[0]), int(keep[-1]) + 1


def measure(rgb, bbox):
    """The Mark inside Part B's gray-mark box (formats.gray_mark_box), or None when no pixel in it is strongly
    coloured, or none is left inside the solid rows x solid columns (a faint mark: nothing to template or blank; it
    only goes gray). The rows and columns are trimmed separately, so scattered specks can leave an empty overlap
    (Before the Real Heroine Steps In, Eva_Chapter 52 p031)."""
    a = np.asarray(rgb)
    x0, y0, x1, y1 = bbox
    sub = a[y0:y1, x0:x1].astype(np.int16)
    strong = (sub.max(axis=2) - sub.min(axis=2)) > STRONG
    if not strong.any():
        return None
    (sx0, sx1), (sy0, sy1) = _solid(strong.sum(axis=0)), _solid(strong.sum(axis=1))
    strong[:, :sx0] = strong[:, sx1:] = strong[:sy0] = strong[sy1:] = False
    if not strong.any():
        return None
    tight = (x0 + sx0, y0 + sy0, x0 + sx1, y0 + sy1)
    tx0, ty0, tx1, ty1 = tight
    box = _pad(tight, a.shape, round(PAD_REL * (tight[3] - tight[1])))
    fill, frac = ring_stats(a, box)
    template = Template(a[ty0:ty1, tx0:tx1], strong[ty0 - y0 : ty1 - y0, tx0 - x0 : tx1 - x0])
    return Mark(tight, box, fill, frac, template)


# ---------------------------------------------------------------- a variant's decision
def resolve(rows):
    """A logo variant's deciding row from its representatives' deciding rows (store lookups; None = undecided): a keep
    on any representative wins, else a junk on any, else None (undecided). The bulk converter counted only series
    keeps; a global keep counts here too, so a candidate a decision covers never leaves its variant undecided."""
    keeps = [r for r in rows if r is not None and r["verdict"] == "keep"]
    if keeps:
        return keeps[0]
    return next((r for r in rows if r is not None and r["verdict"] == "junk"), None)
