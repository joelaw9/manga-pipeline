"""Test pages and chapters for the convert tests: synthetic colour and gray pages as JPEG / PNG / WebP bytes, a CBZ of
them, its extraction and measurement, and a plan of it (convert.plan.build)."""

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from manga_pipeline import archive, config, images
from manga_pipeline.convert import image, pages, plan, vship

NO_EDIT = {"drop_pages": [], "strip_removed": [], "bands": [], "join": [], "split": False, "blank": []}
QUALITY = {"gray": 80, "colour": 80}


def colour(size=(96, 128), seed=1):
    """A colour panel: a gradient and filled circles."""
    rng = np.random.default_rng(seed)
    w, h = size
    grad = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    im = Image.fromarray(
        np.dstack([np.repeat(grad, w, 1), np.full((h, w), 120, np.uint8), np.repeat(255 - grad, w, 1)]), "RGB"
    )
    d = ImageDraw.Draw(im)
    for _ in range(4):
        x, y = rng.integers(0, w), rng.integers(0, h)
        d.ellipse((x, y, x + 20, y + 20), fill=tuple(int(c) for c in rng.integers(0, 255, 3)))
    return im


def gray(size=(96, 128), seed=2):
    """A gray page: paper, strokes, a frame."""
    rng = np.random.default_rng(seed)
    im = Image.new("L", size, 255)
    d = ImageDraw.Draw(im)
    w, h = size
    for _ in range(10):
        x, y = rng.integers(0, w), rng.integers(0, h)
        d.line((x, y, x + 20, y + 30), fill=0, width=2)
    d.rectangle((2, 2, w - 3, h - 3), outline=0, width=2)
    return im


def encoded(im, fmt, **opts):
    buf = io.BytesIO()
    im.save(buf, fmt, **opts)
    return buf.getvalue()


def jpeg(im, quality=92):
    return encoded(im.convert("RGB") if im.mode not in ("L", "RGB", "CMYK") else im, "JPEG", quality=quality)


def png(im):
    return encoded(im, "PNG")


def webp(im, *, lossless=False):
    return encoded(im, "WEBP", lossless=lossless, quality=80)


class Chapter:
    """A CBZ of members, extracted into x/ and measured, with its folders under root."""

    def __init__(self, root, members):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cbz = self.root / "chapter.cbz"
        archive.write(self.cbz, members)
        self.x = self.root / "x"
        self.files, problems = pages.extract(self.cbz, self.x)
        assert not problems, problems
        self.measured = images.measure_pages(self.cbz)
        self.out, self.work, self.rendered_dir = self.root / "out", self.root / "work", self.root / "render"

    def plan(self, edit=None, layout="pages", page_aspect=None, quality=QUALITY):
        p = plan.build(
            "test chapter", self.measured, self.files, edit or NO_EDIT, plan.SeriesLayout(layout, page_aspect)
        )
        p.quality = dict(quality)
        plan.routes(p)
        return p

    def run(self, chapter_plan):
        avif_first = chapter_plan.outcomes[0]["outcome"] == "avif"
        blanked = image.blank_pages(chapter_plan, self.x, self.root / "blank") if avif_first else {}
        src = image.sources(chapter_plan, self.x, blanked, self.root / "sources")
        rendered = image.render(chapter_plan, src, self.rendered_dir) if chapter_plan.render else []
        with ThreadPoolExecutor(2) as pool:
            return image.process_chapter(
                self.x,
                rendered,
                blanked,
                None,
                self.out,
                work_dir=self.work,
                pool=pool,
                scorer=scorer(),
                chapter_plan=chapter_plan,
                tool_paths=tool_paths(),
            )


def tool_paths():
    return {
        name: str(config.abs_path(config.cached()["tools"][name]))
        for name in ("avifenc", "avifdec", "jpegtran", "oxipng")
    }


def scorer():
    return vship.Scorer(config.cached()["tools"]["vship"])
