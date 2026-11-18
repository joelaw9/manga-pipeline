"""Per-page reads the convert stage needs beyond images.measure_pages (SPEC "Conversion").

Whether avifenc may read a JPEG itself, whether jpegtran can gray it, the lossless pixel check, and the names pages are
extracted under. Copied from the bulk converter's optimizer/image.py (jpeg_feed) and optimizer/integrity.py
(grayscale_jpeg, pixels_match, _target_names).
"""

import io
from collections import Counter
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

from manga_pipeline import archive, images

EXIF_ORIENTATION = 0x0112
LUMA_TOLERANCE = 2  # gray re-encodes (jpegtran -grayscale) differ from RGB->L by colour-conversion rounding
EXT_ALIASES = {images.JPEG: (".jpg", ".jpeg")}
RGB_COMPONENT_IDS = [ord("R"), ord("G"), ord("B")]  # libjpeg reads these ids as RGB when no JFIF / Adobe marker says
COLOUR_COMPONENTS = 3  # a JPEG's colour components (Pillow's layers): Y, Cb, Cr or R, G, B


def jpeg_feed(data):
    """Return whether avifenc can be fed this JPEG itself instead of our decoded PNG.

    Only when it reads the pixels open_image decodes: a YCbCr or RGB-coded JPEG (Pillow mode RGB) with no colour profile
    or an sRGB one and no EXIF rotation (avifenc 1.4.2 refuses CMYK, applies an EXIF orientation as irot, and carries a
    non-sRGB profile into the AVIF). Header only.
    """
    with Image.open(io.BytesIO(data)) as im:
        return (
            im.mode == "RGB"
            and images.is_srgb(im.info.get("icc_profile"))
            and im.getexif().get(EXIF_ORIENTATION, 1) == 1
        )


def _jpeg_kind(im):
    """Return 'ycc' for a 3-component YCbCr JPEG, 'gray' for a 1-component one, else None.

    None for CMYK and RGB-coded ones (an Adobe transform 0, or no JFIF / Adobe marker and component ids 'R', 'G', 'B',
    as libjpeg's colour-space guess reads them).
    """
    if im.format not in ("JPEG", "MPO"):
        return None
    if im.mode == "L" and getattr(im, "layers", 1) == 1:
        return "gray"
    rgb_ids = (
        "jfif" not in im.info
        and "adobe" not in im.info
        and [c[0] for c in getattr(im, "layer", [])] == RGB_COMPONENT_IDS
    )
    if (
        im.mode == "RGB"
        and getattr(im, "layers", 0) == COLOUR_COMPONENTS
        and im.info.get("adobe_transform") != 0
        and not rgb_ids
    ):
        return "ycc"
    return None


def jpeg_gray(data):
    """Return whether `jpegtran -grayscale` grays this JPEG by keeping its stored Y plane.

    True for YCbCr or 1-component (it refuses CMYK, YCCK and RGB-coded ones). Header only.
    """
    with Image.open(io.BytesIO(data)) as im:
        return _jpeg_kind(im) is not None


class Page:
    """1 page's bytes and its decodes, each made at most once (the post-check reads a page several ways)."""

    def __init__(self, data):
        """Store the page's bytes; nothing is decoded yet."""
        self.data, self._raw, self._image = data, None, None

    def raw(self):
        """Pillow's own full decode."""
        if self._raw is None:
            im = Image.open(io.BytesIO(self.data))
            im.load()
            self._raw = im
        return self._raw

    def image(self):
        """images.open_image's (image, has_alpha), made from raw()."""
        if self._image is None:
            self._image = images.from_loaded(self.raw())
        return self._image


def _pixels(page, mode):
    """Return the page's pixels for comparison: RGBA (exact) or LA (luma).

    So a changed alpha channel does not match (beyond LUMA_TOLERANCE in luma mode) and a dropped fully-opaque one still
    does.
    """
    return np.asarray(page.image()[0].convert("LA" if mode == "luma" else "RGBA"))


def _native_luma(page):
    """Return the Y plane exactly as stored, for a YCbCr or 1-component JPEG; None for anything else.

    jpegtran -grayscale keeps Y untouched, while RGB->L of the decoded colour page can differ by a few levels where
    YCbCr->RGB clipped.
    """
    with Image.open(io.BytesIO(page.data)) as im:
        kind = _jpeg_kind(im)
        if kind == "gray":
            return np.asarray(page.raw())
        if kind is None or im.draft("YCbCr", im.size) is None or im.mode != "YCbCr":
            return None
        im.load()
        return np.asarray(im)[..., 0]


def pixels_match(original, output, mode):
    """Check that an output page (bytes or Page) matches its original losslessly: exact (RGBA) or luma.

    Luma of a YCbCr JPEG source against a JPEG output compares the stored Y planes exactly; other luma comparisons go
    through RGB->L, alpha kept, within LUMA_TOLERANCE.
    Returns (match, max difference or None on a shape mismatch).
    """
    original = original if isinstance(original, Page) else Page(original)
    output = output if isinstance(output, Page) else Page(output)
    a = b = None
    tol = 0
    if mode == "luma":
        with Image.open(io.BytesIO(original.data)) as im:
            ycc = _jpeg_kind(im) == "ycc"
        if ycc:
            a, b = _native_luma(original), _native_luma(output)
        if a is None or b is None:
            a, b, tol = None, None, LUMA_TOLERANCE
    if a is None or b is None:  # both None together
        a, b = _pixels(original, mode), _pixels(output, mode)
    if a.shape != b.shape:
        return False, None
    diff = int(np.abs(a.astype(np.int16) - b).max()) if a.size else 0
    return diff <= tol, diff


def has_colour(page):
    """Return whether the page has colour the luma comparison cannot ignore.

    The colour page class (images.classify, judged on the page composited onto white); a gray-mark page (1 compact
    badge) counts as gray.
    """
    im, alpha = page.image()
    rgb = np.asarray(images.flatten(im, alpha=alpha)[0].convert("RGB"))
    return images.classify(rgb, images.COLOUR_BLOCK, images.TINT_P99, images.MARK_LIMITS)[0] == images.COLOUR


def _page_ext(name, fmt):
    """Return the extension a page of format fmt is extracted under.

    A correct existing extension is kept (e.g. .jpeg), so renaming does not churn names.
    """
    ext = PurePosixPath(name).suffix.lower()
    if fmt == images.UNKNOWN or ext in EXT_ALIASES.get(fmt, (images.EXTENSION[fmt],)):
        return ext
    return images.EXTENSION[fmt]


def target_names(names_formats):
    """Return the page file names the bulk converter extracts to ([(archive member name, format)] in reading order).

    001, 002, ... (at least 3 digits) in reading order. Cover-named pages (archive.is_cover_name) keep their names when
    that keeps every page at its place in natural order; else each one but 000_cover gets the number of the numbered
    pages before it in front (Ch1_cover.jpg first -> 000-Ch1_cover.jpg, still a cover name).
    """
    width = max(3, len(str(sum(not archive.is_cover_name(n) for n, _ in names_formats))))

    def names(prefix):
        out, k = [], 0
        for n, f in names_formats:
            stem = PurePosixPath(n.replace("\\", "/")).stem
            if not archive.is_cover_name(n):
                k += 1
                stem = f"{k:0{width}d}"
            elif prefix and stem != archive.COVER_STEM:
                stem = f"{k:0{width}d}-{stem}"
            out.append(stem + _page_ext(n, f))
        return out

    plain = names(prefix=False)
    return plain if _order_kept(plain) else names(prefix=True)


def _order_kept(names):
    return sorted(names, key=archive.natural_key) == names


def name_problems(names):
    """Return why target names cannot stand for the pages ([] when they can).

    2 stems equal ignoring case (their outputs would share 1 name), an order the names do not keep, or names whose
    natural and plain orders differ (a cover inside the chapter, 001-Ch1_cover after 001: the post-check would refuse
    the output).
    """
    stems = Counter(PurePosixPath(n).stem.lower() for n in names)
    out = []
    dup = sorted(n for n in names if stems[PurePosixPath(n).stem.lower()] > 1)
    if dup:
        out.append(f"2 pages share a name stem: {', '.join(dup[:5])}")
    if not _order_kept(names):
        out.append("the renamed pages would not keep the reading order")
    elif sorted(names) != names:
        out.append("the renamed pages sort differently by natural and by plain order")
    return out


def extract(cbz, x_dir):
    """Write a CBZ's pages (archive.archive_pages order) into x_dir under target_names.

    Returns (their paths, the name_problems); with a problem nothing is written.
    """
    members = archive.pages(cbz)
    names = target_names([(n, images.detect(d)) for n, d in members])
    problems = name_problems(names)
    if problems:
        return [], problems
    x_dir = Path(x_dir)
    x_dir.mkdir(parents=True, exist_ok=True)
    paths = [x_dir / n for n in names]
    for path, (_, data) in zip(paths, members, strict=True):
        path.write_bytes(data)
    return paths, []
