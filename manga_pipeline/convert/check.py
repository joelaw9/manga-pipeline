"""The post-check of a staged chapter (SPEC "Conversion").

Copied from the bulk converter's integrity.check_post without its ComicInfo checks (export adds ComicInfo.xml) and with
page bytes, not archive sizes, in the size rule (the download is deflated, the staged file stored).
"""

import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import ReadOnly, TypedDict

from manga_pipeline import archive, images
from manga_pipeline.convert import covers, image, pages


class Staged(TypedDict):
    """What the post-check reads of image.process_chapter's record (read only: an image.ChapterRecord is one)."""

    outcome: ReadOnly[str]
    gate: ReadOnly[str | None]
    lossless: ReadOnly[Mapping[str, image.LosslessSource]]
    scores: ReadOnly[Mapping[str, float]]
    output: ReadOnly[Sequence[image.OutputPage]]
    cover: ReadOnly[covers.InsertedCover | None]


class SourceBytes(TypedDict):
    """A kept source page as the size rule reads it: its size in bytes (read only: a plan.PlanPage is one)."""

    bytes: ReadOnly[int]


def _extensions(fmt: str) -> tuple[str | None, ...]:
    return pages.EXT_ALIASES.get(fmt, (images.EXTENSION.get(fmt),))


@dataclass
class _Read:
    """The page pass's outputs.

    Its failures, the pages' formats, the decodable pages ({"name", "sha1"}) and the bytes of every page read.
    """

    fails: list[str] = field(default_factory=list)
    fmts: Counter[str] = field(default_factory=Counter)
    actual: list[image.OutputPage] = field(default_factory=list)
    out_bytes: int = 0


def check(cbz: Path, rec: Staged, x_dir: Path, kept: Sequence[SourceBytes]) -> list[str]:
    """Return the post-check's failures ([] when the staged CBZ passes).

    rec: image.process_chapter's record; x_dir: the extracted source pages; kept: the plan's kept pages ([{"name",
    "bytes"}]).
    """
    with zipfile.ZipFile(cbz) as z:
        bad = z.testzip()
        if bad is not None:
            return [f"crc-fail: {bad}"]
        infos = [i for i in z.infolist() if not i.is_dir()]
        names = [i.filename for i in infos]
        extra = [n for n in names if PurePosixPath(n).suffix.lower() not in archive.IMAGE_EXTS]
        fails = _entry_fails(infos, extra)
        read = _read_pages(z, [n for n in names if n not in extra], rec, x_dir)
    return [
        *fails,
        *read.fails,
        *_sequence_fails(read.actual, rec["output"]),
        *_format_fails(rec, read.fmts),
        *_order_fails(names),
        *_size_fails(rec, kept, read.out_bytes),
    ]


def _entry_fails(infos: list[zipfile.ZipInfo], extra: list[str]) -> list[str]:
    """Return the archive's own failures: not store mode, folders, entries that are not pages."""
    fails: list[str] = []
    if any(i.compress_type != zipfile.ZIP_STORED for i in infos):
        fails.append("not-store-mode")
    if any("/" in i.filename for i in infos):
        fails.append("nested-entries")
    if extra:
        fails.append(f"unexpected-files: {extra[:5]}")
    return fails


def _read_pages(z: zipfile.ZipFile, names: list[str], rec: Staged, x_dir: Path) -> _Read:
    """Read each page entry in order and check it.

    Its extension, that it decodes, then (a decodable page) its lossless match and its AVIF score.
    """
    read = _Read()
    for name in names:
        data = z.read(name)
        read.out_bytes += len(data)
        fmt = images.detect(data)
        read.fmts[fmt] += 1
        if PurePosixPath(name).suffix.lower() not in _extensions(fmt):
            read.fails.append(f"extension-mismatch: {name} is {fmt}")
        page = pages.Page(data)
        try:
            page.raw()
        except Exception as e:  # noqa: BLE001  # any Pillow error on an output page is an undecodable page
            read.fails.append(f"undecodable-page: {name}: {type(e).__name__}")
            continue
        if name in rec["lossless"]:
            read.fails += _lossless_fails(name, page, rec["lossless"][name], x_dir)
        if rec["outcome"] == "avif":
            read.fails += _score_fails(name, rec["scores"].get(name))
        read.actual.append({"name": name, "sha1": archive.sha1(data)})
    return read


def _lossless_fails(name: str, page: pages.Page, source: image.LosslessSource, x_dir: Path) -> list[str]:
    """Check a lossless page against its source ({"source", "mode"}).

    A luma check on a colour source is checked exact.
    """
    fails: list[str] = []
    mode = source["mode"]
    original = pages.Page((Path(x_dir) / source["source"]).read_bytes())
    if mode == "luma" and pages.has_colour(original):
        fails.append(f"luma-check-on-colour-page: {name}")
        mode = "exact"
    if not pages.pixels_match(original, page, mode)[0]:
        fails.append(f"lossless-mismatch ({mode}): {name}")
    return fails


def _score_fails(name: str, score: float | None) -> list[str]:
    """Check an AVIF page's score: present and at least the floor."""
    if score is None:
        return [f"score-missing: {name}"]
    if score < image.SCORE_FLOOR:
        return [f"score-below-min: {name} {score:.2f}"]
    return []


def _sequence_fails(actual: list[image.OutputPage], expected: Sequence[image.OutputPage]) -> list[str]:
    """Check the decodable pages against the record's output.

    The same count, then the same names and contents in order.
    """
    if len(actual) != len(expected):
        return [f"page-count: {len(actual)} != expected {len(expected)}"]
    if actual != expected:
        got, want = [p["name"] for p in actual], [p["name"] for p in expected]
        return ["page-order-mismatch: names differ" if got != want else "page-content-mismatch"]
    return []


def _format_fails(rec: Staged, fmts: Counter[str]) -> list[str]:
    """Check that an AVIF outcome holds AVIF pages only."""
    if rec["outcome"] == "avif" and set(fmts) != {images.AVIF}:
        return ["mixed-formats: " + "+".join(sorted(fmts))]
    return []


def _order_fails(names: list[str]) -> list[str]:
    """Check that the entries sort the same naturally and plainly."""
    if sorted(names, key=archive.natural_key) != sorted(names):
        return ["page-order-ambiguous: natural and lexical order differ"]
    return []


def _size_fails(rec: Staged, kept: Sequence[SourceBytes], out_bytes: int) -> list[str]:
    """Check that the lossless and size-gated AVIF outcomes are not larger than the source pages."""
    if not (rec["outcome"] == "lossless" or rec.get("gate") == "floor+size"):
        return []
    source_bytes = sum(q["bytes"] for q in kept)
    if rec["cover"]:  # an inserted volume cover's bytes are left out, as in the bulk converter's check
        out_bytes -= rec["cover"]["bytes"]
    if out_bytes > source_bytes:
        return [f"output-larger-than-input: {out_bytes} > {source_bytes} page bytes"]
    return []
