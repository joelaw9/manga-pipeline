"""Fakes of the convert stage's outside world (SPEC "Tests": offline, no GPU): the 4 image tools and the scorer.

fake_run stands in for convert.tools.run and does each tool's job with Pillow, so the files it writes are real images
of the right format: avifenc writes an AVIF of the feed (gray for --yuv 400), avifdec a PNG of the AVIF, jpegtran and
oxipng copy their input (lossless by definition). Every call is appended to CALLS as (tool name, args).

install also runs the page workers as threads (convert.image.pool), so the fake tools reach them.

FakeScorer stands in for convert.vship.Scorer. Its score comes from SCORE(ref path, decode path), by default 90; a
test sets its own, e.g. reading the quality from the decode's name (`<stem>.q<Q>.png`).

install(setattr) puts both in place and makes the tool files the config names, for a test (monkeypatch.setattr) or a
process run as a world (tests/world_run.py).
"""

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from manga_pipeline import config, fleet
from manga_pipeline.convert import image, tools, vship

REAL_SCORER = vship.Scorer  # taken at import, before install replaces it
CALLS = []
DEFAULT_SCORE = 90.0
SCORE = [lambda _ref, _dec: DEFAULT_SCORE]  # 1-element holder: tests replace SCORE[0]


def _tool(path):
    return Path(path).name.removesuffix(".exe")


def _after(args, flag):
    return args[args.index(flag) + 1]


def fake_run(*args):
    args = [str(a) for a in args]
    name, rest = _tool(args[0]), args[1:]
    CALLS.append((name, rest))
    if name == "avifenc":
        feed, out = rest[-2], rest[-1]
        with Image.open(feed) as im:
            im = im.convert("L" if _after(rest, "--yuv") == "400" else "RGB")
            im.save(out, "AVIF", quality=int(_after(rest, "-q")), speed=10)
    elif name == "avifdec":
        with Image.open(rest[-2]) as im:
            im.save(rest[-1], "PNG")
    elif name == "jpegtran":
        shutil.copyfile(rest[-1], _after(rest, "-outfile"))
    elif name == "oxipng":
        shutil.copyfile(rest[-1], _after(rest, "--out"))
    else:
        msg = f"fake_run: no fake for tool {args[0]}"
        raise AssertionError(msg)


class FakeScorer:
    made = []  # every construction's path: a test can count them

    def __init__(self, path):
        FakeScorer.made.append(Path(path))
        self.path = Path(path)
        self.threads = ThreadPoolExecutor(vship.SCORE_THREADS, thread_name_prefix="fake-vship")

    def score_files(self, reference, distorted):
        return SCORE[0](Path(reference), Path(distorted))


# What fleet._tools reports for the fake tools (SPEC "State": the tools' versions enter the fleet digest).
FAKE_TOOL_VERSIONS = {"avifenc": "fake", "avifdec": "fake", "jpegtran": "fake", "oxipng": "fake", "vship": "fake"}


def install(setattr):
    """Put the fakes in place (setattr(target, name, value), e.g. monkeypatch.setattr) and create the tool files the
    config names, so the "image tools" preflight check passes."""
    for name in (*tools.NAMES, "vship"):
        path = config.abs_path(config.cached()["tools"][name])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    setattr(tools, "run", fake_run)
    setattr(image, "pool", ThreadPoolExecutor)  # threads: the fake tools reach the workers
    setattr(vship, "Scorer", FakeScorer)
    setattr(fleet, "_tools", lambda _cfg: dict(FAKE_TOOL_VERSIONS))  # the fakes have no version to print
    CALLS.clear()
    SCORE[0] = lambda _ref, _dec: DEFAULT_SCORE
