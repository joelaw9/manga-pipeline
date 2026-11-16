"""SSIMULACRA2 on the GPU (SPEC "Conversion"): ctypes binding of Vship 5.1.2's C API, copied from the bulk
converter's optimizer/vship.py. The library path is config `tools.vship` (the Linux libvship.so or the Windows DLL;
the binding loads either unchanged).

Scorer(path) loads the library and checks the GPU (Vship_GPUFullCheck); a failure raises VshipError naming Vship's own
message. Use it in 1 process only (the parent): every process holding a handle opens its own CUDA context (~390 MiB).
Each scoring thread holds 1 handle, made again when the image size changes, so video memory stays bounded by the
thread count.

Input: 8-bit full-range sRGB, planes R, G, B (RGB matrix 0, transfer 13 sRGB, primaries 1 BT.709), as measured against
libjxl's ssimulacra2.
"""

import ctypes
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

from manga_pipeline import images

log = logging.getLogger(__name__)

GPU = 0
SCORE_THREADS = 3  # scoring threads, 1 Vship handle each (the bulk converter measured 79-97 scores/s with 2-4)
ALPHA_BACKGROUNDS = (26, 230)  # a page with alpha: min score over dark / light backdrops (libjxl's tool: 0.1 / 0.9)

# Vship enum values (VshipColor.h)
_UINT8, _FULL_RANGE, _RGB_FAMILY, _RGB_MATRIX, _SRGB_TRANSFER, _BT709 = 2, 1, 1, 0, 13, 1
_INIT_STRUCT, _SCORE_STRUCT = 1, 4
RGB_NDIM = 3  # an RGB image array: (height, width, channel)


class VshipError(RuntimeError):
    """The Vship library did not load, the GPU check failed, or a Vship call returned an error."""


class _Sub(ctypes.Structure):
    _fields_ = [("subw", ctypes.c_int), ("subh", ctypes.c_int)]


class _Crop(ctypes.Structure):
    _fields_ = [("top", ctypes.c_int), ("bottom", ctypes.c_int), ("left", ctypes.c_int), ("right", ctypes.c_int)]


class _Colorspace(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int64),
        ("height", ctypes.c_int64),
        ("target_width", ctypes.c_int64),
        ("target_height", ctypes.c_int64),
        ("sample", ctypes.c_int),
        ("range", ctypes.c_int),
        ("subsampling", _Sub),
        ("chromaLocation", ctypes.c_int),
        ("colorFamily", ctypes.c_int),
        ("YUVMatrix", ctypes.c_int),
        ("transferFunction", ctypes.c_int),
        ("primaries", ctypes.c_int),
        ("crop", _Crop),
    ]


class _Init(ctypes.Structure):
    _fields_ = [("structType", ctypes.c_int), ("src", _Colorspace), ("dis", _Colorspace), ("gpu_id", ctypes.c_int)]


class _Score(ctypes.Structure):
    _fields_ = [("structType", ctypes.c_int), ("score", ctypes.c_double)]


_PLANES, _STRIDES = ctypes.c_void_p * 3, ctypes.c_int64 * 3


def load(path):
    """The Vship library at path with its argument types set. Raises VshipError when it does not load."""
    try:
        lib = ctypes.CDLL(str(path))
        lib.Vship_InitHandler.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        lib.Vship_ComputeHandler.argtypes = [ctypes.c_void_p, ctypes.c_void_p, _PLANES, _PLANES, _STRIDES, _STRIDES]
        lib.Vship_FreeHandler.argtypes = [ctypes.c_void_p]
        lib.Vship_GetDetailedLastError.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.Vship_GPUFullCheck.argtypes = [ctypes.c_int]
    except (OSError, AttributeError) as e:
        msg = f"cannot load the Vship library {path}: {e}"
        raise VshipError(msg) from e
    return lib


def _error(lib, code):
    buf = ctypes.create_string_buffer(1024)
    lib.Vship_GetDetailedLastError(buf, len(buf))
    return f"Vship error {code}: {buf.value.decode(errors='replace').strip() or '(no detail)'}"


def check_gpu(lib, gpu=GPU):
    """Vship_GPUFullCheck on 1 GPU. Raises VshipError (no usable GPU, driver too old, ...)."""
    code = lib.Vship_GPUFullCheck(gpu)
    if code:
        msg = f"GPU {gpu} unusable for Vship: {_error(lib, code)}"
        raise VshipError(msg)


class _Handle:
    """1 thread's Vship handle for 1 image size."""

    def __init__(self, lib, w, h):
        cs = _Colorspace(
            w,
            h,
            -1,
            -1,
            _UINT8,
            _FULL_RANGE,
            _Sub(0, 0),
            0,
            _RGB_FAMILY,
            _RGB_MATRIX,
            _SRGB_TRANSFER,
            _BT709,
            _Crop(0, 0, 0, 0),
        )
        self.lib, self.size, self.ptr = lib, (w, h), ctypes.c_void_p()
        code = lib.Vship_InitHandler(ctypes.byref(self.ptr), ctypes.byref(_Init(_INIT_STRUCT, cs, cs, GPU)))
        if code:
            msg = f"handle {w}x{h}: {_error(lib, code)}"
            raise VshipError(msg)

    def free(self):
        code = self.lib.Vship_FreeHandler(self.ptr)
        if code:
            msg = f"free handle {self.size[0]}x{self.size[1]}: {_error(self.lib, code)}"
            raise VshipError(msg)


class Scorer:
    """The loaded, GPU-checked library, its scoring threads (`threads`, SCORE_THREADS of them, made once) and their
    handles. A thread's handle lives as long as the thread, so the threads live as long as the Scorer: a thread that
    ended would leave its handle's GPU memory allocated."""

    def __init__(self, path, *, loader=load):
        """loader(path): the Vship library at path (load by default)."""
        self.lib = loader(path)
        check_gpu(self.lib)
        self._local = threading.local()
        self.threads = ThreadPoolExecutor(SCORE_THREADS, thread_name_prefix="vship")

    def _handle(self, w, h):
        hd = getattr(self._local, "handle", None)
        if hd is None or hd.size != (w, h):
            if hd is not None:
                self._local.handle = None
                hd.free()
            self._local.handle = _Handle(self.lib, w, h)
        return self._local.handle

    def score(self, ref, dist):
        """SSIMULACRA2 of dist against ref: HxWx3 uint8 RGB arrays of 1 size. Thread-safe (a handle per thread)."""
        if (
            ref.shape != dist.shape
            or ref.ndim != RGB_NDIM
            or ref.shape[2] != images.RGB_CHANNELS
            or ref.dtype != np.uint8
            or dist.dtype != np.uint8
        ):
            msg = (
                f"vship.score: 2 HxWx3 uint8 arrays of 1 size needed, got {ref.shape} {ref.dtype} and "
                f"{dist.shape} {dist.dtype}"
            )
            raise ValueError(msg)
        h, w, _ = ref.shape
        pa = [np.ascontiguousarray(ref[:, :, c]) for c in range(3)]
        pb = [np.ascontiguousarray(dist[:, :, c]) for c in range(3)]
        out = _Score(_SCORE_STRUCT, 0.0)
        code = self.lib.Vship_ComputeHandler(
            self._handle(w, h).ptr,
            ctypes.byref(out),
            _PLANES(*[p.ctypes.data for p in pa]),
            _PLANES(*[p.ctypes.data for p in pb]),
            _STRIDES(w, w, w),
            _STRIDES(w, w, w),
        )
        if code:
            msg = f"score {w}x{h}: {_error(self.lib, code)}"
            raise VshipError(msg)
        return out.score

    def score_files(self, reference, distorted):
        """SSIMULACRA2 of 2 image files (8-bit PNGs: the pipeline's reference and avifdec's decode; gray ones are
        scored as RGB). With alpha on either side: the lower score over a dark and a light backdrop."""
        t0 = time.perf_counter()
        with Image.open(reference) as a, Image.open(distorted) as b:
            a.load()
            b.load()
            if a.size != b.size:
                msg = f"vship: {reference} is {a.size}, {distorted} is {b.size}"
                raise ValueError(msg)
            if _has_alpha(a) or _has_alpha(b):
                pairs = [(_over(a, bg), _over(b, bg)) for bg in ALPHA_BACKGROUNDS]
            else:
                pairs = [(np.asarray(a.convert("RGB")), np.asarray(b.convert("RGB")))]
        s = min(self.score(*p) for p in pairs)
        log.debug("vship %s %s -> %.2f %.3fs", reference, distorted, s, time.perf_counter() - t0)
        return s


def _has_alpha(im):
    return im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info


def _over(im, bg):
    backdrop = Image.new("RGBA", im.size, (bg, bg, bg, 255))
    return np.asarray(Image.alpha_composite(backdrop, im.convert("RGBA")).convert("RGB"))
