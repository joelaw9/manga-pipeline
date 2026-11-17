"""The Vship binding (convert.vship; SPEC "Conversion").

Synthetic manga and webtoon pages scored on the real GPU (marked gpu: run with --vship PATH), and the fail-fast paths.
Ported from the bulk converter's tests/test_vship.py.
"""

import io
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from manga_pipeline.convert import vship

from tests.fakes import convert as fake_convert
from tests.timing import budget


def manga_page(size, seed):
    """Draw a gray page: white paper, black panel borders and strokes, scan noise."""
    rng = np.random.default_rng(seed)
    im = Image.new("L", size, 255)
    d = ImageDraw.Draw(im)
    w, h = size
    for _ in range(40):
        x, y = rng.integers(0, w), rng.integers(0, h)
        d.line((x, y, x + rng.integers(-80, 80), y + rng.integers(-80, 80)), fill=0, width=int(rng.integers(1, 5)))
    d.rectangle((10, 10, w - 10, h - 10), outline=0, width=4)
    a = np.asarray(im, np.int16) + rng.integers(-4, 5, (h, w))
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L").convert("RGB")


def webtoon_page(size, seed):
    """Draw a colour panel: gradient sky and filled shapes."""
    rng = np.random.default_rng(seed)
    w, h = size
    grad = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    a = np.dstack([np.repeat(grad, w, 1), np.full((h, w), 120, np.uint8), np.repeat(255 - grad, w, 1)])
    im = Image.fromarray(a, "RGB")
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rng.integers(0, w), rng.integers(0, h)
        d.ellipse((x, y, x + 60, y + 60), fill=tuple(int(c) for c in rng.integers(0, 255, 3)))
    return im


def rgb(im):
    return np.asarray(im.convert("RGB"))


def jpeg_copy(im, quality):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    with Image.open(buf) as j:
        return rgb(j)


def blurred(im, radius):
    return rgb(im.filter(ImageFilter.GaussianBlur(radius)))


@pytest.fixture
def scorer(vship_path):
    return vship.Scorer(vship_path)


@pytest.mark.gpu
@budget(load="subprocess")
@pytest.mark.parametrize("page", [manga_page((600, 860), 1), webtoon_page((480, 1400), 2)], ids=["manga", "webtoon"])
def test_identical_page_scores_near_100_and_degraded_pages_lower(scorer, page):
    ref = rgb(page)
    same, weak, strong = (
        scorer.score(ref, ref),
        scorer.score(ref, jpeg_copy(page, 90)),
        scorer.score(ref, jpeg_copy(page, 30)),
    )
    assert same > 99, f"identical pages must score ~99.5, got {same}"
    assert same > weak > strong, (same, weak, strong)


@pytest.mark.gpu
@budget(load="subprocess")
def test_same_pair_scores_identically_five_times(scorer):
    page = manga_page((600, 860), 3)
    ref, dist = rgb(page), blurred(page, 1.2)
    assert len({scorer.score(ref, dist) for _ in range(5)}) == 1, "a page's score does not depend on when it is scored"


@pytest.mark.gpu
@budget(load="subprocess")
def test_a_new_size_on_the_same_thread_gets_its_own_handle(scorer):
    pages = [webtoon_page(s, 4) for s in ((300, 500), (320, 500), (300, 500))]
    pairs = [(rgb(p), blurred(p, 1)) for p in pages]
    here = [scorer.score(*pr) for pr in pairs]
    with ThreadPoolExecutor(1) as t:
        fresh = [t.submit(scorer.score, *pr).result() for pr in pairs[:2]]
    assert here[:2] == fresh, "each size scores as on a fresh thread"
    assert here[0] == here[2], "each size scores as on a fresh thread"


@pytest.mark.gpu
@budget(load="subprocess")
def test_score_files_reads_gray_pngs_as_rgb(scorer, tmp_path):
    page = manga_page((400, 580), 5).convert("L")
    worse = page.filter(ImageFilter.GaussianBlur(1))
    page.save(tmp_path / "ref.png")
    worse.save(tmp_path / "dec.png")
    assert scorer.score_files(tmp_path / "ref.png", tmp_path / "dec.png") == scorer.score(rgb(page), rgb(worse))


@pytest.mark.gpu
@budget(load="subprocess")
def test_alpha_is_composited_before_scoring(scorer, tmp_path):
    a = np.dstack([rgb(webtoon_page((300, 400), 6)), np.full((400, 300), 255, np.uint8)])
    a[:100, :, 3] = 0
    b = a.copy()
    b[:100, :, :3] = 0  # differs only under fully transparent pixels
    Image.fromarray(a, "RGBA").save(tmp_path / "ref.png")
    Image.fromarray(b, "RGBA").save(tmp_path / "dec.png")
    assert scorer.score_files(tmp_path / "ref.png", tmp_path / "dec.png") > 99


@pytest.mark.gpu
@budget(load="subprocess")
def test_a_missing_gpu_fails_the_check_with_vships_message(vship_path):
    with pytest.raises(vship.VshipError, match="GPU 99 unusable for Vship: Vship error"):
        vship.check_gpu(vship.load(vship_path), 99)


@budget(load="fs")
def test_a_file_that_is_not_the_library_fails_to_load(tmp_path):
    bad = tmp_path / "libvship_NVIDIA.dll"
    bad.write_bytes(b"not a library")
    with pytest.raises(vship.VshipError, match="cannot load the Vship library"):
        vship.load(bad)


@budget(load="pure")
def test_arrays_of_different_sizes_are_refused():
    s = object.__new__(fake_convert.REAL_SCORER)  # the autouse fake stands in for vship.Scorer here
    with pytest.raises(ValueError, match="2 HxWx3 uint8 arrays of 1 size"):
        s.score(np.zeros((10, 10, 3), np.uint8), np.zeros((10, 11, 3), np.uint8))


@budget(load="pure")
def test_a_handle_vship_fails_to_free_raises_with_vships_message():
    """A non-zero code from Vship_FreeHandler raises instead of leaking GPU memory.

    Stubbed: a real double free crashes the process. A score at a new size frees the thread's handle of the old size.
    """
    lib = type(
        "Lib",
        (),
        {
            "Vship_GPUFullCheck": staticmethod(lambda _gpu: 0),
            "Vship_InitHandler": staticmethod(lambda _ptr, _init: 0),
            "Vship_ComputeHandler": staticmethod(lambda *_args: 0),
            "Vship_FreeHandler": staticmethod(lambda _ptr: 5),
            "Vship_GetDetailedLastError": staticmethod(lambda _buf, _n: 0),
        },
    )()
    scorer = fake_convert.REAL_SCORER("stub", loader=lambda _path: lib)  # the autouse fake stands in for vship.Scorer
    scorer.score(np.zeros((500, 300, 3), np.uint8), np.zeros((500, 300, 3), np.uint8))
    with pytest.raises(vship.VshipError, match=r"free handle 300x500: Vship error 5"):
        scorer.score(np.zeros((10, 10, 3), np.uint8), np.zeros((10, 10, 3), np.uint8))
