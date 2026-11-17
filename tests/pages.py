"""Page, CBZ and ComicInfo builders: synthetic stand-ins for what the library holds (no real pages are copied).

Copied from the bulk converter's tests/fixtures.py; PAGE_FIX holds tiny ICC profiles taken from real pages.
"""

import io
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

PAGE_FIX = Path(__file__).with_name("fixtures_pages")
PILOT = (1200, 1723)  # Akuyaku Kizoku's page size (the gray-mark caps are relative to it)
ZERO_BYTE = b""  # page entry written with no data (seen: 5 chapters on the share)


def zero_filled(n=4096):
    """Return `n` NUL bytes: a page entry full of them."""
    return bytes(n)  # page entry full of NUL bytes (seen: 3 PNG pages on the share)


def webtoon_page(size=(800, 2400), seed=0):
    """Smooth colour artwork (webtoon panel), built from a row and a column (the channels are separable)."""
    x, y = np.arange(size[0])[None, :], np.arange(size[1])[:, None]
    a = np.empty((size[1], size[0], 3), np.uint8)
    a[..., 0] = np.clip(120 + 80 * np.sin(x / 40 + seed) * np.cos(y / 90), 0, 255).astype(np.uint8)
    a[..., 1] = np.clip(140 + 60 * np.cos(x / 70 + seed), 0, 255).astype(np.uint8)
    a[..., 2] = np.clip(160 + 50 * np.sin(y / 50 + seed), 0, 255).astype(np.uint8)
    return Image.fromarray(a)


def manga_page(size=(900, 1300), seed=0, noise=0.0):
    """Draw a B&W manga page: halftone screentone, ink lines, white gutters; optional scan noise.

    RGB-encoded like most scans.
    """
    y, x = np.ogrid[0 : size[1], 0 : size[0]]
    dots = ((x % 6 - 3) ** 2 + (y % 6 - 3) ** 2) < 5
    a = np.where(dots, 30, 245).astype(np.float32)
    a[((x + seed * 37) // 150 + y // 200) % 3 == 0] = 245
    a[(np.abs(x - y * 0.7 - seed) < 3) | (np.abs(x + y * 0.4 - 700) < 2)] = 0
    if noise:
        a += np.random.default_rng(seed).normal(0, noise, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).convert("RGB")


def with_tint(im, tint=(12, 6, 0)):
    """Sepia / yellowed scan: faint cast, no strongly coloured pixel."""
    a = np.asarray(im.convert("RGB")).astype(np.int16) + np.array(tint, np.int16)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def blob(seed, size=(300, 426)):
    """Smooth colour art with a seed-specific layout."""
    small = np.random.default_rng(seed).integers(0, 255, (8, 3, 3)).astype(np.uint8)
    return Image.fromarray(small).resize(size, Image.Resampling.BICUBIC)


def encode(im, fmt="JPEG", **kw):
    """Return im saved in format `fmt` with Pillow's save options `kw`, as bytes."""
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def jpeg(im, quality=92, **kw):
    """Return im as JPEG bytes at `quality`."""
    return encode(im, "JPEG", quality=quality, **kw)


def png(im):
    """Return im as PNG bytes."""
    return encode(im, "PNG")


def webp(im, *, lossless=False, extended=False):
    """Lossy (VP8) or lossless (VP8L) WebP; extended=True adds an ICC profile, forcing the VP8X container."""
    kw: dict[str, object] = {"lossless": True} if lossless else {"quality": 80}
    if extended:
        kw["icc_profile"] = bytes(132)
    return encode(im, "WEBP", **kw)


def cbz(path, pages, comicinfo=None, method=zipfile.ZIP_DEFLATED):
    """Write a chapter archive the way Suwayomi does: flat pages ({name: bytes}), then ComicInfo.xml."""
    path = Path(path)
    with zipfile.ZipFile(path, "w", method) as z:
        for name, data in pages.items():
            z.writestr(name, data)
        if comicinfo is not None:
            z.writestr("ComicInfo.xml", comicinfo)
    return path


def suwayomi_comicinfo(title="Chapter 1", series="Some Series", number="1"):
    """Layout Suwayomi writes: Tachiyomi status tag, then Day/Month/Year (out of schema order)."""
    return f"""<?xml version='1.0' encoding='UTF-8' ?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{title}</Title>
  <Series>{series}</Series>
  <Number>{number}</Number>
  <Summary>A summary.</Summary>
  <Writer>Writer</Writer>
  <Penciller>Artist</Penciller>
  <Genre>Action, Fantasy</Genre>
  <Web>https://example.org/series/chapter/{number}</Web>
  <ty:PublishingStatusTachiyomi xmlns:ty="http://www.w3.org/2001/XMLSchema">Completed</ty:PublishingStatusTachiyomi>
  <Day>23</Day>
  <Month>9</Month>
  <Year>2024</Year>
</ComicInfo>""".encode()


def pipeline_comicinfo(title="Chapter 1", series="Some Series", number="1", volume=None, *, cover=False):
    """Schema-ordered ComicInfo of the kind the metadata stage writes."""
    vol = f"<Volume>{volume}</Volume>" if volume is not None else ""
    pages = '<Pages><Page Image="0" Type="FrontCover"/></Pages>' if cover else ""
    return (
        f'<?xml version="1.0" encoding="utf-8"?><ComicInfo><Title>{title}</Title><Series>{series}</Series>'
        f"<Number>{number}</Number>{vol}<Year>2024</Year><Month>9</Month><Day>23</Day>"
        f"<Translator>Group</Translator><Manga>Yes</Manga>{pages}</ComicInfo>"
    ).encode()


def credit(size=(200, 150)):
    """Draw a scanlator credit block: dark bars on white (identical in every chapter that carries it)."""
    a = np.zeros((size[1], size[0], 3), np.uint8)
    a[:] = np.linspace(140, 240, size[0]).astype(np.uint8)[None, :, None]  # no uniform row inside the block
    for k, y in enumerate(range(15, size[1] - 15, 18)):
        a[y : y + 9, 20 + 10 * (k % 3) : size[0] - 20] = (30, 30, 120)
    return Image.fromarray(a)


def warning_banner(width=600, height=240):
    """Draw a scanlator WARNING band (Thunder Scans style): dark block with yellow text bars.

    Identical in every chapter.
    Shaded across x so no row of it is uniform: where it meets the art directly there is no gutter to find.
    """
    a = np.empty((height, width, 3), np.uint8)
    a[:] = np.linspace(20, 90, width).astype(np.uint8)[None, :, None]
    for k, y in enumerate(range(20, height - 20, 36)):
        a[y : y + 14, 40 + 30 * (k % 3) : width - 40 - 20 * (k % 2)] = (230, 200, 40)
    return Image.fromarray(a)


def stack(*ims):
    """Images (or a (height, width) white gutter) one above the other: a band stitched onto a page."""
    w = max(im.width for im in ims if isinstance(im, Image.Image))
    rows = [
        np.full((im[0], w, 3), 255, np.uint8) if isinstance(im, tuple) else np.asarray(im.convert("RGB")) for im in ims
    ]
    return Image.fromarray(np.vstack(rows))
