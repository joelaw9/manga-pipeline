"""Page images (contracts section 1): format detection from file contents, dimensions, decoding to 8-bit sRGB, and
page classes. Copied from the bulk converter's optimizer/formats.py (its archive half is archive.py); reads no config.
"""

import io
import struct

import imagehash
import numpy as np
from PIL import ExifTags, Image, ImageCms, JpegImagePlugin

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


def _webp_kind(data):
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


def _avif_brand(data):
    if data[4:8] != b"ftyp":
        return False
    box = struct.unpack(">I", data[:4])[0]
    brands = {data[8:12]} | {data[i : i + 4] for i in range(16, min(box, len(data)), 4)}
    return bool(brands & {b"avif", b"avis"})


def detect(data):
    """Return the format constant for image bytes (needs the whole file for VP8X WebP)."""
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return PNG
    if data[:4] in (b"GIF8",):
        return GIF
    if data[:2] == b"BM":
        return BMP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_kind(data)
    if _avif_brand(data):
        return AVIF
    if data[:2] == b"\xff\x0a" or data[:12] == b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a":
        return JXL
    return UNKNOWN


def dimensions(head):
    """(width, height) from the first bytes of an image, without decoding. WebP needs this because
    Pillow decodes the whole stream to learn the size. Returns None if the header isn't understood: always for an
    AVIF cut short, whose size Pillow reads only from the whole file."""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        chunk = head[12:16]
        if chunk == b"VP8X" and len(head) >= 30:
            return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
        if chunk == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
            return struct.unpack("<H", head[26:28])[0] & 0x3FFF, struct.unpack("<H", head[28:30])[0] & 0x3FFF
        if chunk == b"VP8L" and len(head) >= 25 and head[20] == 0x2F:
            bits = int.from_bytes(head[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return None
    try:
        return Image.open(io.BytesIO(head)).size
    except Exception:  # noqa: BLE001 - Pillow's format plugins raise many types on a partial header: size unknown
        return None


def file_dimensions(src):
    """dimensions() from a file path or an open binary file, reading only its first HEADER_BYTES."""
    if hasattr(src, "read"):
        return dimensions(src.read(HEADER_BYTES))
    with open(src, "rb") as f:
        return dimensions(f.read(HEADER_BYTES))


def page_size(path):
    """(width, height) of a page file: from its header, else by decoding it."""
    return file_dimensions(path) or open_image(path)[0].size


def exif_orientation(data):
    """A page's EXIF orientation from its header (1: none, or upright; XMP tiff:Orientation counts too). Image.getexif
    of the base class reads only what the header holds: PngImageFile.getexif would decode the whole page to look for
    an eXIf chunk after the pixels."""
    with Image.open(io.BytesIO(data)) as im:
        return Image.Image.getexif(im).get(ExifTags.Base.Orientation, 1)


def is_srgb(icc):
    """True for no profile or an sRGB profile (description read properly, incl. UTF-16 ICC v4 text)."""
    if not icc:
        return True
    try:
        return "srgb" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).lower()
    except Exception:  # noqa: BLE001 - a profile littlecms cannot parse (any error type) falls back to the byte test
        return b"sRGB" in icc


def open_image(src):
    """Decode a page fully into an 8-bit image Pillow converts correctly, and close the file.
    - 16-bit gray (I;16) / 32-bit (I) are scaled to 8 bits; Pillow's own convert("L") clips them to white.
    - Palette images keep transparency (tRNS) as RGBA.
    - Pixels in a non-sRGB ICC profile are converted to sRGB, so everything downstream can drop the profile. A profile
      applies only to pixels of its own colour space: CMYK pixels in a CMYK profile go straight to sRGB; a profile of
      another space (Azumi's colour JPEGs carry a GRAY "Dot Gain 20%" profile from Ch.100 on) describes nothing here
      and is ignored (the pixels are taken as sRGB).
    Returns (image, has_alpha)."""
    with Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src) as im:
        im.load()
        return from_loaded(im)


def from_loaded(im):
    """open_image of a page Pillow has opened and loaded already (a check that decoded it reuses that decode); im is
    left as it is. Returns (image, has_alpha)."""
    icc = im.info.get("icc_profile")
    profile = None if is_srgb(icc) else ImageCms.ImageCmsProfile(io.BytesIO(icc))
    space = profile.profile.xcolor_space.strip() if profile else None
    if im.mode == "CMYK" and space == "CMYK":
        im = ImageCms.profileToProfile(im, profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
    elif im.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        a = np.asarray(im).astype(np.uint32)
        shift = 8 if a.max() > 255 else 0
        im = Image.fromarray((a >> shift).astype(np.uint8), "L")
    elif im.mode in ("P", "PA"):
        im = im.convert("RGBA" if im.has_transparency_data else "RGB")
    elif im.mode not in ("L", "LA", "RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() or im.has_transparency_data else "RGB")
    else:
        im = im.copy()
    if space == "RGB" and im.mode in ("RGB", "RGBA"):
        alpha = im.getchannel("A") if im.mode == "RGBA" else None
        im = ImageCms.profileToProfile(im.convert("RGB"), profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
        if alpha is not None:
            im.putalpha(alpha)
    has_alpha = "A" in im.getbands() and im.getchannel("A").getextrema()[0] < 255
    if "A" in im.getbands() and not has_alpha:  # fully opaque alpha carries nothing
        im = im.convert("L" if im.mode == "LA" else "RGB")
    return im, has_alpha


ALPHA_WHITE = 1  # version of open_flat's compositing: page scans record it per page, the strip slots and the digest
# of a rendered chapter carry it only where a page has real alpha (SPEC cache keys)


def open_flat(src):
    """open_image with real alpha (any pixel under 255) composited onto white, the canvas rendered pages are built on:
    the page as a rendered page shows it, and as the plan measures it. An opaque-alpha or alpha-free page comes back
    exactly as open_image returns it. Returns (image, had real alpha)."""
    return flatten(*open_image(src))


def flatten(im, alpha):
    """open_flat of open_image's (image, has_alpha). Returns (image, had real alpha)."""
    if alpha:
        im = Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im.convert("RGBA")).convert("RGB")
    return im, alpha


def may_have_alpha(path):
    """True when a page's header allows transparency (an alpha band, or a transparency entry: PNG / GIF tRNS); a page
    for which this is False has no real alpha, known without a decode."""
    with Image.open(path) as im:
        return "A" in im.getbands() or "transparency" in im.info


def to_gray(im, alpha):
    """An open_image page as true gray (L, or LA with alpha) for a gray encode. A colour profile the page carried is
    dropped: it can't describe gray pixels, and libpng readers (libjxl's ssimulacra2, the scorer before Vship) refuse a
    gray PNG holding one (Gunslinger
    Girl ch38-56, 83-86: sRGB-profiled colour JPEGs classed gray)."""
    g = im.convert("LA" if alpha else "L")
    icc = g.info.get("icc_profile")
    if icc and ImageCms.ImageCmsProfile(io.BytesIO(icc)).profile.xcolor_space.strip() != "GRAY":
        del g.info["icc_profile"]
    return g


def save_png(im, path):
    """Fast PNG (lossless intermediate for the encoders)."""
    im.save(path, "PNG", compress_level=1)


GRAY, TINTED, COLOUR, GRAY_MARK = "gray", "tinted", "colour", "gray-mark"  # page classes (SPEC "Page classes")
MARK_DILATION = 3  # tiles: coloured tiles this close (Manhattan) belong to one mark
MARK_BLOCK = 8  # px: side of the colour tiles (classify, gray_mark_box)


def _spread(rgb):
    """Channel spread (max-min of R,G,B) of each pixel, channel by channel: numpy's reduction over the 3-value axis
    is ~12x slower (it was most of the plan's page scan)."""
    a = np.asarray(rgb)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)


def _tile_spread(spread, block):
    """Mean channel spread (_spread) of each block x block tile; None when the page is smaller than a tile (then the
    page's own max spread is the strength)."""
    h, w = (spread.shape[0] // block) * block, (spread.shape[1] // block) * block
    if not h or not w:
        return None, float(spread.max())
    return spread[:h, :w].reshape(h // block, block, w // block, block).mean(axis=(1, 3)), None


def _components(mask):
    """4-connected components of a boolean grid, each as (row0, col0, row1, col1) with exclusive ends."""
    seen = np.zeros_like(mask)
    boxes = []
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


def _colour_mask(rgb, colour_block, block):
    """Boolean grid of coloured tiles (tile spread above colour_block); None for a page smaller than a tile."""
    tiles, _ = _tile_spread(_spread(rgb), block)
    return None if tiles is None else tiles > colour_block


def _mark_boxes(mask, block):
    """[(x0, y0, x1, y1)] pixel box of each coloured component. Components join after MARK_DILATION tiles of
    dilation, and each box is the dilated component's (the extent the gray-mark size caps were calibrated on)."""
    grown = mask
    for _ in range(MARK_DILATION):
        n = grown.copy()
        n[1:] |= grown[:-1]
        n[:-1] |= grown[1:]
        n[:, 1:] |= grown[:, :-1]
        n[:, :-1] |= grown[:, 1:]
        grown = n
    return [(c0 * block, r0 * block, c1 * block, r1 * block) for r0, c0, r1, c1 in _components(grown)]


def gray_mark_box(rgb, colour_block, limits, block=MARK_BLOCK):
    """The pixel box (x0, y0, x1, y1) of the page's one compact colour mark (a scanlation badge), or None when the
    page is not gray-mark: exactly 1 component, under limits["mark_max_frac"] of the page's tiles, box within
    mark_max_w x mark_max_h of the page and at least mark_min_area px2 (a lone faint speck is not a badge; it keeps the
    page colour). The one mark finder: Part C (optimizer.marks.measure) measures the logo inside this box. limits: the
    config's integrity section, the caller's own (never config.cached())."""
    return _mark_box(_colour_mask(rgb, colour_block, block), np.asarray(rgb).shape[:2], limits, block)


def _mark_box(mask, shape, limits, block):
    """gray_mark_box from the page's coloured-tile mask (_colour_mask) and its (height, width)."""
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


def classify(rgb, colour_block, tint_p99, limits):
    """(page class, mark box): gray-mark with its mark's box (gray_mark_box); colour: any other coloured patch
    (colour strength, the strongest tile mean channel spread, above colour_block); tinted: no patch but a faint
    overall cast (sepia, yellowed scan, p99 spread above tint_p99); gray: none of these. The box is None for every
    class but gray-mark. limits: the config's integrity section (mark size limits)."""
    spread = _spread(rgb)  # once: colour strength, gray_mark_box and the tint test share it
    tiles, whole = _tile_spread(spread, MARK_BLOCK)
    if (whole if tiles is None else float(tiles.max())) > colour_block:
        box = _mark_box(None if tiles is None else tiles > colour_block, spread.shape, limits, MARK_BLOCK)
        return (COLOUR, None) if box is None else (GRAY_MARK, box)
    return (TINTED if np.percentile(spread[::2, ::2], 99) > tint_p99 else GRAY), None


def page_class(rgb, colour_block, tint_p99, limits):
    """The page class of classify (gray | tinted | colour | gray-mark)."""
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


def signature(im):
    """(tiles, aspect): one pHash per roughly square tile from top to bottom."""
    im = im.convert("RGB")
    w, h = im.size
    n = max(1, round(h / w))
    tile = -(-h // n)
    tiles = tuple(int(str(imagehash.phash(im.crop((0, t, w, min(h, t + tile))))), 16) for t in range(0, h, tile))
    return tiles, h / w


def matches(a, b, threshold=PHASH_THRESHOLD, aspect_tol=ASPECT_TOLERANCE):
    (ta, aa), (tb, ab) = a, b
    return (
        len(ta) == len(tb)
        and abs(aa - ab) <= aspect_tol * max(aa, ab)
        and all((x ^ y).bit_count() <= threshold for x, y in zip(ta, tb, strict=True))
    )


def is_uniform(im, tolerance=UNIFORM_TOLERANCE):
    lo_hi = im.convert("RGB").getextrema()
    return all(hi - lo <= tolerance for lo, hi in lo_hi)


def _tiles_str(tiles):
    """Tile hashes as text (database, review JSON, sidecars): 16 hex digits each, ':'-separated."""
    return ":".join(f"{t:016x}" for t in tiles)


def _tiles(text):
    return tuple(int(t, 16) for t in text.split(":"))


def sig_to_str(sig):
    """Signature as text for JSON (sidecars, review files): 'tiles:tiles|aspect'."""
    tiles, aspect = sig
    return _tiles_str(tiles) + f"|{aspect!r}"


def sig_from_str(s):
    tiles, aspect = s.split("|")
    return _tiles(tiles), float(aspect)


# ---------------------------------------------------------------- page pHash (bulk converter's optimizer/integrity.py)
BLANK_STD = 3.0  # luma std below this on a 64x64 thumbnail = blank page
THUMB = (64, 64)  # the luma thumbnail phash and the blank test read
_N = np.arange(32)
_DCT = np.cos(np.pi * np.outer(_N, 2 * _N + 1) / 64)  # DCT-II basis, 32 points (scale irrelevant to the median test)
_BITS = 1 << np.arange(63, -1, -1, dtype=np.uint64)


def phash(thumb):
    """pHash (imagehash layout: 32x32 luma, 2-D DCT, 8x8 low band incl. DC > its median) of a 64x64 luma thumbnail,
    as a 64-bit int. Reuses the page's thumbnail, so no second decode."""
    small = thumb.astype(np.float64).reshape(32, 2, 32, 2).mean(axis=(1, 3))
    low = (_DCT @ small @ _DCT.T)[:8, :8]
    return int(_BITS[(low > np.median(low)).ravel()].sum())


# ---------------------------------------------------------------- chroma (bulk converter's optimizer/image.py)
JPEG_CHROMA = {0: "444", 1: "422", 2: "420"}  # Pillow's get_sampling values; -1 is any other sampling (4:4:0, 4:1:1)
GRAY_JPEG_MODE = "L"  # single component: no chroma, get_sampling returns -1 for it


def _jpeg_sampling(data):
    """(mode, Pillow sampling) of a JPEG header."""
    with Image.open(io.BytesIO(data)) as im:
        return im.mode, JpegImagePlugin.get_sampling(im)


def chroma(data, fmt):
    """A source image's chroma resolution as avifenc --yuv: a JPEG's stored subsampling (444 / 422 / 420; an
    unrecognised one counts as 444, never below the source), full chroma (444) for PNG, lossless WebP, GIF and BMP,
    420 for lossy WebP and anything else. A single-component (grayscale) JPEG has no chroma: 400. Header only."""
    if fmt == JPEG:
        mode, sampling = _jpeg_sampling(data)
        return "400" if mode == GRAY_JPEG_MODE else JPEG_CHROMA.get(sampling, "444")
    return "444" if fmt in (PNG, WEBP_LOSSLESS, GIF, BMP) else "420"


# ---------------------------------------------------------------- page measurement
COLOUR_BLOCK = 22  # integrity.colour_block_spread: B&W compression noise <= 19.7, faintest real colour 25
TINT_P99 = 6  # integrity.tint_p99_spread: no colour tile but p99 spread above this -> tinted
MARK_LIMITS = {  # integrity's gray-mark limits (classify's `limits`)
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


def _measure_decoded(data):
    """The DECODED_FIELDS of a page from one decode: width, height, blank and phash from the stored pixels' 64x64 luma
    thumbnail (as the bulk converter's integrity check; phash None for a blank page), junk_signature and colour_class
    from the page composited onto white (open_flat, as the bulk converter's page scan; junk_signature None for a
    uniform page)."""
    with Image.open(io.BytesIO(data)) as raw:
        raw.load()
        thumb = np.asarray(raw.convert("L").resize(THUMB))
        rgb = flatten(*from_loaded(raw))[0].convert("RGB")
    blank = bool(thumb.std() < BLANK_STD)
    return {
        "width": rgb.width,
        "height": rgb.height,
        "blank": blank,
        "phash": None if blank else f"{phash(thumb):016x}",
        "junk_signature": None if is_uniform(rgb) else sig_to_str(signature(rgb)),
        "colour_class": classify(np.asarray(rgb), COLOUR_BLOCK, TINT_P99, MARK_LIMITS)[0],
    }


def measure_pages(cbz, known=None):
    """The one page measurement: [{PAGE_FIELDS}] of a CBZ's pages in archive.archive_pages order. known: {index: dict
    of fields measured before}; a page keeps its known fields and gets only the missing ones, and is decoded only when
    a DECODED_FIELDS field is missing. A known sha1 that is not the page's raises ValueError (stale measurements).
    Pure: writes nothing."""
    known = known or {}
    out = []
    for index, (name, data) in enumerate(archive.pages(cbz)):
        page = dict(known.get(index, {}))
        digest = archive.sha1(data)
        if page.get("sha1", digest) != digest:
            raise ValueError(f"{cbz}: page {index} ({name}) known sha1 {page['sha1']} is not its bytes' {digest}")
        page.update(index=index, name=name, bytes=len(data), sha1=digest)
        try:
            page.setdefault("format", detect(data))
            if "chroma" not in page:
                page["chroma"] = chroma(data, page["format"])
            if any(f not in page for f in DECODED_FIELDS):
                page.update({f: v for f, v in _measure_decoded(data).items() if f not in page})
        except Exception as e:  # noqa: BLE001 - Pillow raises many types on a page it cannot read
            raise ValueError(f"{cbz}: page {index} ({name}) cannot be measured: {e}") from e
        out.append({f: page[f] for f in PAGE_FIELDS})
    return out
