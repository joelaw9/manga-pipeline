"""OCR of strip units and the keyword test (SPEC "Junk decisions", candidates; "Preflight", OCR device)."""

import queue
import threading
import time
from pathlib import Path

import onnxruntime
import pytest
from PIL import Image, ImageDraw, ImageFont

from manga_pipeline import cli, config, images, preflight, strip
from manga_pipeline.junk import ocr
from manga_pipeline.junk import strip_units as su

from tests.builders import problem_text
from tests.timing import budget

JUNK_FIX = Path(__file__).parent / "fixtures_pages" / "junk"


def words() -> ocr.WordLists:
    junk = config.cached()["junk"]
    return {"keywords": junk["keywords"], "weak": junk["weak"], "paired": junk["paired"], "suppress": junk["suppress"]}


@budget()
@pytest.mark.parametrize(
    ("text", "height", "hits"),
    [
        ("Join our DISCORD server", 300, ["discord", "join our"]),
        ("visit  mysite.COM\nnow", 800, [".com"]),
        ("visit mysite.com now", 801, []),  # a weak term on a tall unit is story art, not a notice
        ("I will clean the house", 300, []),  # a paired term alone is dialogue
        ("Translator: A  Clean: B", 300, ["translat", "clean"]),
        ("Translator: A, Head of Localization: B", 300, []),  # an official credit: suppress cancels every hit
    ],
)
def test_keyword_hits_follow_the_weak_paired_and_suppress_rules(text, height, hits):
    assert ocr.text_hits(text, height, words()) == hits, "the keyword rules decide which terms count"


@budget()
def test_weak_and_paired_terms_must_be_keywords():
    with pytest.raises(ValueError, match=r"junk\.paired"):
        ocr.text_hits("x", 10, {**words(), "paired": ["nonsense"]})


@budget()
def test_tall_units_are_read_in_overlapping_tiles():
    parts = ocr.tiles(Image.new("RGB", (1000, 4000)))
    tops = [0, ocr.TILE_PX - ocr.TILE_OVERLAP, 2 * (ocr.TILE_PX - ocr.TILE_OVERLAP)]
    assert [p.height for p in parts] == [ocr.TILE_PX, ocr.TILE_PX, 4000 - tops[2]], "tiles overlap and end at the end"


@budget(load="db", seconds=20.0)
def test_cpu_ocr_reads_a_rendered_notice():
    im = Image.new("RGB", (1200, 220), "white")
    ImageDraw.Draw(im).text((40, 60), "JOIN OUR DISCORD", fill="black", font=ImageFont.load_default(size=80))
    assert "discord" in ocr.normalize(ocr.text(im, "cpu")), "RapidOCR on cpu reads plain printed text"


@budget()
def test_ocr_device_check_names_an_unknown_device():
    assert "not one of" in problem_text(ocr.provider_problem("tpu")), "an unknown device is a problem"


@budget(seconds=5.0)
def test_ocr_device_check_passes_on_cpu():
    assert ocr.provider_problem("cpu") is None, "the CPU provider always loads"


@budget(seconds=5.0)
def test_ocr_device_check_fails_when_the_session_falls_back(monkeypatch):
    class FellBack:
        def __init__(self, *args, **kwargs):
            pass

        def get_providers(self):
            return ["CPUExecutionProvider"]

    monkeypatch.setattr(onnxruntime, "InferenceSession", FellBack)
    monkeypatch.setattr(onnxruntime, "preload_dlls", lambda: None, raising=False)
    problem = ocr.provider_problem("cuda")
    assert "not CUDAExecutionProvider" in problem_text(problem), "a session that fell back to CPU fails the check"


@budget()
def test_process_runs_the_ocr_device_check():
    assert preflight.OCR_DEVICE in cli.COMMANDS["process"].checks, "process checks its OCR device"
    assert preflight.OCR_DEVICE not in cli.COMMANDS["intake"].checks, "intake runs no OCR"


def numbered(n):
    """Return n images whose height is their index + 1, so a patched reader knows which it reads."""
    return [Image.new("RGB", (10, i + 1)) for i in range(n)]


def fresh_engines(monkeypatch, reader):
    """Start the process's engines anew; every engine made reads with `reader`. Returns the list of devices made."""
    made = []

    def new_reader(device):
        made.append(device)
        return reader

    monkeypatch.setattr(ocr, "_engines", {})
    monkeypatch.setattr(ocr, "_new_reader", new_reader)
    return made


@budget(load="fs", seconds=30.0)
@pytest.mark.parametrize("chapter", ["s5_5_bring_the_love_c0095", "s5_2_i_stole_c0006"])
def test_pooled_reads_of_a_real_strip_equal_serial_reads(chapter):
    """A guard, not a red test: units read 4 at once get the text 1 engine reads, on the real cpu engine."""
    path = JUNK_FIX / chapter / "strip.png"
    st = strip.make_strip([path], [images.page_size(path)], [strip.page_rows(images.open_image(path)[0])])
    serial = su.scan(st, lambda ims: ocr.texts(ims, "cpu", 1))
    pooled = su.scan(st, lambda ims: ocr.texts(ims, "cpu", 4))
    assert sum("text" in u for b in serial["blocks"] for u in [b, *b["joins"]]) > 1, "several units are read"
    assert pooled == serial, "the pool reads each unit's text as 1 engine does"


@budget()
def test_texts_come_back_in_input_order_when_reads_finish_out_of_order(monkeypatch):
    def slow_first(im, _device):
        time.sleep(0.01 * (5 - im.height))  # the first image's read ends last
        return f"unit {im.height}"

    monkeypatch.setattr(ocr, "text", slow_first)
    assert ocr.texts(numbered(5), "cpu", 4) == [f"unit {i}" for i in range(1, 6)], "texts follow the images' order"


@budget()
def test_engines_are_made_once_per_process_up_to_the_most_workers_asked(monkeypatch):
    both = threading.Barrier(2, timeout=5)

    def read(_array):
        both.wait()  # passes only while 2 engines read at once
        return ["x"]

    made = fresh_engines(monkeypatch, read)
    assert ocr.texts(numbered(4), "cpu", 2) == ["x"] * 4
    assert made == ["cpu", "cpu"], "2 workers read on 2 engines at once"
    assert ocr.texts(numbered(4), "cpu", 2) == ["x"] * 4
    assert made == ["cpu", "cpu"], "a later call reuses the process's engines"


@budget()
def test_more_workers_make_more_engines_while_one_is_idle(monkeypatch):
    both = threading.Barrier(2, timeout=5)
    gate = []

    def read(_array):
        if gate:
            both.wait()  # passes only while 2 engines read at once
        return ["x"]

    class SlowQueue(queue.Queue):
        def get(self, *args, **kwargs):
            time.sleep(0.1)  # a waiting taker is slow to take: the other thread checks the queue meanwhile
            return super().get(*args, **kwargs)

    made = fresh_engines(monkeypatch, read)
    engines = {}
    monkeypatch.setattr(ocr, "_engines", engines)
    assert ocr.texts(numbered(1), "cpu", 1) == ["x"], "1 engine made and left idle"
    slow = SlowQueue()
    slow.put(engines["cpu"].idle.get())
    engines["cpu"].idle = slow
    gate.append(1)
    assert ocr.texts(numbered(2), "cpu", 2) == ["x"] * 2
    assert made == ["cpu", "cpu"], "the second engine is made beside the idle one"


@budget()
def test_an_interrupted_wait_cancels_the_reads_not_started(monkeypatch):
    read = []

    def slow(im, _device):
        read.append(im.height)
        time.sleep(0.02)
        return ""

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ocr, "text", slow)
    monkeypatch.setattr(ocr, "wait", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ocr.texts(numbered(20), "cpu", 1)
    assert len(read) < 20, f"reads not started are cancelled: {read}"


@budget()
def test_a_failed_read_returns_its_engine(monkeypatch):
    calls = []

    def read(_array):
        calls.append(1)
        if len(calls) == 1:
            msg = "out of memory"
            raise RuntimeError(msg)
        return ["x"]

    made = fresh_engines(monkeypatch, read)
    with pytest.raises(RuntimeError, match="out of memory"):
        ocr.text(Image.new("RGB", (10, 10)), "cpu")
    assert ocr.text(Image.new("RGB", (10, 10)), "cpu") == "x", "the engine is free again after a failed read"
    assert made == ["cpu"], "no engine is lost to a failed read"


@budget()
def test_a_failed_read_names_its_image_and_cancels_the_reads_not_started(monkeypatch):
    read = []

    def fail_second(im, _device):
        read.append(im.height)
        if im.height == 2:
            msg = "bad tile"
            raise RuntimeError(msg)
        time.sleep(0.05)
        return ""

    monkeypatch.setattr(ocr, "text", fail_second)
    with pytest.raises(ocr.OcrError, match="image 1 failed: RuntimeError: bad tile") as e:
        ocr.texts(numbered(20), "cpu", 2)
    assert e.value.index == 1, "the failed image's index"
    assert len(read) < 20, f"reads not started are cancelled: {read}"


@budget()
@pytest.mark.parametrize(("device", "workers"), [("cpu", 0), ("dml", 2)])
def test_texts_refuse_workers_below_1_and_above_1_on_dml(device, workers):
    with pytest.raises(ValueError, match="at least 1, and 1 on dml"):
        ocr.texts([], device, workers)


@budget(load="fs")
def test_a_failed_read_fails_the_strip_scan_naming_the_units_rows():
    path = JUNK_FIX / "s5_2_i_stole_c0006" / "strip.png"
    st = strip.make_strip([path], [images.page_size(path)], [strip.page_rows(images.open_image(path)[0])])
    last = su.scan(st)["blocks"][-1]  # a block is read after its joins: the last image read is the last block

    def fail_last(ims):
        msg = "bad tile"
        raise ocr.OcrError(len(ims) - 1, RuntimeError(msg))

    with pytest.raises(RuntimeError, match=f"strip rows {last['start']}-{last['end']} failed: RuntimeError: bad tile"):
        su.scan(st, fail_last)
