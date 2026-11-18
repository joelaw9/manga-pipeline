"""Execute a chapter plan (SPEC "Conversion").

Copied from the bulk converter's optimizer/image.py without the post-check fallback; settings are constants (they must
not drift from the bulk converter's), the page workers, the scorer and the tool paths are the caller's Encoder.

process_chapter tries the plan's outcomes in order; a measured failure (the SSIMULACRA2 floor after the q90 retry, the
AVIF size gate) moves to the next:
- rendered pages (crops, joins, split, strip): AVIF, floor only; else the original pages minus drops.
- JPEG / PNG / lossless WebP: AVIF (q90 retry per page) under floor + size gate; else the lossless path.
- mixed with lossy WebP: AVIF under both gates; else untouched. Lossy WebP / GIF / AVIF / JXL: untouched.
Gray, tinted and gray-mark pages encode 4:0:0 (scored against the gray version); colour pages keep their source's
chroma; a colour JPEG the plan marked jpeg_feed is fed to avifenc as is. The lossless path grays gray and gray-mark
JPEG / PNG pages (a JPEG only when jpegtran can: jpeg_gray); a lossless WebP passes through.
Logo blanks (the plan's edits["blank"]) apply to the AVIF outcomes only: blank_pages writes each blanked page as a PNG,
which the AVIF pages outcome encodes (and scores against) in place of the page, and which sources() puts in place of
the page for the render. Lossless and untouched outcomes never blank.

Work files go to the work folder the caller gives (inside the chapter's scratch folder), never elsewhere.
"""

import multiprocessing
import shutil
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, Self, TypedDict

import numpy as np
from PIL import Image

from manga_pipeline import archive, images, strip
from manga_pipeline.convert import covers, pages, plan, tools, vship

AVIF_QUALITY = 80  # the uncalibrated quality, and the ceiling (SPEC "Calibration")
RETRY_QUALITY = 90  # a page under the floor is encoded once more at this
SPEED = 6
SCORE_FLOOR = 80  # SSIMULACRA2, rounded to 2 decimals
MAX_SIZE_RATIO = 0.90  # page route: AVIF total bytes at most this share of the source pages' bytes
RENDERED_FMT = "RENDERED"  # task format of a rendered PNG page
LOSSLESS_GRAY = (images.GRAY, images.GRAY_MARK)  # the lossless path drops their colour channels


class PageTask(TypedDict):
    """1 AVIF page to encode: its source, output and work folder, format, class, avifenc --yuv, JPEG feed, qualities.

    q: the quality encoded first; retry_q: the quality of the one retry under the floor (None: no retry).
    """

    src: str
    out: str
    tmp: str
    fmt: str
    cls: str
    yuv: str
    jpeg_feed: bool
    q: int
    retry_q: int | None


class EncodeTask(PageTask):
    """A PageTask as a worker takes it: with the image tools' paths by tool name."""

    tools: dict[str, str]


class Encoded(TypedDict):
    """1 encode's worker result: its score reference and AVIF decode (paths), the AVIF's bytes, its seconds."""

    ref: str
    dec: str
    bytes_out: int
    sec: float


class AvifPage(plan.PageClass):
    """A finished AVIF page's record: its name, source format and bytes, --yuv, feed, last quality, score, outcome."""

    name: str
    fmt: str
    yuv: str
    jpeg_feed: bool
    bytes_in: int
    q: int
    score: float
    bytes_out: int
    ok: bool
    retried: bool
    sec: float


class LosslessTask(TypedDict):
    """1 page for the lossless path: its source, output and work folder, format, class and jpeg_gray."""

    src: str
    out: str
    tmp: str
    fmt: str
    cls: str
    jpeg_gray: bool


class LosslessJob(LosslessTask):
    """A LosslessTask as a worker takes it: with the image tools' paths by tool name."""

    tools: dict[str, str]


class LosslessPage(plan.PageClass):
    """A lossless page's record: its name, format, bytes in and out, its pixel check (verify) and whether it grayed."""

    name: str
    fmt: str
    bytes_in: int
    bytes_out: int
    verify: str
    grayed: bool


class Reshape(TypedDict):
    """A rendered outcome's reshape: its kind (reshape_kind), pages in and out."""

    kind: str
    pages_in: int
    pages_out: int


class OutputPage(TypedDict):
    """An output page: its file name and sha1."""

    name: str
    sha1: str


class LosslessSource(TypedDict):
    """A lossless output page's source page name and the pixel check it passed ("exact" or "luma")."""

    source: str
    mode: str


class ChapterRecord(TypedDict):
    """process_chapter's record (its docstring names each key); avif_rejected, the page records and reshape only set.

    avif_rejected: why the first AVIF attempt failed; avif_pages: the last AVIF attempt's; lossless_pages: the lossless
    outcome's; reshape: the rendered outcome's, when it stood.
    """

    route: str | None
    pages: int
    formats: dict[str, int]
    dropped: list[str]
    bytes_in: int
    outcome: str
    edits: str
    gate: str | None
    avif_rejected: NotRequired[str]
    avif_pages: NotRequired[list[AvifPage]]
    lossless_pages: NotRequired[list[LosslessPage]]
    reshape: NotRequired[Reshape]
    quality: dict[str, int]
    blanked: list[str]
    mean_score: float | None
    min_score: float | None
    gray: bool
    cover: covers.InsertedCover | None
    bytes_out: int
    output: list[OutputPage]
    scores: dict[str, float]
    lossless: dict[str, LosslessSource]


def pool(workers: int) -> ProcessPoolExecutor:
    """Return the page workers of 1 chapter: fresh processes.

    Spawned: never a fork of a process holding the GPU context and scorer threads.
    """
    return ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))


def _tmp(tmp: str | Path, src: str | Path, tag: str, ext: str = "png") -> Path:
    # built by concatenation: Path.with_suffix would cut dotted stems ("Ch.5_p001" -> "Ch.ref.png")
    return Path(tmp) / f"{Path(src).stem}.{tag}.{ext}"


def encode_page(job: tuple[EncodeTask, int]) -> Encoded:
    """Run the worker half of 1 AVIF page attempt (job: (task, q)).

    Decode the page, write its score reference, encode at q at the planned chroma, decode the AVIF with avifdec. The
    reference is our decode (images.open_image), as a gray version for a 4:0:0 encode. jpeg_feed: avifenc reads the JPEG
    page itself, else the reference PNG. Returns {"ref", "dec" (paths in task["tmp"]), "bytes_out", "sec"}.
    """
    task, q = job
    src, out, tmp = Path(task["src"]), Path(task["out"]), Path(task["tmp"])
    t0 = time.perf_counter()
    if task["jpeg_feed"] and (task["fmt"] != images.JPEG or task["yuv"] == "400"):
        msg = f"{src.name}: a JPEG feed needs a colour-encoded JPEG page, not {task['fmt']} at --yuv {task['yuv']}"
        raise ValueError(msg)
    im, alpha = images.open_image(src)
    if task["yuv"] == "400":
        ref = _tmp(tmp, src, "gray")
        images.save_png(images.to_gray(im, alpha=alpha), ref)
    else:
        ref = _tmp(tmp, src, "ref")
        images.save_png(im.convert("RGBA" if alpha else "RGB"), ref)
    feed, extra = (src, ("--ignore-exif", "--ignore-xmp")) if task["jpeg_feed"] else (ref, ())
    t = task["tools"]
    tools.run(t["avifenc"], "-q", q, "-s", SPEED, "-j", 1, "--yuv", task["yuv"], *extra, feed, out)
    dec = _tmp(tmp, src, f"q{q}")
    tools.run(t["avifdec"], "-j", 1, "--png-compress", 0, out, dec)
    return {"ref": str(ref), "dec": str(dec), "bytes_out": out.stat().st_size, "sec": time.perf_counter() - t0}


def _cleanup(task: PageTask, qs: list[int]) -> None:
    """Remove a page's reference and decodes (the .avif is the output)."""
    src, tmp = Path(task["src"]), Path(task["tmp"])
    for tag in ("ref", "gray", *(f"q{q}" for q in qs)):
        _tmp(tmp, src, tag).unlink(missing_ok=True)


type _Scored = tuple[float, float]  # a page's score and the seconds scoring took
type _JobFuture = Future[Encoded] | Future[_Scored]  # avif_pages' jobs: a page's encode or its score
type _PageFuture = _JobFuture | Future[LosslessPage]  # any page work in the pool or the scorer's threads


@dataclass(frozen=True)
class _Encoding:
    """A page's encode in the workers: its task's index and the future of its Encoded."""

    page: int
    future: Future[Encoded]


@dataclass(frozen=True)
class _Scoring:
    """A page's score in the scorer's threads: its task's index, its encode and the future of its score."""

    page: int
    encoded: Encoded
    future: Future[_Scored]


def _cancel(jobs: Collection[_PageFuture]) -> None:
    """Cancel queued work and wait for running work, so no page is still writing into the chapter's folders."""
    for g in jobs:
        g.cancel()
    for g in jobs:
        if not g.cancelled():
            g.exception()  # waits for it to finish; its own error is not raised here


def _retry_q(task: PageTask, score: float, tried: list[int]) -> int | None:
    """Return the quality a page scored under SCORE_FLOOR at its first quality is encoded at once more, else None.

    tried: the qualities encoded.
    """
    return task["retry_q"] if score < SCORE_FLOOR and len(tried) == 1 else None


def _drop_queued(jobs: dict[_JobFuture, _Encoding | _Scoring]) -> None:
    """Cancel the queued jobs and forget them; the running ones stay."""
    for g in [g for g in jobs if g.cancel()]:
        jobs.pop(g)


def _page_record(task: PageTask, tried: list[int], score: float, bytes_out: int, sec: float) -> AvifPage:
    """Return a finished AVIF page's record; tried: the qualities encoded, the last the one scored."""
    src = Path(task["src"])
    return {
        "name": src.name,
        "fmt": task["fmt"],
        "class": task["cls"],
        "yuv": task["yuv"],
        "jpeg_feed": task["jpeg_feed"],
        "bytes_in": src.stat().st_size,
        "q": tried[-1],
        "score": score,
        "bytes_out": bytes_out,
        "ok": score >= SCORE_FLOOR,
        "retried": len(tried) > 1,
        "sec": round(sec, 2),
    }


class Encoder:
    """The page encodes of a run.

    Its page workers (pool), the GPU scorer and the image tools' paths, which every encode needs. A task names no
    tools: the Encoder gives each its own.
    """

    def __init__(self, pool: Executor, scorer: vship.Scorer, tool_paths: dict[str, str]) -> None:
        """Store the run's page workers (pool), the GPU scorer and the image tools' paths."""
        self.pool, self.scorer, self.tool_paths = pool, scorer, tool_paths

    def _score(self, ref: str, dec: str) -> tuple[float, float]:
        t0 = time.perf_counter()
        return self.scorer.score_files(ref, dec), time.perf_counter() - t0

    def avif_pages(
        self, tasks: Sequence[PageTask], *, stop_on_fail: bool = False
    ) -> tuple[list[AvifPage], AvifPage | None]:
        """Encode and score AVIF pages.

        Workers in the pool encode and decode each page (encode_page); scorer threads in this process score each decode
        against its reference (scorer.score_files); a page under SCORE_FLOOR (its score rounded to 2 decimals) is
        encoded once more at task["retry_q"] (None: no retry, a calibration probe). With stop_on_fail, once a page fails
        after its retry the queued work is cancelled and the running work waited for.
        An exception cancels the rest, waits for the running work and is raised.
        task: {"src", "out", "tmp", "fmt", "cls", "yuv", "jpeg_feed", "q", "retry_q"}.
        Returns (the finished pages' records in task order, the failing page's record or None): {"name", "fmt",
        "class", "yuv", "jpeg_feed", "bytes_in", "q", "score", "bytes_out", "ok", "retried", "sec"}.
        """
        jobs: dict[_JobFuture, _Encoding | _Scoring]
        recs: dict[int, AvifPage]
        failed: AvifPage | None
        jobs, recs, failed = {}, {}, None
        qs: list[list[int]] = [[] for _ in tasks]
        secs = [0.0] * len(tasks)

        scorers = self.scorer.threads  # the scorer's own threads: their GPU handles live as long as it does

        def encode(i: int, q: int) -> None:
            qs[i].append(q)
            job: EncodeTask = {**tasks[i], "tools": self.tool_paths}
            future = self.pool.submit(encode_page, (job, q))
            jobs[future] = _Encoding(i, future)

        for i, t in enumerate(tasks):
            encode(i, t["q"])
        try:
            while jobs:
                # Future is invariant: typeshed's wait takes futures of 1 result type, these are encodes and scores.
                done, _ = wait(jobs, return_when=FIRST_COMPLETED)  # pyright: ignore[reportArgumentType]
                for f in done:
                    work = jobs.pop(f)
                    i = work.page
                    if isinstance(work, _Encoding):  # encoded: score it
                        r = work.future.result()
                        secs[i] += r["sec"]
                        scoring = scorers.submit(self._score, r["ref"], r["dec"])
                        jobs[scoring] = _Scoring(i, r, scoring)
                        continue
                    score, sec = work.future.result()
                    task = tasks[i]
                    s = round(score, 2)
                    secs[i] += sec
                    retry = _retry_q(task, s, qs[i])
                    if retry is not None:
                        if failed is None:
                            encode(i, retry)
                        else:
                            _cleanup(task, qs[i])
                        continue
                    _cleanup(task, qs[i])
                    recs[i] = _page_record(task, qs[i], s, work.encoded["bytes_out"], secs[i])
                    if stop_on_fail and failed is None and not recs[i]["ok"]:
                        failed = recs[i]
                        _drop_queued(jobs)
        except BaseException:
            _cancel(jobs)
            raise
        return [recs[i] for i in sorted(recs)], failed

    def cover(self, data: bytes, page_width: int, work_dir: Path) -> plan.CoverAvif:
        """Encode a volume cover's AVIF (covers) at page_width, through the AVIF gate at q80 (q90 retry).

        At its own class and chroma. Returns {"ok", "score", "class", "yuv"} and, when it passed, "path" (in work_dir)
        and "bytes".
        """
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True)
        cls, yuv = covers.cover_class(data)
        src = work_dir / f"{archive.COVER_STEM}.png"
        covers.scaled(data, page_width).save(src, "PNG")
        task: PageTask = {
            "src": str(src),
            "out": str(work_dir / f"{archive.COVER_STEM}.avif"),
            "tmp": str(work_dir),
            "fmt": RENDERED_FMT,
            "cls": cls,
            "yuv": yuv,
            "jpeg_feed": False,
            "q": AVIF_QUALITY,
            "retry_q": RETRY_QUALITY,
        }
        (r,), _ = self.avif_pages([task])
        if not r["ok"]:
            return {"ok": False, "score": r["score"], "class": cls, "yuv": yuv}
        return {
            "ok": True,
            "path": Path(task["out"]),
            "score": r["score"],
            "bytes": r["bytes_out"],
            "class": cls,
            "yuv": yuv,
        }

    def lossless_pages(self, tasks: Sequence[LosslessTask]) -> list[LosslessPage]:
        """Run each page through lossless_page in the workers (task: lossless_page's, without "tools").

        An exception cancels the rest, waits for what runs and is raised.
        """
        jobs: list[LosslessJob] = [{**t, "tools": self.tool_paths} for t in tasks]
        return _run_all(self.pool, lossless_page, jobs)


def grays_losslessly(cls: str, entry: plan.PlanPage) -> bool:
    """Return whether the lossless path stores this page gray.

    It does for a gray or gray-mark PNG, or such a JPEG jpegtran can gray (jpeg_gray); a CMYK, YCCK or RGB-coded JPEG
    stays exact, a lossless WebP passes through.
    """
    fmt = entry["format"]
    return cls in LOSSLESS_GRAY and (fmt == images.PNG or (fmt == images.JPEG and entry["jpeg_gray"]))


def lossless_page(task: LosslessJob) -> LosslessPage:
    """Store 1 page on the lossless path with jpegtran / oxipng.

    Gray and gray-mark JPEG / PNG pages lose their colour channels (a JPEG only when jpeg_gray), a lossless WebP passes
    through. Verifies pixels; an output not smaller than its source keeps the source bytes.
    task: {"src", "out", "tmp", "fmt", "cls", "jpeg_gray", "tools"}. Returns the record, "grayed" telling whether the
    page lost colour.
    """
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
            images.save_png(images.to_gray(im, alpha=alpha), feed)
        tools.run(t["oxipng"], "-o", "4", "--strip", "safe", "-t", "1", "--out", out, feed)
    else:  # lossless WebP passes through
        shutil.copyfile(src, out)
    mode = "luma" if gray else "exact"
    ok, diff = pages.pixels_match(src.read_bytes(), out.read_bytes(), mode)
    if not ok:
        msg = f"lossless verification failed: {src.name} ({mode}, max difference {diff})"
        raise RuntimeError(msg)
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


def _run_all(
    pool: Executor, fn: Callable[[LosslessJob], LosslessPage], tasks: Sequence[LosslessJob]
) -> list[LosslessPage]:
    """Run lossless page tasks; an exception cancels the rest, waits for what runs and is raised."""
    futures = [pool.submit(fn, t) for t in tasks]
    try:
        return [f.result() for f in futures]
    except BaseException:
        _cancel(futures)
        raise


def reshape_kind(edits: plan.PlannedEdits) -> str:
    """Return a rendered chapter's reshape: restitch (long strip), split (stitched pages) or rejoin (crops, joins)."""
    return "restitch" if edits["strip"] else "split" if edits["split"] else "rejoin"


def blank_pages(chapter_plan: plan.ChapterPlan, x_dir: Path, out_dir: Path) -> dict[str, Path]:
    """Apply the plan's logo blanks: {page name: its blanked PNG in out_dir}, alpha kept ({} when it has none)."""
    out: dict[str, Path] = {}
    for b in chapter_plan.edits["blank"]:
        im, alpha = images.open_image(Path(x_dir) / b["page"])
        blanked = Image.fromarray(images.blank(np.asarray(im.convert("RGB")), b["box"], b["fill"]))
        if alpha:
            blanked.putalpha(im.getchannel("A"))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        path = Path(out_dir) / (Path(b["page"]).stem + ".png")
        images.save_png(blanked, path)
        out[b["page"]] = path
    return out


def sources(chapter_plan: plan.ChapterPlan, x_dir: Path, blanked: Mapping[str, Path], out_dir: Path) -> Path:
    """Return the folder a render reads the pages from.

    x_dir, or (with blanks) out_dir holding every kept page under its own name, a blanked one as its blanked PNG (pages
    are decoded by content, not by extension).
    """
    if not blanked:
        return Path(x_dir)
    Path(out_dir).mkdir(parents=True)
    for q in chapter_plan.kept():
        shutil.copyfile(blanked.get(q["name"], Path(x_dir) / q["name"]), Path(out_dir) / q["name"])
    return Path(out_dir)


def render(chapter_plan: plan.ChapterPlan, x_dir: Path, out_dir: Path) -> list[Path]:
    """Render the plan's pages as PNGs in out_dir (strip.render), in order."""
    st = chapter_plan.edits["strip"] or chapter_plan.edits["split"]
    return strip.render(chapter_plan.render, x_dir, out_dir, st)


@dataclass(frozen=True)
class PageFiles:
    """A chapter's page sources.

    Its extracted download, the plan's rendered PNGs (in order, made from the blanked sources; [] when it renders
    nothing) and its blanked PNGs (blank_pages: {page name: blanked PNG}).
    """

    extracted: Path
    rendered: list[Path]
    blanked: dict[str, Path]

    @classmethod
    def of(cls, extracted: Path, rendered: Iterable[Path], blanked: Mapping[str, Path]) -> Self:
        """Build the PageFiles of the folder, rendered paths and blanked mapping as the plan's steps give them."""
        return cls(Path(extracted), [Path(p) for p in rendered], {name: Path(p) for name, p in blanked.items()})

    def original(self, name: str) -> Path:
        """Return the page as downloaded."""
        return self.extracted / name

    def is_blanked(self, name: str) -> bool:
        """Return whether the page has a blanked PNG."""
        return name in self.blanked


@dataclass(frozen=True)
class _AvifStood:
    """An AVIF attempt that stood: its outcome, its pages' records and its reshape (a rendered one's, else None)."""

    outcome: plan.Outcome
    pages: list[AvifPage]
    reshape: Reshape | None


@dataclass(frozen=True)
class _LosslessStood:
    """The lossless attempt (it always stands): its outcome and its pages' records."""

    outcome: plan.Outcome
    pages: list[LosslessPage]


@dataclass(frozen=True)
class _UntouchedStood:
    """The untouched attempt (it always stands): its outcome."""

    outcome: plan.Outcome


type _Stood = _AvifStood | _LosslessStood | _UntouchedStood


@dataclass(frozen=True)
class _Rejected:
    """A rejected AVIF attempt: why, and the records of the pages it encoded."""

    why: str
    pages: list[AvifPage]


class _Chapter:
    """1 chapter plan executed into out_dir (the attempts of process_chapter)."""

    def __init__(
        self, pages: PageFiles, out_dir: Path, tmp: Path, *, encoder: Encoder, chapter_plan: plan.ChapterPlan
    ) -> None:
        self.pages, self.out, self.tmp = pages, out_dir, tmp
        self.encoder, self.plan = encoder, chapter_plan
        self.kept = chapter_plan.kept()

    def task(self, src: Path, cls: str, yuv: str, fmt: str, *, jpeg_feed: bool) -> PageTask:
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
        }

    def avif(self, results: list[AvifPage], failed: AvifPage | None, *, size_gate: bool) -> str | None:
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

    def rendered_avif(self, o: plan.Outcome) -> _AvifStood | _Rejected:
        ch = self.plan
        tasks = [
            self.task(p, r["class"], r["yuv"], RENDERED_FMT, jpeg_feed=False)
            for p, r in zip(self.pages.rendered, ch.render, strict=True)
        ]
        results, failed = self.encoder.avif_pages(tasks, stop_on_fail=True)
        reshape: Reshape = {"kind": reshape_kind(ch.edits), "pages_in": len(self.kept), "pages_out": len(tasks)}
        why = self.avif(results, failed, size_gate=False)
        return _Rejected(why, results) if why is not None else _AvifStood(o, results, reshape)

    def pages_avif(self, o: plan.Outcome, *, size_gate: bool) -> _AvifStood | _Rejected:
        ch = self.plan
        tasks: list[PageTask] = []
        for q in self.kept:
            cls = ch.classes[q["name"]]
            yuv = plan.yuv_for(cls, q["chroma"])
            if self.pages.is_blanked(q["name"]):  # a new image: encoded from its blanked PNG and scored against it
                tasks.append(self.task(self.pages.blanked[q["name"]], cls, yuv, RENDERED_FMT, jpeg_feed=False))
            else:
                tasks.append(
                    self.task(self.pages.original(q["name"]), cls, yuv, q["format"], jpeg_feed=plan.feeds_jpeg(q, yuv))
                )
        results, failed = self.encoder.avif_pages(tasks, stop_on_fail=True)
        by_stem = {Path(q["name"]).stem: q for q in self.kept}
        for r in results:  # a blanked page: record the page's own name and source bytes
            q = by_stem[Path(r["name"]).stem]
            if self.pages.is_blanked(q["name"]):
                r.update(name=q["name"], bytes_in=q["bytes"])
        why = self.avif(results, failed, size_gate=size_gate)
        return _Rejected(why, results) if why is not None else _AvifStood(o, results, None)

    def lossless(self, o: plan.Outcome) -> _LosslessStood:
        tasks: list[LosslessTask] = [
            {
                "src": str(self.pages.original(q["name"])),
                "out": str(self.out / (Path(q["name"]).stem + images.EXTENSION[q["format"]])),
                "tmp": str(self.tmp),
                "fmt": q["format"],
                "cls": self.plan.classes[q["name"]],
                "jpeg_gray": q["jpeg_gray"],
            }
            for q in self.kept
        ]
        return _LosslessStood(o, self.encoder.lossless_pages(tasks))

    def untouched(self, o: plan.Outcome) -> _UntouchedStood:
        for q in self.kept:
            shutil.copyfile(self.pages.original(q["name"]), self.out / q["name"])
        return _UntouchedStood(o)

    def first_standing(self) -> tuple[_Stood, list[_Rejected]]:
        """Try the plan's outcomes in order; return the attempt that stood and the rejected ones before it, in order.

        A rejected attempt's output pages and AVIFs are cleared. RuntimeError when none stands.
        """
        rejected: list[_Rejected] = []
        for o in self.plan.outcomes:
            tried = self.attempt(o)
            if not isinstance(tried, _Rejected):
                return tried, rejected
            rejected.append(tried)
            _clear(self.out)
            for f in self.tmp.glob("*.avif"):
                f.unlink()
        msg = f"{self.plan.name}: no outcome stood"
        raise RuntimeError(msg)

    def attempt(self, o: plan.Outcome) -> _Stood | _Rejected:
        """Run 1 outcome into out_dir: the attempt that stood, or the rejected one."""
        if o["outcome"] == "avif" and o["edits"] == "all":
            return self.rendered_avif(o)
        if o["outcome"] == "avif":
            return self.pages_avif(o, size_gate=o["gate"] == "floor+size")
        if o["outcome"] == "lossless":
            return self.lossless(o)
        if o["outcome"] == "untouched":
            return self.untouched(o)
        msg = f"{self.plan.name}: unknown outcome {o['outcome']!r}"
        raise ValueError(msg)


def _clear(d: Path) -> None:
    for p in Path(d).iterdir():
        p.unlink()


def output_gray(chapter_plan: plan.ChapterPlan, outcome: plan.Outcome) -> bool:
    """Return whether every page the outcome stores is gray, from the plan's page classes (no decode).

    ComicInfo BlackAndWhite. An AVIF page of a gray / tinted / gray-mark class is 4:0:0; the lossless path grays gray
    and gray-mark JPEG / PNG pages; untouched pages are stored as they are (only a gray page is gray). An inserted
    volume cover is not counted, nor the page it replaces (BlackAndWhite describes the chapter's own pages).
    """
    kind = outcome["outcome"]

    def replaced(name: str) -> bool:
        return bool(outcome["cover"]) and Path(name).stem == archive.COVER_STEM

    if outcome["edits"] == "all":
        return all(r["class"] in plan.GRAYISH for r in chapter_plan.render if not replaced(r["name"]))
    kept = [(chapter_plan.classes[q["name"]], q) for q in chapter_plan.kept() if not replaced(q["name"])]
    if kind == "avif":
        return all(cls in plan.GRAYISH for cls, _ in kept)
    if kind == "lossless":
        return all(cls == images.GRAY or grays_losslessly(cls, q) for cls, q in kept)
    return all(cls == images.GRAY for cls, _ in kept)


def process_chapter(
    pages: PageFiles, out_dir: Path, *, work_dir: Path, encoder: Encoder, chapter_plan: plan.ChapterPlan
) -> ChapterRecord:
    """Execute a chapter plan and return its record.

    pages (its PageFiles), output pages in out_dir, work files in work_dir; encoder: the run's Encoder. Tries the
    outcomes in order; the last one (untouched or lossless) always stands. The one that stands inserts the volume cover
    its plan gave it (plan.routes), in place of a page named 000_cover. Returns the record: {"outcome", "edits",
    "route", "pages", "formats", "dropped",
    "bytes_in", "bytes_out", "avif_rejected"? (why the first AVIF attempt failed), "avif_pages"?, "lossless_pages"?,
    "reshape"?, "blanked" (the pages blanked: an AVIF outcome's only), "cover" (the inserted cover {"name", "format",
    "score", "bytes"} or None), "quality", "mean_score", "min_score" (the AVIF pages', when an AVIF outcome stood, else
    None), "gray", "output": [{"name", "sha1"}], "scores": {output name: score}, "lossless": {output name: {"source",
    "mode"}}}.
    """
    out_dir, tmp = Path(out_dir), Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    ch = chapter_plan
    run = _Chapter(pages, out_dir, tmp, encoder=encoder, chapter_plan=ch)
    stood, rejected = run.first_standing()
    o = stood.outcome
    avif = stood.pages if isinstance(stood, _AvifStood) else []
    inserted = None if o["cover"] is None else covers.insert(out_dir, o["cover"])
    outputs = sorted(out_dir.iterdir(), key=lambda p: archive.natural_key(p.name))
    if len(outputs) != o["pages_out"]:
        msg = f"{ch.name}: {len(outputs)} output pages, the outcome writes {o['pages_out']}"
        raise RuntimeError(msg)
    lossless_pages = stood.pages if isinstance(stood, _LosslessStood) else []
    scores, lossless = _output_checks(avif, lossless_pages, outputs, inserted)
    rec: ChapterRecord = {
        "route": ch.route,
        "pages": len(run.kept),
        "formats": dict(Counter(q["format"] for q in run.kept)),
        "dropped": [d["page"] for d in ch.edits["drop"]],
        "bytes_in": sum(q["bytes"] for q in run.kept),
        "outcome": o["outcome"],
        "edits": o["edits"],
        "gate": o["gate"],
        "quality": ch.quality,
        "blanked": sorted(pages.blanked, key=archive.natural_key) if o["outcome"] == "avif" else [],
        "mean_score": round(sum(r["score"] for r in avif) / len(avif), 2) if avif else None,
        "min_score": min((r["score"] for r in avif), default=None),
        "gray": output_gray(ch, o),
        "cover": inserted,
        "bytes_out": sum(p.stat().st_size for p in outputs),
        "output": [{"name": p.name, "sha1": archive.sha1(p.read_bytes())} for p in outputs],
        "scores": scores,
        "lossless": lossless,
    }
    _record_attempts(rec, stood, rejected)
    return rec


def _record_attempts(rec: ChapterRecord, stood: _Stood, rejected: list[_Rejected]) -> None:
    """Add to the record what the attempts left.

    The first rejection's reason, the last AVIF attempt's pages, the lossless pages and a rendered reshape that stood.
    """
    if rejected:
        rec["avif_rejected"] = rejected[0].why
    if isinstance(stood, _AvifStood):
        rec["avif_pages"] = stood.pages
        if stood.reshape is not None:
            rec["reshape"] = stood.reshape
    elif rejected:
        rec["avif_pages"] = rejected[-1].pages
    if isinstance(stood, _LosslessStood):
        rec["lossless_pages"] = stood.pages


def _output_checks(
    avif: list[AvifPage], lossless_pages: list[LosslessPage], outputs: list[Path], inserted: covers.InsertedCover | None
) -> tuple[dict[str, float], dict[str, LosslessSource]]:
    """Return the output pages' scores ({output name: score}) and lossless sources ({output name: {source, mode}}).

    avif: the AVIF pages of an AVIF outcome that stood, lossless_pages those of a lossless one ([] for another). The
    inserted cover replaced any page of its stem: its own score stands, nothing of that stem is lossless.
    """
    scores = {Path(r["name"]).stem + ".avif": r["score"] for r in avif}
    stems = {Path(p.name).stem: p.name for p in outputs}
    lossless: dict[str, LosslessSource] = {
        stems[Path(r["name"]).stem]: {"source": r["name"], "mode": r["verify"]} for r in lossless_pages
    }
    if inserted:
        stem = archive.COVER_STEM
        scores = {k: v for k, v in scores.items() if Path(k).stem != stem}
        lossless = {k: v for k, v in lossless.items() if Path(k).stem != stem}
        if inserted["score"] is not None:
            scores[inserted["name"]] = inserted["score"]
    return scores, lossless
