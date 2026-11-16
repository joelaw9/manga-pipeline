"""OCR text of a strip unit and its keyword test (SPEC "Junk decisions", candidates). Copied from the bulk converter's
optimizer/ocr.py and junk.text_hits; its process pool is not ported (only a chapter's unmatched edge blocks and probes
are read). RapidOCR with its bundled PP-OCR models on onnxruntime, on device `junk.ocr_device`:

- cpu: onnxruntime (CI, any machine);
- dml: onnxruntime-directml (Windows GPU); DirectML crashes when 2 threads run sessions at once: calls are serial;
- cuda: onnxruntime-gpu with its CUDA wheels (Linux NVIDIA); onnxruntime.preload_dlls() finds the wheel libraries.

`text` scales the image to WIDTH and reads it in overlapping tiles (a tall unit is otherwise shrunk until its text is
unreadable). The engine of a device is made once per process (loading it costs ~1 s).
"""

import functools
import importlib.resources
import logging
import threading
import time

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

WIDTH = 1000  # px: a unit is scaled to this width before OCR (old junk_ocr.width)
TILE_PX = 1800  # tile height at WIDTH ...
TILE_OVERLAP = 150  # ... overlapping so a line cut by one tile is whole in the next
MAX_WEAK_PX = 800  # a weak keyword counts only on a unit at most this tall (strip px; old junk_ocr.max_weak_px)
PROVIDERS = {"cpu": "CPUExecutionProvider", "dml": "DmlExecutionProvider", "cuda": "CUDAExecutionProvider"}
_QUIET = {
    "Global.log_level": "error",
    # explicit thread counts: onnxruntime otherwise logs an affinity error per session in an LXC
    "EngineConfig.onnxruntime.intra_op_num_threads": 1,
    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
}
PARAMS = {
    "cpu": _QUIET,
    "dml": {**_QUIET, "EngineConfig.onnxruntime.use_dml": True},
    "cuda": {**_QUIET, "EngineConfig.onnxruntime.use_cuda": True},
}
_serial = threading.Lock()


def provider_problem(device):
    """Why `device` cannot run OCR here, or None: unknown device, no onnxruntime, or a session on the device's
    provider that falls back to another (listing a provider is no proof it loads)."""
    if device not in PROVIDERS:
        return f"junk.ocr_device {device!r} is not one of {sorted(PROVIDERS)}"
    try:
        import onnxruntime  # noqa: PLC0415  # lazy: only process loads onnxruntime/RapidOCR; every command imports junk.ocr

        if device == "cuda":
            onnxruntime.preload_dlls()
        model = importlib.resources.files("rapidocr") / "models" / "PP-OCRv6_det_small.onnx"
        session = onnxruntime.InferenceSession(
            str(model), providers=list(dict.fromkeys([PROVIDERS[device], "CPUExecutionProvider"]))
        )
        first = session.get_providers()[0]
    except Exception as e:  # a missing or broken onnxruntime raises many types; reported as text
        return f"OCR on {device} cannot start: {type(e).__name__}: {e}"
    if first != PROVIDERS[device]:
        return f"OCR on {device}: onnxruntime ran the session on {first}, not {PROVIDERS[device]}"
    return None


@functools.cache
def _engine(device):
    """A reader of 1 tile: its text lines (RapidOCR on the device's onnxruntime provider)."""
    import onnxruntime  # noqa: PLC0415  # lazy: only process loads onnxruntime/RapidOCR; every command imports junk.ocr
    from rapidocr import RapidOCR  # noqa: PLC0415  # lazy: only process loads onnxruntime/RapidOCR; every command imports junk.ocr
    from rapidocr.utils.output import RapidOCROutput  # noqa: PLC0415  # lazy: only process loads onnxruntime/RapidOCR; every command imports junk.ocr

    if device == "cuda":
        onnxruntime.preload_dlls()
    engine = RapidOCR(params=PARAMS[device])

    def read(array):
        result = engine(array)
        if not isinstance(result, RapidOCROutput):  # detection, classification and recognition all run
            msg = f"RapidOCR returned {type(result).__name__}, not its full result"
            raise TypeError(msg)
        return list(result.txts or ())

    return read


def tiles(im):
    """Horizontal slices of im, at most TILE_PX tall, overlapping by TILE_OVERLAP."""
    if im.height <= TILE_PX:
        return [im]
    out, y = [], 0
    while True:
        out.append(im.crop((0, y, im.width, min(im.height, y + TILE_PX))))
        if y + TILE_PX >= im.height:
            return out
        y += TILE_PX - TILE_OVERLAP


def scale(im, width=WIDTH):
    """im as RGB, `width` px wide (aspect kept)."""
    im = im.convert("RGB")
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.Resampling.LANCZOS)


def text(im, device):
    """The lines read in im (scaled to WIDTH) on `device`, newline-joined; "" when none."""
    if device not in PARAMS:
        msg = f"ocr device must be one of {sorted(PARAMS)}, got {device!r}"
        raise ValueError(msg)
    im = scale(im)
    with _serial:
        read = _engine(device)
        t0 = time.perf_counter()
        lines = []
        for t in tiles(im):
            lines += read(np.asarray(t))
    log.debug("ocr %s %dx%d -> %d line(s) %.3fs", device, im.width, im.height, len(lines), time.perf_counter() - t0)
    return "\n".join(lines)


def normalize(s):
    """OCR text as the keyword test reads it: lower case, every whitespace run one space."""
    return " ".join(s.split()).lower()


def word_list_problem(words):
    """What is wrong with config `junk`'s word lists, or None: a weak or a paired term only restricts when a keyword
    counts, so each must be a keyword."""
    stray = {key: sorted(set(words[key]) - set(words["keywords"])) for key in ("weak", "paired")}
    return "; ".join(f"junk.{key} terms not in junk.keywords: {terms}" for key, terms in stray.items() if terms) or None


def text_hits(text_, height, words):
    """The keywords found in a unit's OCR text, in list order: each a case-insensitive substring of the normalized
    text; a weak term counts only on a unit at most MAX_WEAK_PX tall; a paired term only beside a hit of a keyword
    that is not paired; none at all when a suppress term is found. words: config `junk` (keywords, weak, paired,
    suppress)."""
    problem = word_list_problem(words)
    if problem:
        raise ValueError(problem)
    t = normalize(text_)
    if any(s.lower() in t for s in words["suppress"]):
        return []
    hits = [k for k in words["keywords"] if k.lower() in t and (height <= MAX_WEAK_PX or k not in words["weak"])]
    return hits if any(k not in words["paired"] for k in hits) else []
