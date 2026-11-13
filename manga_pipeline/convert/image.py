"""Execute a chapter plan (SPEC "Conversion"). Copied from the bulk converter's optimizer/image.py without logo
blanking, volume covers, force_avif and the post-check fallback; settings are constants (they must not drift from
the bulk converter's), tool paths and the scorer are passed in.

process_chapter tries the plan's outcomes in order; a measured failure (the SSIMULACRA2 floor after the q90 retry, the
AVIF size gate) moves to the next:
- rendered pages (crops, joins, split, strip): AVIF, floor only; else the original pages minus drops.
- JPEG / PNG / lossless WebP: AVIF (q90 retry per page) under floor + size gate; else the lossless path.
- mixed with lossy WebP: AVIF under both gates; else untouched. Lossy WebP / GIF / AVIF / JXL: untouched.
Gray, tinted and gray-mark pages encode 4:0:0 (scored against the gray version); colour pages keep their source's
chroma; a colour JPEG the plan marked jpeg_feed is fed to avifenc as is. The lossless path grays gray and gray-mark
JPEG / PNG pages (a JPEG only when jpegtran can: jpeg_gray); a lossless WebP passes through.

Work files go to the work folder the caller gives (inside the chapter's scratch folder), never elsewhere.
"""

import multiprocessing
import shutil
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

from PIL import Image

from manga_pipeline import archive, images, strip
from manga_pipeline.convert import pages, plan, tools

AVIF_QUALITY = 80  # the uncalibrated quality, and the ceiling (SPEC "Calibration")
RETRY_QUALITY = 90  # a page under the floor is encoded once more at this
SPEED = 6
SCORE_FLOOR = 80  # SSIMULACRA2, rounded to 2 decimals
MAX_SIZE_RATIO = 0.90  # page route: AVIF total bytes at most this share of the source pages' bytes
RENDERED_FMT = "RENDERED"  # task format of a rendered PNG page
LOSSLESS_GRAY = (images.GRAY, images.GRAY_MARK)  # the lossless path drops their colour channels


def pool(workers):
    """The page workers of 1 chapter: fresh processes (spawn: never a fork of a process holding the GPU context and
    scorer threads)."""
    return ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))


def _tmp(tmp, src, tag, ext="png"):
    # built by concatenation: Path.with_suffix would cut dotted stems ("Ch.5_p001" -> "Ch.ref.png")
    return Path(tmp) / f"{Path(src).stem}.{tag}.{ext}"


def encode_page(job):
    """Worker half of 1 AVIF page attempt (job: (task, q)): decode the page, write its score reference, encode at q at
    the planned chroma, decode the AVIF with avifdec. The reference is our decode (images.open_image), as a gray
    version for a 4:0:0 encode. jpeg_feed: avifenc reads the JPEG page itself, else the reference PNG. Returns {"ref",
    "dec" (paths in task["tmp"]), "bytes_out", "sec"}."""
    task, q = job
    src, out, tmp = Path(task["src"]), Path(task["out"]), Path(task["tmp"])
    t0 = time.perf_counter()
    if task["jpeg_feed"] and (task["fmt"] != images.JPEG or task["yuv"] == "400"):
        raise ValueError(
            f"{src.name}: a JPEG feed needs a colour-encoded JPEG page, not {task['fmt']} at --yuv {task['yuv']}"
        )
    im, alpha = images.open_image(src)
    if task["yuv"] == "400":
        ref = _tmp(tmp, src, "gray")
        images.save_png(images.to_gray(im, alpha), ref)
    else:
        ref = _tmp(tmp, src, "ref")
        images.save_png(im.convert("RGBA" if alpha else "RGB"), ref)
    feed, extra = (src, ("--ignore-exif", "--ignore-xmp")) if task["jpeg_feed"] else (ref, ())
    t = task["tools"]
    tools.run(t["avifenc"], "-q", q, "-s", SPEED, "-j", 1, "--yuv", task["yuv"], *extra, feed, out)
    dec = _tmp(tmp, src, f"q{q}")
    tools.run(t["avifdec"], "-j", 1, "--png-compress", 0, out, dec)
    return {"ref": str(ref), "dec": str(dec), "bytes_out": out.stat().st_size, "sec": time.perf_counter() - t0}


def _cleanup(task, qs):
    """Remove a page's reference and decodes (the .avif is the output)."""
    src, tmp = Path(task["src"]), Path(task["tmp"])
    for tag in ("ref", "gray", *(f"q{q}" for q in qs)):
        _tmp(tmp, src, tag).unlink(missing_ok=True)


def _cancel(jobs):
    """Cancel queued work and wait for running work, so no page is still writing into the chapter's folders."""
    for g in jobs:
        g.cancel()
    wait([g for g in jobs if not g.cancelled()])


def encode_avif_pages(pool, scorer, tasks, stop_on_fail=False):
    """Encode and score AVIF pages. Workers in pool encode and decode each page (encode_page); scorer threads in this
    process score each decode against its reference (scorer.score_files); a page under SCORE_FLOOR (its score rounded
    to 2 decimals) is encoded once more at task["retry_q"] (None: no retry, a calibration probe). With stop_on_fail,
    once a page fails after its retry the queued work is cancelled and the running work waited for. An exception
    cancels the rest, waits for the running work and is raised.
    task: {"src", "out", "tmp", "fmt", "cls", "yuv", "jpeg_feed", "q", "retry_q", "tools"}.
    Returns (the finished pages' records in task order, the failing page's record or None): {"name", "fmt", "class",
    "yuv", "jpeg_feed", "bytes_in", "q", "score", "bytes_out", "ok", "retried", "sec"}."""
    jobs, recs, failed = {}, {}, None
    qs = [[] for _ in tasks]
    secs = [0.0] * len(tasks)

    scorers = scorer.threads  # the scorer's own threads: their GPU handles live as long as it does

    def encode(i, q):
        qs[i].append(q)
        jobs[pool.submit(encode_page, (tasks[i], q))] = (i, q, None)

    def score(ref, dec):
        t0 = time.perf_counter()
        return scorer.score_files(ref, dec), time.perf_counter() - t0

    for i, t in enumerate(tasks):
        encode(i, t["q"])
    try:
        while jobs:
            done, _ = wait(jobs, return_when=FIRST_COMPLETED)
            for f in done:
                i, q, enc = jobs.pop(f)
                r = f.result()
                if enc is None:  # encoded: score it
                    secs[i] += r["sec"]
                    jobs[scorers.submit(score, r["ref"], r["dec"])] = (i, q, r)
                    continue
                task = tasks[i]
                s = round(r[0], 2)
                secs[i] += r[1]
                if s < SCORE_FLOOR and task["retry_q"] is not None and len(qs[i]) == 1:
                    if failed is None:
                        encode(i, task["retry_q"])
                    else:
                        _cleanup(task, qs[i])
                    continue
                _cleanup(task, qs[i])
                src = Path(task["src"])
                recs[i] = {
                    "name": src.name,
                    "fmt": task["fmt"],
                    "class": task["cls"],
                    "yuv": task["yuv"],
                    "jpeg_feed": task["jpeg_feed"],
                    "bytes_in": src.stat().st_size,
                    "q": q,
                    "score": s,
                    "bytes_out": enc["bytes_out"],
                    "ok": s >= SCORE_FLOOR,
                    "retried": len(qs[i]) > 1,
                    "sec": round(secs[i], 2),
                }
                if stop_on_fail and failed is None and not recs[i]["ok"]:
                    failed = recs[i]
                    for g in [g for g in jobs if g.cancel()]:
                        jobs.pop(g)
    except BaseException:
        _cancel(jobs)
        raise
    return [recs[i] for i in sorted(recs)], failed


def grays_losslessly(cls, entry):
    """Does the lossless path store this page gray? A gray or gray-mark PNG, or such a JPEG jpegtran can gray
    (jpeg_gray); a CMYK, YCCK or RGB-coded JPEG stays exact, a lossless WebP passes through."""
    fmt = entry["format"]
    return cls in LOSSLESS_GRAY and (fmt == images.PNG or (fmt == images.JPEG and entry["jpeg_gray"]))


def lossless_page(task):
    """jpegtran / oxipng; gray and gray-mark JPEG / PNG pages lose their colour channels (a JPEG only when jpeg_gray), a
    lossless WebP passes through. Verifies pixels; an output not smaller than its source keeps the source bytes.
    task: {"src", "out", "tmp", "fmt", "cls", "jpeg_gray", "tools"}. Returns the record, "grayed" telling whether the
    page lost colour."""
    src, out, tmp = Path(task["src"]), Path(task["out"]), Path(task["tmp"])
    im, alpha = images.open_image(src)
    with Image.open(src) as raw:
        src_mode, icc = raw.mode, raw.info.get("icc_profile")
    fmt, t = task["fmt"], task["tools"]
    gray = task["cls"] in LOSSLESS_GRAY and (fmt != images.JPEG or task["jpeg_gray"])
    if fmt == images.JPEG:
        # a non-sRGB profile defines the colours; a 1-component page keeps its (GRAY) profile when grayed, a colour
        # container grayed to Y drops it
        keep_icc = not images.is_srgb(icc) and (not gray or src_mode == images.GRAY_JPEG_MODE)
        tools.run(
            t["jpegtran"],
            *(["-grayscale"] if gray else []),
            "-copy",
            "icc" if keep_icc else "none",
            "-optimize",
            "-progressive",
            "-outfile",
            out,
            src,
        )
    elif fmt == images.PNG:
        feed = src
        if gray and src_mode in ("RGB", "RGBA", "P", "PA"):  # 8-bit colour container -> true gray
            feed = _tmp(tmp, src, "gray")
            images.save_png(images.to_gray(im, alpha), feed)
        tools.run(t["oxipng"], "-o", "4", "--strip", "safe", "-t", "1", "--out", out, feed)
    else:  # lossless WebP passes through
        shutil.copyfile(src, out)
    mode = "luma" if gray else "exact"
    ok, diff = pages.pixels_match(src.read_bytes(), out.read_bytes(), mode)
    if not ok:
        raise RuntimeError(f"lossless verification failed: {src.name} ({mode}, max difference {diff})")
    grayed = gray and fmt in (images.JPEG, images.PNG)
    if out.stat().st_size >= src.stat().st_size:  # optimisation did not help: keep the original bytes
        shutil.copyfile(src, out)
        mode, grayed = "exact", False
    return {
        "name": src.name,
        "fmt": fmt,
        "class": task["cls"],
        "bytes_in": src.stat().st_size,
        "bytes_out": out.stat().st_size,
        "verify": mode,
        "grayed": grayed,
    }


def _run_all(pool, fn, tasks):
    """Run page tasks; an exception cancels the rest, waits for what runs and is raised."""
    futures = [pool.submit(fn, t) for t in tasks]
    try:
        return [f.result() for f in futures]
    except BaseException:
        _cancel(futures)
        raise


def reshape_kind(edits):
    """A rendered chapter's reshape: restitch (long strip), split (stitched pages) or rejoin (crops, joins)."""
    return "restitch" if edits["strip"] else "split" if edits["split"] else "rejoin"


def render(chapter_plan, x_dir, out_dir):
    """The plan's rendered pages as PNGs in out_dir (strip.render), in order."""
    st = chapter_plan.edits["strip"] or chapter_plan.edits["split"]
    return strip.render(chapter_plan.render, x_dir, out_dir, st)


class _Chapter:
    """1 chapter plan executed into out_dir (the attempts of process_chapter)."""

    def __init__(self, x_dir, rendered, out_dir, tmp, pool, scorer, chapter_plan, tool_paths):
        self.x, self.rendered, self.out, self.tmp = x_dir, rendered, out_dir, tmp
        self.pool, self.scorer, self.plan, self.tools = pool, scorer, chapter_plan, tool_paths
        self.kept = chapter_plan.kept()

    def task(self, src, cls, yuv, fmt, jpeg_feed):
        return {
            "src": str(src),
            "out": str(self.tmp / (Path(src).stem + ".avif")),
            "tmp": str(self.tmp),
            "fmt": fmt,
            "cls": cls,
            "yuv": yuv,
            "jpeg_feed": jpeg_feed,
            "q": self.plan.quality[plan.quality_group(yuv)],
            "retry_q": RETRY_QUALITY,
            "tools": self.tools,
        }

    def avif(self, results, failed, size_gate):
        """Accept or reject an AVIF attempt: None when accepted, else why not."""
        if failed:
            return f"page {failed['name']} scored {failed['score']} at q{failed['q']}"
        total, bytes_in = sum(r["bytes_out"] for r in results), sum(q["bytes"] for q in self.kept)
        if size_gate and total > MAX_SIZE_RATIO * bytes_in:
            return f"AVIF total {total / bytes_in:.1%} of original > {MAX_SIZE_RATIO:.0%}"
        for r in results:
            stem = Path(r["name"]).stem
            shutil.move(self.tmp / f"{stem}.avif", self.out / f"{stem}.avif")
        return None

    def rendered_avif(self, rec):
        ch = self.plan
        tasks = [
            self.task(p, r["class"], r["yuv"], RENDERED_FMT, False)
            for p, r in zip(self.rendered, ch.render, strict=True)
        ]
        results, failed = encode_avif_pages(self.pool, self.scorer, tasks, stop_on_fail=True)
        rec["avif_pages"] = results
        rec["reshape"] = {"kind": reshape_kind(ch.edits), "pages_in": len(self.kept), "pages_out": len(tasks)}
        return self.avif(results, failed, False)

    def pages_avif(self, rec, size_gate):
        ch = self.plan
        tasks = []
        for q in self.kept:
            yuv = plan.yuv_for(ch.classes[q["name"]], q["chroma"])
            tasks.append(
                self.task(self.x / q["name"], ch.classes[q["name"]], yuv, q["format"], plan.feeds_jpeg(q, yuv))
            )
        results, failed = encode_avif_pages(self.pool, self.scorer, tasks, stop_on_fail=True)
        rec["avif_pages"] = results
        return self.avif(results, failed, size_gate)

    def lossless(self, rec):
        tasks = [
            {
                "src": str(self.x / q["name"]),
                "out": str(self.out / (Path(q["name"]).stem + images.EXTENSION[q["format"]])),
                "tmp": str(self.tmp),
                "fmt": q["format"],
                "cls": self.plan.classes[q["name"]],
                "jpeg_gray": q["jpeg_gray"],
                "tools": self.tools,
            }
            for q in self.kept
        ]
        rec["lossless_pages"] = _run_all(self.pool, lossless_page, tasks)

    def untouched(self):
        for q in self.kept:
            shutil.copyfile(self.x / q["name"], self.out / q["name"])

    def attempt(self, o, rec):
        """Run 1 outcome into out_dir. None when it stands, else why it was rejected."""
        if o["outcome"] == "avif" and o["edits"] == "all":
            return self.rendered_avif(rec)
        if o["outcome"] == "avif":
            return self.pages_avif(rec, o["gate"] == "floor+size")
        if o["outcome"] == "lossless":
            return self.lossless(rec)
        if o["outcome"] == "untouched":
            return self.untouched()
        raise ValueError(f"{self.plan.name}: unknown outcome {o['outcome']!r}")


def _clear(d):
    for p in Path(d).iterdir():
        p.unlink()


def output_gray(chapter_plan, outcome):
    """True when every page the outcome stores is gray, from the plan's page classes (no decode): ComicInfo
    BlackAndWhite. An AVIF page of a gray / tinted / gray-mark class is 4:0:0; the lossless path grays gray and
    gray-mark JPEG / PNG pages; untouched pages are stored as they are (only a gray page is gray)."""
    kind = outcome["outcome"]
    if outcome["edits"] == "all":
        return all(r["class"] in plan.GRAYISH for r in chapter_plan.render)
    kept = [(chapter_plan.classes[q["name"]], q) for q in chapter_plan.kept()]
    if kind == "avif":
        return all(cls in plan.GRAYISH for cls, _ in kept)
    if kind == "lossless":
        return all(cls == images.GRAY or grays_losslessly(cls, q) for cls, q in kept)
    return all(cls == images.GRAY for cls, _ in kept)


def process_chapter(x_dir, rendered, out_dir, work_dir, pool, scorer, chapter_plan, tool_paths):
    """Execute a chapter plan: pages in x_dir (the extraction), rendered (the plan's rendered PNGs, in order; [] when
    it renders nothing), output pages in out_dir, work files in work_dir. Tries the outcomes in order; the last one
    (untouched or lossless) always stands. Returns the record: {"outcome", "edits", "route", "pages", "formats",
    "dropped", "bytes_in", "bytes_out", "avif_rejected"? (why the first AVIF attempt failed), "avif_pages"?,
    "lossless_pages"?, "reshape"?, "quality", "mean_score", "min_score" (the AVIF pages', when an AVIF outcome stood,
    else None), "gray", "output": [{"name", "sha1"}], "scores": {output name: score}, "lossless": {output name:
    {"source", "mode"}}}."""
    x_dir, out_dir, tmp = Path(x_dir), Path(out_dir), Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    ch = chapter_plan
    run = _Chapter(x_dir, [Path(p) for p in rendered], out_dir, tmp, pool, scorer, ch, tool_paths)
    rec = {
        "route": ch.route,
        "pages": len(run.kept),
        "formats": dict(Counter(q["format"] for q in run.kept)),
        "dropped": [d["page"] for d in ch.edits["drop"]],
        "bytes_in": sum(q["bytes"] for q in run.kept),
    }
    for o in ch.outcomes:
        why = run.attempt(o, rec)
        if why is None:
            rec.update(outcome=o["outcome"], edits=o["edits"], gate=o["gate"])
            break
        rec.setdefault("avif_rejected", why)
        rec.pop("reshape", None)
        _clear(out_dir)
        for f in tmp.glob("*.avif"):
            f.unlink()
    else:
        raise RuntimeError(f"{ch.name}: no outcome stood")
    rec["quality"] = ch.quality
    avif = rec["avif_pages"] if rec["outcome"] == "avif" else []
    rec["mean_score"] = round(sum(r["score"] for r in avif) / len(avif), 2) if avif else None
    rec["min_score"] = min((r["score"] for r in avif), default=None)
    rec["gray"] = output_gray(ch, o)
    outputs = sorted(out_dir.iterdir(), key=lambda p: archive.natural_key(p.name))
    expected = len(ch.render) if rec["edits"] == "all" else len(run.kept)
    if len(outputs) != expected:
        raise RuntimeError(f"{ch.name}: {len(outputs)} output pages, the outcome writes {expected}")
    rec["bytes_out"] = sum(p.stat().st_size for p in outputs)
    rec["output"] = [{"name": p.name, "sha1": archive.sha1(p.read_bytes())} for p in outputs]
    rec["scores"] = {Path(r["name"]).stem + ".avif": r["score"] for r in avif}
    if rec["outcome"] == "lossless":
        stems = {Path(p.name).stem: p.name for p in outputs}
        rec["lossless"] = {
            stems[Path(r["name"]).stem]: {"source": r["name"], "mode": r["verify"]} for r in rec["lossless_pages"]
        }
    else:
        rec["lossless"] = {}
    return rec
