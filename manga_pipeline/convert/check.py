"""The post-check of a staged chapter (SPEC "Conversion"), copied from the bulk converter's integrity.check_post without
its ComicInfo checks (export adds ComicInfo.xml) and with page bytes, not archive sizes, in the size rule (the
download is deflated, the staged file stored)."""

import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

from manga_pipeline import archive, images
from manga_pipeline.convert import image, pages


def _extensions(fmt):
    return pages.EXT_ALIASES.get(fmt, (images.EXTENSION.get(fmt),))


def check(cbz, rec, x_dir, kept):
    """The post-check's failures ([] when the staged CBZ passes). rec: image.process_chapter's record; x_dir: the
    extracted source pages; kept: the plan's kept pages ([{"name", "bytes"}])."""
    fails = []
    with zipfile.ZipFile(cbz) as z:
        bad = z.testzip()
        if bad is not None:
            return [f"crc-fail: {bad}"]
        infos = [i for i in z.infolist() if not i.is_dir()]
        if any(i.compress_type != zipfile.ZIP_STORED for i in infos):
            fails.append("not-store-mode")
        if any("/" in i.filename for i in infos):
            fails.append("nested-entries")
        names = [i.filename for i in infos]
        extra = [n for n in names if PurePosixPath(n).suffix.lower() not in archive.IMAGE_EXTS]
        if extra:
            fails.append(f"unexpected-files: {extra[:5]}")
        fmts, actual, out_bytes = Counter(), [], 0
        for name in names:
            if name in extra:
                continue
            data = z.read(name)
            out_bytes += len(data)
            fmt = images.detect(data)
            fmts[fmt] += 1
            if PurePosixPath(name).suffix.lower() not in _extensions(fmt):
                fails.append(f"extension-mismatch: {name} is {fmt}")
            page = pages.Page(data)
            try:
                page.raw()
            except Exception as e:  # noqa: BLE001 - any Pillow error on an output page is an undecodable page
                fails.append(f"undecodable-page: {name}: {type(e).__name__}")
                continue
            if name in rec["lossless"]:
                source, mode = rec["lossless"][name]["source"], rec["lossless"][name]["mode"]
                original = pages.Page((Path(x_dir) / source).read_bytes())
                if mode == "luma" and pages.has_colour(original):
                    fails.append(f"luma-check-on-colour-page: {name}")
                    mode = "exact"
                if not pages.pixels_match(original, page, mode)[0]:
                    fails.append(f"lossless-mismatch ({mode}): {name}")
            if rec["outcome"] == "avif":
                score = rec["scores"].get(name)
                if score is None:
                    fails.append(f"score-missing: {name}")
                elif score < image.SCORE_FLOOR:
                    fails.append(f"score-below-min: {name} {score:.2f}")
            actual.append({"name": name, "sha1": archive.sha1(data)})
    if len(actual) != len(rec["output"]):
        fails.append(f"page-count: {len(actual)} != expected {len(rec['output'])}")
    elif actual != rec["output"]:
        got, want = [p["name"] for p in actual], [p["name"] for p in rec["output"]]
        fails.append("page-order-mismatch: names differ" if got != want else "page-content-mismatch")
    if rec["outcome"] == "avif" and set(fmts) != {images.AVIF}:
        fails.append("mixed-formats: " + "+".join(sorted(fmts)))
    if sorted(names, key=archive.natural_key) != sorted(names):
        fails.append("page-order-ambiguous: natural and lexical order differ")
    if rec["outcome"] == "lossless" or rec.get("gate") == "floor+size":
        source_bytes = sum(q["bytes"] for q in kept)
        if rec["cover"]:  # an inserted volume cover's bytes are left out, as in the bulk converter's check
            out_bytes -= rec["cover"]["bytes"]
        if out_bytes > source_bytes:
            fails.append(f"output-larger-than-input: {out_bytes} > {source_bytes} page bytes")
    return fails
