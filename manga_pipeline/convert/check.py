"""The post-check of a staged chapter (SPEC "Conversion"), copied from the bulk converter's integrity.check_post without
its ComicInfo checks (export adds ComicInfo.xml) and with page bytes, not archive sizes, in the size rule (the
download is deflated, the staged file stored)."""

import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from manga_pipeline import archive, images
from manga_pipeline.convert import image, pages


def _extensions(fmt):
    return pages.EXT_ALIASES.get(fmt, (images.EXTENSION.get(fmt),))


@dataclass
class _Read:
    """The page pass's outputs: its failures, the pages' formats, the decodable pages ({"name", "sha1"}) and the
    bytes of every page read."""

    fails: list = field(default_factory=list)
    fmts: Counter = field(default_factory=Counter)
    actual: list = field(default_factory=list)
    out_bytes: int = 0


def check(cbz, rec, x_dir, kept):
    """The post-check's failures ([] when the staged CBZ passes). rec: image.process_chapter's record; x_dir: the
    extracted source pages; kept: the plan's kept pages ([{"name", "bytes"}])."""
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


def _entry_fails(infos, extra):
    """The archive's own failures: not store mode, folders, entries that are not pages."""
    fails = []
    if any(i.compress_type != zipfile.ZIP_STORED for i in infos):
        fails.append("not-store-mode")
    if any("/" in i.filename for i in infos):
        fails.append("nested-entries")
    if extra:
        fails.append(f"unexpected-files: {extra[:5]}")
    return fails


def _read_pages(z, names, rec, x_dir):
    """Each page entry read in order and checked: its extension, that it decodes, then (a decodable page) its lossless
    match and its AVIF score."""
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
        except Exception as e:  # any Pillow error on an output page is an undecodable page
            read.fails.append(f"undecodable-page: {name}: {type(e).__name__}")
            continue
        if name in rec["lossless"]:
            read.fails += _lossless_fails(name, page, rec["lossless"][name], x_dir)
        if rec["outcome"] == "avif":
            read.fails += _score_fails(name, rec["scores"].get(name))
        read.actual.append({"name": name, "sha1": archive.sha1(data)})
    return read


def _lossless_fails(name, page, source, x_dir):
    """A lossless page against its source ({"source", "mode"}): a luma check on a colour source is checked exact."""
    fails = []
    mode = source["mode"]
    original = pages.Page((Path(x_dir) / source["source"]).read_bytes())
    if mode == "luma" and pages.has_colour(original):
        fails.append(f"luma-check-on-colour-page: {name}")
        mode = "exact"
    if not pages.pixels_match(original, page, mode)[0]:
        fails.append(f"lossless-mismatch ({mode}): {name}")
    return fails


def _score_fails(name, score):
    """An AVIF page's score: present and at least the floor."""
    if score is None:
        return [f"score-missing: {name}"]
    if score < image.SCORE_FLOOR:
        return [f"score-below-min: {name} {score:.2f}"]
    return []


def _sequence_fails(actual, expected):
    """The decodable pages against the record's output: the same count, then the same names and contents in order."""
    if len(actual) != len(expected):
        return [f"page-count: {len(actual)} != expected {len(expected)}"]
    if actual != expected:
        got, want = [p["name"] for p in actual], [p["name"] for p in expected]
        return ["page-order-mismatch: names differ" if got != want else "page-content-mismatch"]
    return []


def _format_fails(rec, fmts):
    """An AVIF outcome holds AVIF pages only."""
    if rec["outcome"] == "avif" and set(fmts) != {images.AVIF}:
        return ["mixed-formats: " + "+".join(sorted(fmts))]
    return []


def _order_fails(names):
    """The entries sort the same naturally and plainly."""
    if sorted(names, key=archive.natural_key) != sorted(names):
        return ["page-order-ambiguous: natural and lexical order differ"]
    return []


def _size_fails(rec, kept, out_bytes):
    """The lossless and size-gated AVIF outcomes are not larger than the source pages."""
    if not (rec["outcome"] == "lossless" or rec.get("gate") == "floor+size"):
        return []
    source_bytes = sum(q["bytes"] for q in kept)
    if rec["cover"]:  # an inserted volume cover's bytes are left out, as in the bulk converter's check
        out_bytes -= rec["cover"]["bytes"]
    if out_bytes > source_bytes:
        return [f"output-larger-than-input: {out_bytes} > {source_bytes} page bytes"]
    return []
