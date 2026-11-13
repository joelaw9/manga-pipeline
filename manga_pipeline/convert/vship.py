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

import ctypes as C
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

GPU = 0
SCORE_THREADS = 3  # scoring threads, 1 Vship handle each (the bulk converter measured 79-97 scores/s with 2-4)
ALPHA_BACKGROUNDS = (26, 230)  # a page with alpha: min score over dark / light backdrops (libjxl's tool: 0.1 / 0.9)

# Vship enum values (VshipColor.h)
_UINT8, _FULL_RANGE, _RGB_FAMILY, _RGB_MATRIX, _SRGB_TRANSFER, _BT709 = 2, 1, 1, 0, 13, 1
_INIT_STRUCT, _SCORE_STRUCT = 1, 4


class VshipError(RuntimeError):
    """The Vship library did not load, the GPU check failed, or a Vship call returned an error."""


class _Sub(C.Structure):
    _fields_ = [("subw", C.c_int), ("subh", C.c_int)]


class _Crop(C.Structure):
    _fields_ = [("top", C.c_int), ("bottom", C.c_int), ("left", C.c_int), ("right", C.c_int)]


class _Colorspace(C.Structure):
    _fields_ = [
        ("width", C.c_int64),
        ("height", C.c_int64),
        ("target_width", C.c_int64),
        ("target_height", C.c_int64),
        ("sample", C.c_int),
        ("range", C.c_int),
        ("subsampling", _Sub),
        ("chromaLocation", C.c_int),
        ("colorFamily", C.c_int),
        ("YUVMatrix", C.c_int),
        ("transferFunction", C.c_int),
        ("primaries", C.c_int),
        ("crop", _Crop),
    ]


class _Init(C.Structure):
    _fields_ = [("structType", C.c_int), ("src", _Colorspace), ("dis", _Colorspace), ("gpu_id", C.c_int)]


class _Score(C.Structure):
    _fields_ = [("structType", C.c_int), ("score", C.c_double)]


_PLANES, _STRIDES = C.c_void_p * 3, C.c_int64 * 3


def load(path):
    """The Vship library at path with its argument types set. Raises VshipError when it does not load."""
    try:
        lib = C.CDLL(str(path))
        lib.Vship_InitHandler.argtypes = [C.POINTER(C.c_void_p), C.c_void_p]
        lib.Vship_ComputeHandler.argtypes = [C.c_void_p, C.c_void_p, _PLANES, _PLANES, _STRIDES, _STRIDES]
        lib.Vship_FreeHandler.argtypes = [C.c_void_p]
        lib.Vship_GetDetailedLastError.argtypes = [C.c_char_p, C.c_int]
        lib.Vship_GPUFullCheck.argtypes = [C.c_int]
    except (OSError, AttributeError) as e:
        raise VshipError(f"cannot load the Vship library {path}: {e}") from e
    return lib


def _error(lib, code):
    buf = C.create_string_buffer(1024)
    lib.Vship_GetDetailedLastError(buf, len(buf))
    return f"Vship error {code}: {buf.value.decode(errors='replace').strip() or '(no detail)'}"


def check_gpu(lib, gpu=GPU):
    """Vship_GPUFullCheck on 1 GPU. Raises VshipError (no usable GPU, driver too old, ...)."""
    code = lib.Vship_GPUFullCheck(gpu)
    if code:
        raise VshipError(f"GPU {gpu} unusable for Vship: {_error(lib, code)}")


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
        self.lib, self.size, self.ptr = lib, (w, h), C.c_void_p()
        code = lib.Vship_InitHandler(C.byref(self.ptr), C.byref(_Init(_INIT_STRUCT, cs, cs, GPU)))
        if code:
            raise VshipError(f"handle {w}x{h}: {_error(lib, code)}")

    def free(self):
        code = self.lib.Vship_FreeHandler(self.ptr)
        if code:
            raise VshipError(f"free handle {self.size[0]}x{self.size[1]}: {_error(self.lib, code)}")


class Scorer:
    """The loaded, GPU-checked library, its scoring threads (`threads`, SCORE_THREADS of them, made once) and their
    handles. A thread's handle lives as long as the thread, so the threads live as long as the Scorer: a thread that
    ended would leave its handle's GPU memory allocated."""

    def __init__(self, path):
        self.lib = load(path)
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
            or ref.ndim != 3
            or ref.shape[2] != 3
            or ref.dtype != np.uint8
            or dist.dtype != np.uint8
        ):
            raise ValueError(
                f"vship.score: 2 HxWx3 uint8 arrays of 1 size needed, got {ref.shape} {ref.dtype} and "
                f"{dist.shape} {dist.dtype}"
            )
        h, w, _ = ref.shape
        pa = [np.ascontiguousarray(ref[:, :, c]) for c in range(3)]
        pb = [np.ascontiguousarray(dist[:, :, c]) for c in range(3)]
        out = _Score(_SCORE_STRUCT, 0.0)
        code = self.lib.Vship_ComputeHandler(
            self._handle(w, h).ptr,
            C.byref(out),
            _PLANES(*[p.ctypes.data for p in pa]),
            _PLANES(*[p.ctypes.data for p in pb]),
            _STRIDES(w, w, w),
            _STRIDES(w, w, w),
        )
        if code:
            raise VshipError(f"score {w}x{h}: {_error(self.lib, code)}")
        return out.score

    def score_files(self, reference, distorted):
        """SSIMULACRA2 of 2 image files (8-bit PNGs: the pipeline's reference and avifdec's decode; gray ones are
        scored as RGB). With alpha on either side: the lower score over a dark and a light backdrop."""
        t0 = time.perf_counter()
        with Image.open(reference) as a, Image.open(distorted) as b:
            a.load()
            b.load()
            if a.size != b.size:
                raise ValueError(f"vship: {reference} is {a.size}, {distorted} is {b.size}")
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
