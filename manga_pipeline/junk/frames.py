"""Template frames (SPEC "Junk decisions", units): a page's outer border, shrunk, as the key of a scanlator template
page. Copied from the bulk converter's optimizer/frames.py (signature and matching; its cross-series index is not
ported). The constants are frozen: the imported frame decisions were made with them.

The frame is the page shrunk to W x H RGB (BOX) keeping only the outer MARGIN_X / MARGIN_Y cells on every side. It
carries template content (`has_content`) when it is not one plain tone and at least INK_FRAC of its cells are
mid-tone or coloured. 2 frames match when the RMS of their RGB difference is <= RMS_MAX and their aspects (height /
width) are within ASPECT_TOL.
"""

from typing import NamedTuple

import numpy as np
from PIL import Image

W, H = 48, 72  # shrunk page, cells
MARGIN_X, MARGIN_Y = 4, 6  # frame width in cells (8% of W / H)
RMS_MAX = 18  # match: RMS of the RGB difference over the frame cells
ASPECT_TOL = 0.12  # match: aspect ratios within this fraction
PLAIN_BAND = 8  # luma distance from the median counted as "the same tone"
PLAIN_FRAC = 0.85  # plain frame: at least this share of the cells within PLAIN_BAND
INK_LO, INK_HI = 40, 200  # mid-tone luma range [INK_LO, INK_HI)
INK_CHROMA = 24  # coloured cell: channel spread above this
INK_FRAC = 0.30  # template content: at least this share of mid-tone or coloured cells

_MASK = np.zeros((H, W), dtype=bool)
_MASK[:MARGIN_Y] = _MASK[-MARGIN_Y:] = True
_MASK[:, :MARGIN_X] = _MASK[:, -MARGIN_X:] = True
CELLS = int(_MASK.sum())


class Frame(NamedTuple):
    vec: np.ndarray  # uint8, CELLS * 3 (RGB per frame cell, row-major)
    aspect: float  # page height / width


def frame_of_small(small, aspect):
    """Frame from an (H, W, 3) uint8 shrunk page."""
    small = np.asarray(small, dtype=np.uint8)
    if small.shape != (H, W, 3):
        msg = f"shrunk page must be {(H, W, 3)}, got {small.shape}"
        raise ValueError(msg)
    return Frame(small[_MASK].reshape(-1), float(aspect))


def frame(im, size=None):
    """Frame of a PIL page. A JPEG not yet decoded is draft-decoded first, as the stored frames were (it changes the
    pixels). `size` = (width, height) of the original page when `im` is already a draft decode; defaults to im.size."""
    w, h = size or im.size
    im.draft("RGB", (W * 4, H * 4))  # JPEG only, no-op once decoded
    return frame_of_small(np.asarray(im.convert("RGB").resize((W, H), Image.Resampling.BOX)), h / w)


def has_content(f):
    """True when the frame carries template content (not one plain tone, >= INK_FRAC inked cells)."""
    cells = f.vec.reshape(-1, 3).astype(np.int16)
    lum = cells.mean(axis=1)
    if (np.abs(lum - np.median(lum)) <= PLAIN_BAND).mean() >= PLAIN_FRAC:
        return False
    inked = ((lum >= INK_LO) & (lum < INK_HI)) | ((cells.max(axis=1) - cells.min(axis=1)) > INK_CHROMA)
    return bool(inked.mean() >= INK_FRAC)


def distance(a, b):
    """RMS of the RGB difference between 2 frames (0-255 scale)."""
    d = a.vec.astype(np.float64) - b.vec.astype(np.float64)
    return float(np.sqrt((d**2).mean()))


def matches(a, b):
    return abs(a.aspect - b.aspect) <= ASPECT_TOL * max(a.aspect, b.aspect) and distance(a, b) <= RMS_MAX


def from_bytes(blob, aspect):
    """Frame from its stored bytes. A vector of another length was made with other geometry: ValueError."""
    vec = np.frombuffer(blob, np.uint8)
    if vec.size != CELLS * 3:
        msg = f"stored frame has {vec.size} bytes, expected {CELLS * 3} (CELLS * 3)"
        raise ValueError(msg)
    return Frame(vec, float(aspect))


def key_text(f):
    """The frame as a decision key: the vector in hex, '|', the aspect."""
    return f"{f.vec.tobytes().hex()}|{f.aspect!r}"


def from_key(text):
    vec, aspect = text.split("|")
    return from_bytes(bytes.fromhex(vec), float(aspect))
