"""OCR of strip units and the keyword test (SPEC "Junk decisions", candidates; "Preflight", OCR device)."""

import onnxruntime
import pytest
from PIL import Image, ImageDraw, ImageFont

from manga_pipeline import cli, config, preflight
from manga_pipeline.junk import ocr

from tests.builders import problem_text
from tests.timing import budget


def words() -> config.JunkConfig:
    return config.cached()["junk"]


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
