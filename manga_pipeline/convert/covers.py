"""Volume covers (SPEC "Conversion"): a cover handed over in `volume_cover` goes into the chapter as the page
000_cover, encoded like the chapter. Copied from the bulk converter's optimizer/covers.py (the insertion half; picking
the chapter and fetching the image are intake's).

The cover is scaled down to the chapter's page width (most common width of its non-cover pages). Its AVIF is encoded
and scored before the route is chosen (image.encode_cover): an AVIF cover that passed lets a chapter whose pages share
no cover format convert to AVIF (plan.routes, force_avif). Each outcome takes the cover in the format its pages share
(plan.cover_format); insert writes it.
"""

import hashlib
import shutil
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from manga_pipeline import archive, config, errors, images
from manga_pipeline.convert import plan

JPEG_QUALITY = 92
WEBP_QUALITY = 90


class CoverError(RuntimeError):
    """The chapter's volume cover cannot be read as handed over (bad path, missing file, other sha256)."""


def load(cfg, row):
    """The cover file's bytes of a volume_cover row, checked: its path relative to paths.covers and inside it, the file
    there, its sha256 the row's. Raises CoverError naming the path; a dropped share's FileNotFoundError
    (errors.dropped_share) is raised as it is."""
    rel = Path(row["path"])
    root = config.abs_path(cfg["paths"]["covers"]).resolve()
    path = (root / rel).resolve()
    if rel.is_absolute() or rel.drive or not path.is_relative_to(root):
        msg = f"volume cover path {row['path']!r} is not inside paths.covers"
        raise CoverError(msg)
    try:
        data = path.read_bytes()
    except FileNotFoundError as e:
        if errors.dropped_share(e):  # the share, not the file: workflow.share_errors makes it Unreachable
            raise
        msg = f"volume cover {path} is missing"
        raise CoverError(msg) from e
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        msg = f"volume cover {path} is not the one handed over (another sha256)"
        raise CoverError(msg)
    return data


def width(chapter_plan):
    """The chapter's page width: the most common width of its non-cover pages (of all its pages when it has none),
    the first seen winning a tie."""
    pages = [q for q in chapter_plan.pages if not q["cover"]] or chapter_plan.pages
    return Counter(q["size"][0] for q in pages).most_common(1)[0][0]


def cover_class(data):
    """(page class, avifenc --yuv) of a cover: its class from its pixels, its own chroma for a colour cover."""
    rgb = np.asarray(images.open_image(data)[0].convert("RGB"))
    cls = images.page_class(rgb, images.COLOUR_BLOCK, images.TINT_P99, images.MARK_LIMITS)
    return cls, plan.yuv_for(cls, images.chroma(data, images.detect(data)))


def scaled(data, page_width):
    """The cover decoded, no wider than page_width (aspect kept; never upscaled)."""
    im, alpha = images.open_image(data)
    im = im.convert("RGBA" if alpha else "RGB")
    if im.width > page_width:
        im = im.resize((page_width, max(1, round(im.height * page_width / im.width))), Image.Resampling.LANCZOS)
    return im


def insert(out_dir, data, fmt, page_width, avif):
    """Write 000_cover.<ext> in format fmt into out_dir, in place of any page of that stem there. AVIF: the encoded
    cover (avif: image.encode_cover's result), copied; JPEG (q92), PNG, lossy WebP (q90): encoded here from the cover
    scaled to page_width. Returns {"name", "format", "score" (AVIF only), "bytes"}."""
    if fmt not in plan.COVER_FORMATS:
        msg = f"no volume cover encoding for {fmt}"
        raise ValueError(msg)
    out_dir = Path(out_dir)
    for old in out_dir.glob(f"{archive.COVER_STEM}.*"):
        old.unlink()
    out = out_dir / (archive.COVER_STEM + images.EXTENSION[fmt])
    score = None
    if fmt == images.AVIF:
        shutil.copyfile(avif["path"], out)
        score = avif["score"]
    else:
        im = scaled(data, page_width)
        if fmt == images.JPEG:
            im.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
        elif fmt == images.PNG:
            im.save(out, "PNG", optimize=True)
        else:
            im.save(out, "WEBP", quality=WEBP_QUALITY, method=6)
    return {"name": out.name, "format": fmt, "score": score, "bytes": out.stat().st_size}
