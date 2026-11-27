"""OCR text of a strip unit and its keyword test (SPEC "Junk decisions", candidates).

Copied from the bulk converter's optimizer/ocr.py and junk.text_hits; its process pool is a thread pool here, over 1
chapter's units (only a chapter's unmatched edge blocks and probes are read). RapidOCR with its bundled PP-OCR models
on onnxruntime, on device `junk.ocr_device`:

- cpu: onnxruntime (CI, any machine);
- dml: onnxruntime-directml (Windows GPU); DirectML crashes when 2 threads run sessions at once: 1 engine only;
- cuda: onnxruntime-gpu with its CUDA wheels (Linux NVIDIA); onnxruntime.preload_dlls() finds the wheel libraries.

`text` scales the image to WIDTH and reads it in overlapping tiles (a tall unit is otherwise shrunk until its text is
unreadable). `texts` reads a chapter's units on `junk.ocr_workers` threads at once (SPEC "Junk decisions",
candidates), each read on 1 engine of its own: a device's engines are made as reads need them, up to the most workers
asked for in the process, and kept for the process (loading one costs ~1 s).
"""

import importlib.resources
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import TypedDict

import numpy as np
from numpy.typing import NDArray
from PIL import Image

log = logging.getLogger(__name__)

WIDTH = 1000  # px: a unit is scaled to this width before OCR (old junk_ocr.width)
TILE_PX = 1800  # tile height at WIDTH ...
TILE_OVERLAP = 150  # ... overlapping so a line cut by one tile is whole in the next
MAX_WEAK_PX = 800  # a weak keyword counts only on a unit at most this tall (strip px; old junk_ocr.max_weak_px)
PROVIDERS = {"cpu": "CPUExecutionProvider", "dml": "DmlExecutionProvider", "cuda": "CUDAExecutionProvider"}
_QUIET: dict[str, str | int] = {
    "Global.log_level": "error",
    # explicit thread counts: onnxruntime otherwise logs an affinity error per session in an LXC
    "EngineConfig.onnxruntime.intra_op_num_threads": 1,
    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
}
PARAMS: dict[str, dict[str, str | int | bool]] = {
    "cpu": _QUIET,
    "dml": {**_QUIET, "EngineConfig.onnxruntime.use_dml": True},
    "cuda": {**_QUIET, "EngineConfig.onnxruntime.use_cuda": True},
}
type Reader = Callable[[NDArray[np.uint8]], list[str]]  # 1 engine: a tile's text lines


@dataclass
class _Engines:
    """A device's engines: those not reading now, how many were made, and how many may be (the most workers asked)."""

    idle: queue.Queue[Reader] = field(default_factory=queue.Queue[Reader])
    made: int = 0
    cap: int = 1


_engines: dict[str, _Engines] = {}
_making = threading.Lock()  # guards _engines and makes 1 engine at a time


class OcrError(Exception):
    """A read of `texts` failed: the index of its image (the first failed in input order) and its error."""

    def __init__(self, index: int, cause: BaseException) -> None:
        """Store the index and the error; the message names both."""
        super().__init__(f"OCR of image {index} failed: {type(cause).__name__}: {cause}")
        self.index = index
        self.cause = cause


class WordLists(TypedDict):
    """The keyword test's word lists (config `junk`): keywords, and the weak, paired and suppress terms."""

    keywords: list[str]
    weak: list[str]
    paired: list[str]
    suppress: list[str]


def word_lists(junk: WordLists) -> WordLists:
    """Return the keyword test's word lists from config `junk` (which holds more keys)."""
    return {"keywords": junk["keywords"], "weak": junk["weak"], "paired": junk["paired"], "suppress": junk["suppress"]}


def provider_problem(device: str) -> str | None:
    """Return why `device` cannot run OCR here, or None.

    Unknown device, no onnxruntime, or a session on the device's provider that falls back to another (listing a provider
    is no proof it loads).
    """
    if device not in PROVIDERS:
        return f"junk.ocr_device {device!r} is not one of {sorted(PROVIDERS)}"
    try:
        import onnxruntime  # noqa: PLC0415  # lazy: only process loads onnxruntime/RapidOCR; every command imports junk.ocr

        if device == "cuda":
            # onnxruntime's preload_dlls leaves its directory parameter unannotated: its type is partially Unknown
            onnxruntime.preload_dlls()  # pyright: ignore[reportUnknownMemberType]
        model = importlib.resources.files("rapidocr") / "models" / "PP-OCRv6_det_small.onnx"
        session = onnxruntime.InferenceSession(
            str(model), providers=list(dict.fromkeys([PROVIDERS[device], "CPUExecutionProvider"]))
        )
        first = session.get_providers()[0]
    except Exception as e:  # noqa: BLE001  # a missing or broken onnxruntime raises many types; reported as text
        return f"OCR on {device} cannot start: {type(e).__name__}: {e}"
    if first != PROVIDERS[device]:
        return f"OCR on {device}: onnxruntime ran the session on {first}, not {PROVIDERS[device]}"
    return None


def _new_reader(device: str) -> Reader:
    """Return a reader of 1 tile: its text lines (RapidOCR on the device's onnxruntime provider)."""
    import onnxruntime  # noqa: PLC0415  # lazy: only process loads onnxruntime/RapidOCR; every command imports junk.ocr
    from rapidocr import RapidOCR  # noqa: PLC0415  # lazy: only process loads onnxruntime/RapidOCR; every command imports junk.ocr
    from rapidocr.utils.output import RapidOCROutput  # noqa: PLC0415  # lazy: only process loads onnxruntime/RapidOCR; every command imports junk.ocr

    if device == "cuda":
        # onnxruntime's preload_dlls leaves its directory parameter unannotated: its type is partially Unknown
        onnxruntime.preload_dlls()  # pyright: ignore[reportUnknownMemberType]
    engine = RapidOCR(params=PARAMS[device])

    def read(array: NDArray[np.uint8]) -> list[str]:
        result = engine(array)
        if not isinstance(result, RapidOCROutput):  # detection, classification and recognition all run
            msg = f"RapidOCR returned {type(result).__name__}, not its full result"
            raise TypeError(msg)
        return list(result.txts or ())

    return read


def tiles(im: Image.Image) -> list[Image.Image]:
    """Horizontal slices of im, at most TILE_PX tall, overlapping by TILE_OVERLAP."""
    if im.height <= TILE_PX:
        return [im]
    out: list[Image.Image]
    out, y = [], 0
    while True:
        out.append(im.crop((0, y, im.width, min(im.height, y + TILE_PX))))
        if y + TILE_PX >= im.height:
            return out
        y += TILE_PX - TILE_OVERLAP


def scale(im: Image.Image, width: int = WIDTH) -> Image.Image:
    """Return im as RGB, `width` px wide (aspect kept)."""
    im = im.convert("RGB")
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.Resampling.LANCZOS)


def _check_device(device: str) -> None:
    if device not in PARAMS:
        msg = f"ocr device must be one of {sorted(PARAMS)}, got {device!r}"
        raise ValueError(msg)


def _take(device: str) -> Reader:
    """Return an idle engine of `device`, making one while fewer than its cap exist, else waiting for one."""
    with _making:
        engines = _engines.setdefault(device, _Engines())
        try:
            return engines.idle.get_nowait()
        except queue.Empty:
            if engines.made < engines.cap:
                read = _new_reader(device)
                engines.made += 1
                return read
    return engines.idle.get()


def text(im: Image.Image, device: str) -> str:
    """Return the lines read in im (scaled to WIDTH) on `device`, newline-joined; "" when none.

    Read on 1 engine of the device, which no other read uses meanwhile.
    """
    _check_device(device)
    im = scale(im)
    read = _take(device)
    try:
        t0 = time.perf_counter()
        lines: list[str] = []
        for t in tiles(im):
            lines += read(np.asarray(t))
    finally:
        _engines[device].idle.put(read)
    log.debug("ocr %s %dx%d -> %d line(s) %.3fs", device, im.width, im.height, len(lines), time.perf_counter() - t0)
    return "\n".join(lines)


def texts(images: Sequence[Image.Image], device: str, workers: int) -> list[str]:
    """Return `text` of each image, in input order, read on `workers` threads at once (each with its own engine).

    workers: at least 1, and 1 on dml. Once a read fails (or the wait is interrupted), the reads not started are
    cancelled; OcrError names the first image (in input order) whose read had failed by then.
    """
    _check_device(device)
    if workers < 1 or (device == "dml" and workers > 1):
        msg = f"ocr workers must be at least 1, and 1 on dml; got {workers} on {device}"
        raise ValueError(msg)
    with _making:
        engines = _engines.setdefault(device, _Engines())
        engines.cap = max(engines.cap, workers)
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(text, im, device) for im in images]
        try:
            wait(futures, return_when=FIRST_EXCEPTION)
        finally:
            for f in futures:
                f.cancel()  # only reads not started: a done or running one stays as it is
        failed = [
            (i, e) for i, f in enumerate(futures) if f.done() and not f.cancelled() and (e := f.exception()) is not None
        ]
        if failed:
            i, cause = failed[0]
            raise OcrError(i, cause) from cause
    return [f.result() for f in futures]


def normalize(s: str) -> str:
    """OCR text as the keyword test reads it: lower case, every whitespace run one space."""
    return " ".join(s.split()).lower()


def word_list_problem(words: WordLists) -> str | None:
    """Return what is wrong with config `junk`'s word lists, or None.

    A weak or a paired term only restricts when a keyword counts, so each must be a keyword.
    """
    stray = {key: sorted(set(words[key]) - set(words["keywords"])) for key in ("weak", "paired")}
    return "; ".join(f"junk.{key} terms not in junk.keywords: {terms}" for key, terms in stray.items() if terms) or None


def text_hits(text_: str, height: int, words: WordLists) -> list[str]:
    """Return the keywords found in a unit's OCR text, in list order.

    Each a case-insensitive substring of the normalized text; a weak term counts only on a unit at most MAX_WEAK_PX
    tall; a paired term only beside a hit of a keyword that is not paired; none at all when a suppress term is found.
    words: config `junk` (keywords, weak, paired, suppress).
    """
    problem = word_list_problem(words)
    if problem:
        raise ValueError(problem)
    t = normalize(text_)
    if any(s.lower() in t for s in words["suppress"]):
        return []
    hits = [k for k in words["keywords"] if k.lower() in t and (height <= MAX_WEAK_PX or k not in words["weak"])]
    return hits if any(k not in words["paired"] for k in hits) else []
