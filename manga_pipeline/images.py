"""Page images (contracts section 1).

Format detection from file contents, dimensions, decoding to 8-bit sRGB, and page classes. Copied from the bulk
converter's optimizer/formats.py (its archive half is archive.py); reads no config.
"""

import functools
import io
import os
import struct
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, ReadOnly, TypedDict

import imagehash
import numpy as np
from numpy.typing import NDArray
from PIL import ExifTags, Image, ImageCms, JpegImagePlugin
from PIL._imagingcms import CmsProfile

from manga_pipeline import archive

Image.MAX_IMAGE_PIXELS = None  # webtoon strips exceed Pillow's bomb guard legitimately (set once, for every module)

JPEG, PNG, GIF, BMP = "JPEG", "PNG", "GIF", "BMP"
WEBP_LOSSY, WEBP_LOSSLESS, WEBP_ANIMATED = "WEBP_LOSSY", "WEBP_LOSSLESS", "WEBP_ANIMATED"
AVIF, JXL, UNKNOWN = "AVIF", "JXL", "UNKNOWN"

EXTENSION = {
    JPEG: ".jpg",
    PNG: ".png",
    GIF: ".gif",
    BMP: ".bmp",
    WEBP_LOSSY: ".webp",
    WEBP_LOSSLESS: ".webp",
    WEBP_ANIMATED: ".webp",
    AVIF: ".avif",
    JXL: ".jxl",
}
HEADER_BYTES = 256 * 1024  # enough of a file for dimensions() on every format we meet but AVIF (the whole file)
# WebP header bytes read for the canvas size: through it (VP8X, VP8), through the VP8L size field
VP8X_HEAD, VP8_HEAD, VP8L_HEAD = 30, 30, 25
VP8L_SIGNATURE = 0x2F  # a lossless bitstream's first byte
CHANNEL_MAX = 255  # an 8-bit channel's top value (opaque, for alpha)
RGB_CHANNELS = 3  # an RGB pixel's channels (r, g, b)

type Box = tuple[int, int, int, int]  # (x0, y0, x1, y1) in px, end-exclusive


def _webp_kind(data: bytes) -> str:
    """Classify a RIFF/WEBP stream by walking its chunks (VP8X wraps the real one)."""
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if fourcc == b"VP8 ":
            return WEBP_LOSSY
        if fourcc == b"VP8L":
            return WEBP_LOSSLESS
        if fourcc == b"ANIM":
            return WEBP_ANIMATED
        pos += 8 + size + (size & 1)
    return UNKNOWN


def _avif_brand(data: bytes) -> bool:
    if data[4:8] != b"ftyp":
        return False
    box = struct.unpack(">I", data[:4])[0]
    brands = {data[8:12]} | {data[i : i + 4] for i in range(16, min(box, len(data)), 4)}
    return bool(brands & {b"avif", b"avis"})


# (leading bytes, format), read in order; WebP and AVIF are told by more than their first bytes (detect)
_SIGNATURES = (
    (b"\xff\xd8\xff", JPEG),
    (b"\x89PNG\r\n\x1a\n", PNG),
    (b"GIF8", GIF),
    (b"BM", BMP),
    (b"\xff\x0a", JXL),  # JPEG XL codestream
    (b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a", JXL),  # JPEG XL in its ISO-BMFF container
)


def detect(data: bytes) -> str:
    """Return the format constant for image bytes (needs the whole file for VP8X WebP).

    The first _SIGNATURES entry the bytes start with, else a RIFF/WEBP stream's kind, else AVIF by its brand, else
    UNKNOWN.
    """
    fmt = next((fmt for prefix, fmt in _SIGNATURES if data[: len(prefix)] == prefix), None)
    if fmt is not None:
        return fmt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_kind(data)
    if _avif_brand(data):
        return AVIF
    return UNKNOWN


def dimensions(head: bytes) -> tuple[int, int] | None:
    """Return (width, height) from the first bytes of an image, without decoding.

    WebP needs this because Pillow decodes the whole stream to learn the size. Returns None if the header isn't
    understood: always for an AVIF cut short, whose size Pillow reads only from the whole file.
    """
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        chunk = head[12:16]
        if chunk == b"VP8X" and len(head) >= VP8X_HEAD:
            return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
        if chunk == b"VP8 " and len(head) >= VP8_HEAD and head[23:26] == b"\x9d\x01\x2a":
            return struct.unpack("<H", head[26:28])[0] & 0x3FFF, struct.unpack("<H", head[28:30])[0] & 0x3FFF
        if chunk == b"VP8L" and len(head) >= VP8L_HEAD and head[20] == VP8L_SIGNATURE:
            bits = int.from_bytes(head[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return None
    try:
        return Image.open(io.BytesIO(head)).size
    except Exception:  # Pillow's format plugins raise many types on a partial header: size unknown
        return None


def file_dimensions(src: str | os.PathLike[str] | IO[bytes]) -> tuple[int, int] | None:
    """dimensions() from a file path or an open binary file, reading only its first HEADER_BYTES."""
    if not isinstance(src, (str, os.PathLike)):
        return dimensions(src.read(HEADER_BYTES))
    with Path(src).open("rb") as f:
        return dimensions(f.read(HEADER_BYTES))


def page_size(path: str | Path) -> tuple[int, int]:
    """(width, height) of a page file: from its header, else by decoding it."""
    return file_dimensions(path) or open_image(path)[0].size


def exif_orientation(data: bytes) -> int:
    """Return a page's EXIF orientation from its header (1: none, or upright; XMP tiff:Orientation counts too).

    Image.getexif of the base class reads only what the header holds: PngImageFile.getexif would decode the whole page
    to look for an eXIf chunk after the pixels.
    """
    with Image.open(io.BytesIO(data)) as im:
        return Image.Image.getexif(im).get(ExifTags.Base.Orientation, 1)


def is_srgb(icc: bytes | None) -> bool:
    """Return whether icc is no profile or an sRGB profile (description read properly, incl. UTF-16 ICC v4 text)."""
    if not icc:
        return True
    try:
        return "srgb" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).lower()
    except Exception:  # a profile littlecms cannot parse (any error type) falls back to the byte test
        return b"sRGB" in icc


def open_image(src: bytes | str | Path) -> tuple[Image.Image, bool]:
    """Decode a page fully into an 8-bit image Pillow converts correctly, and close the file.

    - 16-bit gray (I;16) / 32-bit (I) are scaled to 8 bits; Pillow's own convert("L") clips them to white.
    - Palette images keep transparency (tRNS) as RGBA.
    - Pixels in a non-sRGB ICC profile are converted to sRGB, so everything downstream can drop the profile. A profile
      applies only to pixels of its own colour space: CMYK pixels in a CMYK profile go straight to sRGB; a profile of
      another space (Azumi's colour JPEGs carry a GRAY "Dot Gain 20%" profile from Ch.100 on) describes nothing here
      and is ignored (the pixels are taken as sRGB).
    Returns (image, has_alpha).
    """
    with Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src) as im:
        im.load()
        return from_loaded(im)


def from_loaded(im: Image.Image) -> tuple[Image.Image, bool]:
    """Return open_image's result for a page Pillow has opened and loaded already.

    A check that decoded it reuses that decode; im is left as it is. Returns (image, has_alpha).
    """
    icc = im.info.get("icc_profile")
    profile = None if not icc or is_srgb(icc) else ImageCms.core.profile_frombytes(icc)
    space = profile.xcolor_space.strip() if profile else None
    if im.mode == "CMYK" and profile is not None and space == "CMYK":
        im = _to_srgb(im, profile)
    elif im.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        a = np.asarray(im).astype(np.uint32)
        shift = 8 if a.max() > CHANNEL_MAX else 0
        im = Image.fromarray((a >> shift).astype(np.uint8), "L")
    elif im.mode in ("P", "PA"):
        im = im.convert("RGBA" if im.has_transparency_data else "RGB")
    elif im.mode not in ("L", "LA", "RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() or im.has_transparency_data else "RGB")
    else:
        im = im.copy()
    if profile is not None and space == "RGB" and im.mode in ("RGB", "RGBA"):
        alpha = im.getchannel("A") if im.mode == "RGBA" else None
        im = _to_srgb(im.convert("RGB"), profile)
        if alpha is not None:
            im.putalpha(alpha)
    has_alpha = "A" in im.getbands() and np.asarray(im.getchannel("A")).min() < CHANNEL_MAX
    if "A" in im.getbands() and not has_alpha:  # fully opaque alpha carries nothing
        im = im.convert("L" if im.mode == "LA" else "RGB")
    return im, has_alpha


def _to_srgb(im: Image.Image, profile: CmsProfile) -> Image.Image:
    """Convert im's pixels from profile to sRGB, as a new RGB image."""
    out = ImageCms.profileToProfile(im, profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
    if out is None:  # only an in-place conversion returns None
        msg = "ImageCms.profileToProfile returned no image"
        raise RuntimeError(msg)
    return out


def open_flat(src: bytes | str | Path) -> tuple[Image.Image, bool]:
    """Decode a page as open_image does, with real alpha (any pixel under 255) composited onto white.

    White is the canvas rendered pages are built on: the page as a rendered page shows it, and as the plan measures it.
    An opaque-alpha or alpha-free page comes back exactly as open_image returns it. Returns (image, had real alpha).
    """
    im, alpha = open_image(src)
    return flatten(im, alpha=alpha)


def flatten(im: Image.Image, *, alpha: bool) -> tuple[Image.Image, bool]:
    """open_flat of open_image's (image, has_alpha). Returns (image, had real alpha)."""
    if alpha:
        im = Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im.convert("RGBA")).convert("RGB")
    return im, alpha


def to_gray(im: Image.Image, *, alpha: bool) -> Image.Image:
    """Return an open_image page as true gray (L, or LA with alpha) for a gray encode.

    A colour profile the page carried is dropped: it can't describe gray pixels, and libpng readers (libjxl's
    ssimulacra2, the scorer before Vship) refuse a gray PNG holding one (Gunslinger Girl ch38-56, 83-86: sRGB-profiled
    colour JPEGs classed gray).
    """
    g = im.convert("LA" if alpha else "L")
    icc = g.info.get("icc_profile")
    if icc and ImageCms.core.profile_frombytes(icc).xcolor_space.strip() != "GRAY":
        del g.info["icc_profile"]
    return g


def save_png(im: Image.Image, path: Path) -> None:
    """Fast PNG (lossless intermediate for the encoders)."""
    im.save(path, "PNG", compress_level=1)


def as_box(values: Sequence[int]) -> Box:
    """Return a box given as 4 values ([x0, y0, x1, y1], as JSON holds it) as a Box; ValueError for another count."""
    x0, y0, x1, y1 = values
    return x0, y0, x1, y1


GRAY, TINTED, COLOUR, GRAY_MARK = "gray", "tinted", "colour", "gray-mark"  # page classes (SPEC "Page classes")
MARK_DILATION = 3  # tiles: coloured tiles this close (Manhattan) belong to one mark
MARK_BLOCK = 8  # px: side of the colour tiles (classify, _mark_box)


def _spread(rgb: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Return the channel spread (max-min of R,G,B) of each pixel, computed channel by channel.

    numpy's reduction over the 3-value axis is ~12x slower (it was most of the plan's page scan).
    """
    a = np.asarray(rgb)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)


def _tile_spread(spread: NDArray[np.uint8], block: int) -> tuple[NDArray[np.float64] | None, float]:
    """Return (the mean channel spread (_spread) of each block x block tile, the colour strength).

    The strength is the strongest tile's spread; the tiles are None when the page is smaller than a tile, and then the
    page's own max spread is the strength.
    """
    h, w = (spread.shape[0] // block) * block, (spread.shape[1] // block) * block
    if not h or not w:
        return None, float(spread.max())
    tiles = spread[:h, :w].reshape(h // block, block, w // block, block).mean(axis=(1, 3))
    return tiles, float(tiles.max())


def _components(mask: NDArray[np.bool_]) -> list[Box]:
    """4-connected components of a boolean grid, each as (row0, col0, row1, col1) with exclusive ends."""
    seen = np.zeros_like(mask)
    boxes: list[Box] = []
    for r, c in zip(*np.nonzero(mask), strict=True):
        if seen[r, c]:
            continue
        seen[r, c] = True
        stack, r0, c0, r1, c1 = [(r, c)], r, c, r + 1, c + 1
        while stack:
            y, x = stack.pop()
            r0, c0, r1, c1 = min(r0, y), min(c0, x), max(r1, y + 1), max(c1, x + 1)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        boxes.append((int(r0), int(c0), int(r1), int(c1)))
    return boxes


def _mark_boxes(mask: NDArray[np.bool_], block: int) -> list[Box]:
    """Return the [(x0, y0, x1, y1)] pixel box of each coloured component.

    Components join after MARK_DILATION tiles of dilation, and each box is the dilated component's (the extent the
    gray-mark size caps were calibrated on).
    """
    grown = mask
    for _ in range(MARK_DILATION):
        n = grown.copy()
        n[1:] |= grown[:-1]
        n[:-1] |= grown[1:]
        n[:, 1:] |= grown[:, :-1]
        n[:, :-1] |= grown[:, 1:]
        grown = n
    return [(c0 * block, r0 * block, c1 * block, r1 * block) for r0, c0, r1, c1 in _components(grown)]


def _mark_box(
    mask: NDArray[np.bool_] | None, shape: tuple[int, ...], limits: Mapping[str, float], block: int
) -> Box | None:
    """Return the pixel box (x0, y0, x1, y1) of the page's one compact colour mark (a scanlation badge).

    mask: the page's coloured-tile grid (tile spread above colour_block; None for a page smaller than a tile);
    shape: its (height, width). None when the page is not gray-mark: exactly 1 component, under
    limits["mark_max_frac"] of the page's tiles, box within mark_max_w x mark_max_h of the page and at least
    mark_min_area px2 (a lone faint speck is not a badge; it keeps the page colour). The one mark finder:
    junk.marks.measure measures the logo inside this box. limits: the config's integrity section, the caller's own
    (never config.cached()).
    """
    cfg = limits
    if mask is None or not mask.any() or mask.mean() >= cfg["mark_max_frac"]:
        return None
    boxes = _mark_boxes(mask, block)
    h, w = shape
    x0, y0, x1, y1 = boxes[0]
    if (
        len(boxes) == 1
        and x1 - x0 <= cfg["mark_max_w"] * w
        and y1 - y0 <= cfg["mark_max_h"] * h
        and (x1 - x0) * (y1 - y0) >= cfg["mark_min_area"]
    ):
        return boxes[0]
    return None


def classify(
    rgb: NDArray[np.uint8], colour_block: float, tint_p99: float, limits: Mapping[str, float]
) -> tuple[str, Box | None]:
    """Return (page class, mark box) of a page.

    gray-mark with its mark's box (_mark_box); colour: any other coloured patch (colour strength, the strongest tile
    mean channel spread, above colour_block); tinted: no patch but a faint overall cast (sepia, yellowed scan, p99
    spread above tint_p99); gray: none of these. The box is None for every class but gray-mark. limits: the config's
    integrity section (mark size limits).
    """
    spread = _spread(rgb)  # once: colour strength, the mark box and the tint test share it
    tiles, strength = _tile_spread(spread, MARK_BLOCK)
    if strength > colour_block:
        box = _mark_box(None if tiles is None else tiles > colour_block, spread.shape, limits, MARK_BLOCK)
        return (COLOUR, None) if box is None else (GRAY_MARK, box)
    return (TINTED if np.percentile(spread[::2, ::2], 99) > tint_p99 else GRAY), None


def page_class(rgb: NDArray[np.uint8], colour_block: float, tint_p99: float, limits: Mapping[str, float]) -> str:
    """Return the page class of classify (gray | tinted | colour | gray-mark)."""
    return classify(rgb, colour_block, tint_p99, limits)[0]


# ---------------------------------------------------------------- junk signature (bulk converter's optimizer/junk.py)
# Frozen: the stored junk decisions (19,500 rows) were computed with these rules on Pillow + imagehash.
PHASH_THRESHOLD = 6  # live rule: largest tile pHash Hamming distance (restitch.phash_threshold)
ASPECT_TOLERANCE = 0.03  # live rule: aspect difference relative to the larger aspect (restitch.aspect_tolerance)
# Near rule (junk_near: a single strip edge block near an approved row is a review candidate, never removed): d12
# took story panels, so 8; the live rule's 3 % kept re-uploaded credit variants (4-9 % taller) out, so 10 %.
NEAR_THRESHOLD = 8
NEAR_ASPECT_TOLERANCE = 0.10
UNIFORM_TOLERANCE = 10  # a page whose every channel's range is at most this is uniform: no signature

type Signature = tuple[tuple[int, ...], float]  # (tile pHashes from top to bottom, aspect h / w)


def signature(im: Image.Image) -> Signature:
    """(tiles, aspect): one pHash per roughly square tile from top to bottom."""
    im = im.convert("RGB")
    w, h = im.size
    n = max(1, round(h / w))
    tile = -(-h // n)
    tiles = tuple(int(str(imagehash.phash(im.crop((0, t, w, min(h, t + tile))))), 16) for t in range(0, h, tile))
    return tiles, h / w


def matches(a: Signature, b: Signature, threshold: int = PHASH_THRESHOLD, aspect_tol: float = ASPECT_TOLERANCE) -> bool:
    """Return whether 2 signatures match: as many tiles, aspects within aspect_tol, each tile within threshold bits."""
    (ta, aa), (tb, ab) = a, b
    return (
        len(ta) == len(tb)
        and abs(aa - ab) <= aspect_tol * max(aa, ab)
        and all((x ^ y).bit_count() <= threshold for x, y in zip(ta, tb, strict=True))
    )


def is_uniform(im: Image.Image, tolerance: int = UNIFORM_TOLERANCE) -> bool:
    """Return whether every RGB channel of im spans at most tolerance levels."""
    lo_hi = im.convert("RGB").getextrema()
    if len(lo_hi) != RGB_CHANNELS:  # Pillow types a 1-band image's (min, max) in the same union
        msg = f"getextrema of an RGB image gave {lo_hi!r}, not 1 (min, max) per channel"
        raise TypeError(msg)
    return all(hi - lo <= tolerance for lo, hi in lo_hi)


@functools.cache
def uniform_tiles() -> frozenset[int]:
    """Return the tile hashes of uniform images (the pHash of every gray level)."""
    return frozenset(signature(Image.new("L", (64, 64), v))[0][0] for v in range(256))


def unsignable(sig: Signature) -> bool:
    """Return whether every tile of a signature is a uniform image's: it would match any blank or flat rows."""
    return set(sig[0]) <= uniform_tiles()


def _tiles_str(tiles: Sequence[int]) -> str:
    """Tile hashes as text (database, review JSON, sidecars): 16 hex digits each, ':'-separated."""
    return ":".join(f"{t:016x}" for t in tiles)


def _tiles(text: str) -> tuple[int, ...]:
    return tuple(int(t, 16) for t in text.split(":"))


def sig_to_str(sig: Signature) -> str:
    """Signature as text for JSON (sidecars, review files): 'tiles:tiles|aspect'."""
    tiles, aspect = sig
    return _tiles_str(tiles) + f"|{aspect!r}"


def sig_from_str(s: str) -> Signature:
    """Return the signature of its text form (sig_to_str's 'tiles:tiles|aspect')."""
    tiles, aspect = s.split("|")
    return _tiles(tiles), float(aspect)


# ---------------------------------------------------------------- page pHash (bulk converter's optimizer/integrity.py)
BLANK_STD = 3.0  # luma std below this on a 64x64 thumbnail = blank page
THUMB = (64, 64)  # the luma thumbnail phash and the blank test read
_N = np.arange(32)
_DCT = np.cos(np.pi * np.outer(_N, 2 * _N + 1) / 64)  # DCT-II basis, 32 points (scale irrelevant to the median test)
_BITS = 1 << np.arange(63, -1, -1, dtype=np.uint64)


def phash(thumb: NDArray[np.uint8]) -> int:
    """Return the pHash of a 64x64 luma thumbnail, as a 64-bit int.

    imagehash layout: 32x32 luma, 2-D DCT, 8x8 low band incl. DC > its median. Reuses the page's thumbnail, so no second
    decode.
    """
    small = thumb.astype(np.float64).reshape(32, 2, 32, 2).mean(axis=(1, 3))
    low = (_DCT @ small @ _DCT.T)[:8, :8]
    return int(_BITS[(low > np.median(low)).ravel()].sum())


# ---------------------------------------------------------------- chroma (bulk converter's optimizer/image.py)
JPEG_CHROMA = {0: "444", 1: "422", 2: "420"}  # Pillow's get_sampling values; -1 is any other sampling (4:4:0, 4:1:1)
GRAY_JPEG_MODE = "L"  # single component: no chroma, get_sampling returns -1 for it


def _jpeg_sampling(data: bytes) -> tuple[str, int]:
    """(mode, Pillow sampling) of a JPEG header."""
    with Image.open(io.BytesIO(data)) as im:
        return im.mode, JpegImagePlugin.get_sampling(im)


def chroma(data: bytes, fmt: str) -> str:
    """Return a source image's chroma resolution as avifenc --yuv.

    A JPEG's stored subsampling (444 / 422 / 420; an unrecognised one counts as 444, never below the source), full
    chroma (444) for PNG, lossless WebP, GIF and BMP, 420 for lossy WebP and anything else. A single-component
    (grayscale) JPEG has no chroma: 400. Header only.
    """
    if fmt == JPEG:
        mode, sampling = _jpeg_sampling(data)
        return "400" if mode == GRAY_JPEG_MODE else JPEG_CHROMA.get(sampling, "444")
    return "444" if fmt in (PNG, WEBP_LOSSLESS, GIF, BMP) else "420"


# ---------------------------------------------------------------- page measurement
COLOUR_BLOCK = 22  # integrity.colour_block_spread: B&W compression noise <= 19.7, faintest real colour 25
TINT_P99 = 6  # integrity.tint_p99_spread: no colour tile but p99 spread above this -> tinted
MARK_LIMITS: dict[str, float] = {  # integrity's gray-mark limits (classify's `limits`)
    "mark_max_frac": 0.01,  # coloured tiles / page tiles below this (pilot marks 0.13-0.45%, real colour from 2.56%)
    "mark_max_w": 0.35,  # mark box width / page width at most (pilot max 28.7%)
    "mark_max_h": 0.08,  # mark box height / page height at most (pilot max 6.0%)
    "mark_min_area": 5000,  # mark box area (px2, dilated box) at least: badges >= 5376, faint specks 2048-4800
}
PAGE_FIELDS = (
    "index",
    "name",
    "bytes",
    "sha1",
    "width",
    "height",
    "format",
    "blank",
    "phash",
    "junk_signature",
    "colour_class",
    "chroma",
)
DECODED_FIELDS = ("width", "height", "blank", "phash", "junk_signature", "colour_class")  # need the page decoded


class Decoded(TypedDict):
    """The DECODED_FIELDS of a page: width and height in px, blank, pHash (16 hex digits), junk signature text, class.

    phash is None for a blank page, junk_signature (sig_to_str) None for a uniform one; colour_class a page class.
    """

    width: int
    height: int
    blank: bool
    phash: str | None
    junk_signature: str | None
    colour_class: str


class MeasuredPage(Decoded):
    """A page's PAGE_FIELDS: its index in reading order, member name, size in bytes, sha1, format, avifenc chroma.

    With the Decoded fields; format a format constant (detect), chroma an avifenc --yuv value (chroma).
    """

    index: int
    name: str
    bytes: int
    sha1: str
    format: str
    chroma: str


class KnownPage(TypedDict, total=False):
    """The fields of a page measured before, any of PAGE_FIELDS (read only: a MeasuredPage is one)."""

    index: ReadOnly[int]
    name: ReadOnly[str]
    bytes: ReadOnly[int]
    sha1: ReadOnly[str]
    width: ReadOnly[int]
    height: ReadOnly[int]
    format: ReadOnly[str]
    blank: ReadOnly[bool]
    phash: ReadOnly[str | None]
    junk_signature: ReadOnly[str | None]
    colour_class: ReadOnly[str]
    chroma: ReadOnly[str]


def _measure_decoded(data: bytes) -> Decoded:
    """Return the DECODED_FIELDS of a page from one decode.

    width, height, blank and phash from the stored pixels' 64x64 luma thumbnail (as the bulk converter's integrity
    check; phash None for a blank page), junk_signature and colour_class from the page composited onto white (open_flat,
    as the bulk converter's page scan; junk_signature None for a uniform page).
    """
    with Image.open(io.BytesIO(data)) as raw:
        raw.load()
        thumb = np.asarray(raw.convert("L").resize(THUMB))
        loaded, alpha = from_loaded(raw)
        rgb = flatten(loaded, alpha=alpha)[0].convert("RGB")
    blank = bool(thumb.std() < BLANK_STD)
    return {
        "width": rgb.width,
        "height": rgb.height,
        "blank": blank,
        "phash": None if blank else f"{phash(thumb):016x}",
        "junk_signature": None if is_uniform(rgb) else sig_to_str(signature(rgb)),
        "colour_class": classify(np.asarray(rgb), COLOUR_BLOCK, TINT_P99, MARK_LIMITS)[0],
    }


def _known_decoded(page: KnownPage, data: bytes, decode: Callable[[bytes], Decoded]) -> Decoded:
    """Return a page's DECODED_FIELDS: its known ones, the others from 1 decode(data) (none when every one is known)."""
    fresh = functools.cache(functools.partial(decode, data))
    return {
        "width": page["width"] if "width" in page else fresh()["width"],
        "height": page["height"] if "height" in page else fresh()["height"],
        "blank": page["blank"] if "blank" in page else fresh()["blank"],
        "phash": page["phash"] if "phash" in page else fresh()["phash"],
        "junk_signature": page["junk_signature"] if "junk_signature" in page else fresh()["junk_signature"],
        "colour_class": page["colour_class"] if "colour_class" in page else fresh()["colour_class"],
    }


def measure_pages(
    cbz: Path, known: Mapping[int, KnownPage] | None = None, *, decode: Callable[[bytes], Decoded] = _measure_decoded
) -> list[MeasuredPage]:
    """Return [{PAGE_FIELDS}] of a CBZ's pages in archive.archive_pages order: the one page measurement.

    known: {index: dict of fields measured before}; a page keeps its known fields and gets only the missing ones, and is
    decoded only when a DECODED_FIELDS field is missing (decode(page bytes): its DECODED_FIELDS). A known sha1 that is
    not the page's raises ValueError (stale measurements). Pure: writes nothing.
    """
    known = known or {}
    out: list[MeasuredPage] = []
    for index, (name, data) in enumerate(archive.pages(cbz)):
        page = known.get(index, {})
        digest = archive.sha1(data)
        if page.get("sha1", digest) != digest:
            msg = f"{cbz}: page {index} ({name}) known sha1 {page.get('sha1')} is not its bytes' {digest}"
            raise ValueError(msg)
        try:
            fmt = page["format"] if "format" in page else detect(data)
            colour = page["chroma"] if "chroma" in page else chroma(data, fmt)
            d = _known_decoded(page, data, decode)
        except Exception as e:  # Pillow raises many types on a page it cannot read
            msg = f"{cbz}: page {index} ({name}) cannot be measured: {e}"
            raise ValueError(msg) from e
        out.append(
            {
                "index": index,
                "name": name,
                "bytes": len(data),
                "sha1": digest,
                "width": d["width"],
                "height": d["height"],
                "format": fmt,
                "blank": d["blank"],
                "phash": d["phash"],
                "junk_signature": d["junk_signature"],
                "colour_class": d["colour_class"],
                "chroma": colour,
            }
        )
    return out


def blank(rgb: NDArray[np.uint8], box: Sequence[int], fill: Sequence[int]) -> NDArray[np.uint8]:
    """Return a copy of the page (RGB uint8 array) with `box` filled with `fill`.

    A logo's blank (SPEC "Junk decisions", the chapter edit's `blank`). Raises ValueError for a box outside the page or
    a fill that is not one RGB colour; the input is never modified.
    """
    out = np.array(rgb, dtype=np.uint8)
    x0, y0, x1, y1 = box
    h, w = out.shape[:2]
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        msg = f"blank box {tuple(box)} is not inside the {w}x{h} page"
        raise ValueError(msg)
    if len(fill) != RGB_CHANNELS or not all(0 <= int(v) <= CHANNEL_MAX for v in fill):
        msg = f"blank fill {fill!r} is not an RGB colour"
        raise ValueError(msg)
    out[y0:y1, x0:x1] = fill
    return out
