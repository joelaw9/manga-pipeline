"""What a removed strip range looks like on a sheet (SPEC "Junk decisions": decision commands, candidates).

Its rows washed toward grey, and a grey bar and label beside them.
"""

import math
from collections.abc import Sequence
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

from manga_pipeline import strip

RED, GREY = (220, 0, 0), (128, 128, 128)
WASH = 0.5  # a removed row's pixels blended this far toward GREY
FONT_PX = 14
BAR_X, LABEL_X, LABEL_PAD = 4, 9, 4  # a label's bar lies BAR_X into its lane, its text LABEL_X, LABEL_PAD px after it

type Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


def font() -> Font:
    """Return the font sheets draw their labels with."""
    return ImageFont.load_default(size=FONT_PX)


class SheetPage(NamedTuple):
    """1 sheet: strip rows [a, b) drawn `scale` times, `height` px tall."""

    a: int
    b: int
    scale: float
    height: int

    def y(self, row: int) -> int:
        """Return a strip row's y on the sheet (its last row for a row past the end)."""
        return min(self.height - 1, round((row - self.a) * self.scale))

    def bottom(self, end: int) -> int:
        """Return the y of the last sheet row a range ending at strip row `end` covers (at most the last row)."""
        return round((min(end, self.b) - self.a) * self.scale) - 1

    def last_text(self) -> int:
        """Return the lowest y a label's top may take and still show whole on the sheet."""
        return max(0, self.height - FONT_PX - 2)

    def shows(self, rows: strip.Rows) -> bool:
        """Return whether strip rows [x, y) lie partly on this sheet."""
        return rows[1] > self.a and rows[0] < self.b


def washed(im: Image.Image, pg: SheetPage, removed: Sequence[strip.Rows]) -> Image.Image:
    """Return a sheet's strip image with the removed rows (not overlapping) blended WASH toward GREY."""
    for x, y in (r for r in removed if pg.shows(r)):
        top, bottom = round((max(x, pg.a) - pg.a) * pg.scale), round((min(y, pg.b) - pg.a) * pg.scale)
        if bottom > top:
            box = (0, top, im.width, bottom)
            part = im.crop(box)
            im.paste(Image.blend(part, Image.new("RGB", part.size, GREY), WASH), box[:2])
    return im


def removed_labels(
    draw: ImageDraw.ImageDraw, pg: SheetPage, labelled: Sequence[tuple[strip.Rows, str]], left: int
) -> None:
    """Draw each removed range's grey bar and its label on the sheet, in its lane at x `left`.

    A label starts below the one before it, so the labels of near ranges do not overlap, and never below the sheet's
    last text line, so it is always readable.
    """
    f, below = font(), 0
    for (x0, x1), label in ((r, t) for r, t in labelled if pg.shows(r)):
        top = pg.y(max(x0, pg.a))
        draw.line((left + BAR_X, top, left + BAR_X, max(top, pg.bottom(x1))), fill=GREY, width=3)
        at = min(max(top, below), pg.last_text())
        draw.text((left + LABEL_X, at), label, fill=GREY, font=f)
        below = at + FONT_PX + 2


def lane_width(labels: Sequence[str]) -> int:
    """Return the width of a lane that fits its longest label after LABEL_X, with LABEL_PAD px to spare."""
    f = font()
    return LABEL_X + max((math.ceil(f.getlength(t)) for t in labels), default=0) + LABEL_PAD
