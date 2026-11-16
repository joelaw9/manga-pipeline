"""The chapter plan and its execution (convert.plan, convert.image; SPEC "Conversion"): routes, chroma, feeds, gates,
retries, the lossless path, drops, rendered pages, and the record. Tools and scorer are the fakes
(tests/fakes/convert.py): avifenc writes a real AVIF with Pillow, jpegtran and oxipng copy."""

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import archive, images
from manga_pipeline.convert import covers, image, pages, plan, tools

from tests import pages as fx
from tests.convert_pages import NO_EDIT, Chapter, colour, gray, jpeg, png, webp
from tests.fakes import convert as fake
from tests.timing import budget


def calls(tool):
    return [args for name, args in fake.CALLS if name == tool]


def score_by_q(scores):
    """A fake score from the encode quality in the decode's name (<stem>.q<Q>.png): scores[q], else 90."""

    def f(_ref, dec):
        return scores.get(int(re.search(r"\.q(\d+)\.png$", dec.name).group(1)), 90.0)

    return f


# ---------------------------------------------------------------- route, chroma, feeds


@budget(load="pure")
@pytest.mark.parametrize(
    ("fmts", "want"),
    [
        ({images.JPEG}, "avif"),
        ({images.JPEG, images.PNG, images.WEBP_LOSSLESS}, "avif"),
        ({images.JPEG, images.WEBP_LOSSY}, "avif-or-untouched"),
        ({images.WEBP_LOSSY}, "untouched"),
        ({images.GIF, images.AVIF, images.JXL}, "untouched"),
        ({images.WEBP_ANIMATED}, "untouched"),
        ({images.JPEG, images.GIF}, "untouched-flag"),
        ({images.JPEG, images.BMP}, "untouched-flag"),
    ],
)
def test_route_per_format_mix(fmts, want):
    assert plan.route(fmts) == want, "the bulk converter's route table"


@budget(load="pure")
def test_yuv_and_quality_group():
    assert [plan.yuv_for(c, "420") for c in (images.GRAY, images.TINTED, images.GRAY_MARK, images.COLOUR)] == [
        "400",
        "400",
        "400",
        "420",
    ], "grayish classes encode 4:0:0, colour keeps the source chroma"
    assert (plan.quality_group("400"), plan.quality_group("444")) == ("gray", "colour")


@budget(load="pure")
def test_only_a_colour_encode_of_a_feedable_jpeg_feeds_the_jpeg():
    q = {"format": images.JPEG, "jpeg_feed": True}
    assert plan.feeds_jpeg(q, "420")
    assert not plan.feeds_jpeg(q, "400")
    assert not plan.feeds_jpeg({"format": images.PNG, "jpeg_feed": False}, "444")


@budget(load="pure")
def test_jpeg_feed_and_gray_read_the_header():
    rgb = jpeg(colour())
    cmyk = jpeg(colour().convert("CMYK"))
    assert pages.jpeg_feed(rgb), "a plain YCbCr JPEG is fed as is and can be grayed"
    assert pages.jpeg_gray(rgb), "a plain YCbCr JPEG is fed as is and can be grayed"
    assert not pages.jpeg_feed(cmyk), "avifenc refuses CMYK; jpegtran cannot gray it"
    assert not pages.jpeg_gray(cmyk), "avifenc refuses CMYK; jpegtran cannot gray it"
    exif = Image.Exif()
    exif[0x0112] = 6
    turned = jpeg_with_exif(colour(), exif)
    assert not pages.jpeg_feed(turned), "avifenc would apply the rotation open_image does not"


def jpeg_with_exif(im, exif):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, exif=exif.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- the edit and the plan


@budget(load="pure")
@pytest.mark.parametrize(
    ("edit", "match"),
    [
        ({**NO_EDIT, "extra": 1}, "keys"),
        ({k: v for k, v in NO_EDIT.items() if k != "split"}, "keys"),
        ({**NO_EDIT, "split": 0}, "split must be a bool"),
        ({**NO_EDIT, "drop_pages": ["1"]}, "integers"),
        ({**NO_EDIT, "bands": [{"page": 1, "box": [0, 0, 10]}]}, "bands"),
        ({**NO_EDIT, "join": [[1]]}, "join"),
        ({**NO_EDIT, "join": [[1, 2, 3]]}, "pairs"),
        ({**NO_EDIT, "drop_pages": [1, 1]}, "twice"),
        ({**NO_EDIT, "strip_removed": [{"start": 1}]}, "strip_removed entries"),
        ({**NO_EDIT, "strip_removed": [{"start": 1, "end": 2}]}, "strip_removed entries"),
        ({**NO_EDIT, "strip_removed": [{"start": 1, "end": 2, "why": 3}]}, "strip_removed entries"),
        ({**NO_EDIT, "strip_removed": [{"start": 5, "end": 5, "why": "match"}]}, "0 <= start < end"),
        ({**NO_EDIT, "strip_removed": [{"start": -1, "end": 5, "why": "match"}]}, "0 <= start < end"),
        ({**NO_EDIT, "blank": [{"page": 0, "box": [0, 0, 1, 1]}]}, "blank entries"),
        ({**NO_EDIT, "blank": [{"page": 0, "box": [0, 0, 1], "fill": [1, 2, 3]}]}, "blank entries"),
        ({**NO_EDIT, "blank": [{"page": 0, "box": [0, 0, 1, 1], "fill": [1, 2, 3.5]}]}, "integers"),
    ],
)
def test_a_malformed_edit_is_refused_naming_what_is_wrong(edit, match):
    with pytest.raises(plan.EditError, match=match):
        plan.check_edit(json.dumps(edit))


@budget(load="pure")
def test_a_well_formed_edit_passes():
    edit = {**NO_EDIT, "drop_pages": [0], "bands": [{"page": 1, "box": [0, 2, 10, 20]}], "join": [[2, 3]]}
    assert plan.check_edit(json.dumps(edit)) == edit


@budget(load="pool")
def test_an_exif_rotated_page_needs_a_human(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 8
    ch = Chapter(tmp_path, [("001.jpg", jpeg_with_exif(colour(), exif))])
    with pytest.raises(plan.NeedsHuman, match="EXIF orientation 8"):
        ch.plan()


@budget(load="pool")
def test_pages_are_extracted_under_the_bulk_converters_names(tmp_path):
    ch = Chapter(tmp_path, [("p1.jpg", jpeg(colour())), ("x/p10.jpeg", jpeg(colour(seed=3))), ("p2.png", png(gray()))])
    assert [f.name for f in ch.files] == ["001.jpg", "002.png", "003.jpeg"], "001, 002, ... in reading order"


@budget(load="pure")
def test_cover_names_are_kept_or_prefixed_to_keep_the_order():
    assert pages.target_names([("000_cover.jpg", images.JPEG), ("a.jpg", images.JPEG)]) == ["000_cover.jpg", "001.jpg"]
    got = pages.target_names([("a.jpg", images.JPEG), ("Ch1_cover.jpg", images.JPEG), ("b.jpg", images.JPEG)])
    assert got == ["001.jpg", "001-Ch1_cover.jpg", "002.jpg"], "a cover inside the chapter keeps its place"


@budget(load="pool")
def test_2_pages_whose_names_share_a_stem_are_not_extracted(tmp_path):
    cbz = tmp_path / "c.cbz"
    archive.write(cbz, [("cover.jpg", jpeg(colour())), ("cover.png", png(gray()))])
    paths, problems = pages.extract(cbz, tmp_path / "x")
    assert paths == []
    assert "share a name stem" in problems[0]
    assert not (tmp_path / "x").exists()


@budget(load="pool")
def test_an_edit_naming_a_page_the_chapter_lacks_is_refused(tmp_path):
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour()))])
    with pytest.raises(plan.EditError, match="names page 5"):
        ch.plan({**NO_EDIT, "drop_pages": [5]})


# ---------------------------------------------------------------- AVIF page route


@budget(load="pool")
def test_colour_jpeg_chapter_becomes_avif_fed_as_is_at_its_chroma(tmp_path):
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour())), ("002.jpg", jpeg(colour(seed=4)))])
    p = ch.plan()
    rec = ch.run(p)
    assert (p.route, rec["outcome"]) == ("avif", "avif")
    assert [o["name"] for o in rec["output"]] == ["001.avif", "002.avif"], "pages keep their stem"
    enc = calls("avifenc")
    assert all(a[a.index("--yuv") + 1] == "420" for a in enc), "a 4:2:0 JPEG keeps 4:2:0"
    assert all("--ignore-exif" in a and a[-2].endswith(".jpg") for a in enc), "a feedable colour JPEG is fed as is"
    assert rec["scores"] == {"001.avif": 90.0, "002.avif": 90.0}
    assert rec["mean_score"] == 90.0
    assert rec["gray"] is False


@budget(load="pool")
def test_gray_png_chapter_encodes_400_from_the_gray_reference(tmp_path):
    ch = Chapter(tmp_path, [("001.png", png(gray().convert("RGB")))])
    rec = ch.run(ch.plan())
    a = calls("avifenc")[0]
    assert a[a.index("--yuv") + 1] == "400", "gray pages: 4:0:0 of the gray version"
    assert a[-2].endswith(".gray.png"), "gray pages: 4:0:0 of the gray version"
    assert rec["gray"] is True


@budget(load="pool")
def test_each_page_encodes_at_its_groups_quality(tmp_path):
    ch = Chapter(tmp_path, [("001.png", png(gray().convert("RGB"))), ("002.png", png(colour()))])
    ch.run(ch.plan(quality={"gray": 65, "colour": 75}))
    got = {Path(a[-1]).stem: a[a.index("-q") + 1] for a in calls("avifenc")}
    assert got == {"001": "65", "002": "75"}


@budget(load="pool")
def test_a_page_under_the_floor_retries_once_at_q90(tmp_path, monkeypatch):
    monkeypatch.setattr(image, "MAX_SIZE_RATIO", 100.0)  # the gate under test is the floor, not the size
    fake.SCORE[0] = score_by_q({80: 79.994, 90: 85.0})
    ch = Chapter(tmp_path, [("001.png", png(colour()))])
    rec = ch.run(ch.plan())
    assert rec["outcome"] == "avif"
    assert rec["avif_pages"][0]["retried"]
    assert rec["avif_pages"][0]["q"] == 90
    assert rec["avif_pages"][0]["score"] == 85.0


@budget(load="pool")
def test_a_page_failing_after_its_retry_sends_the_chapter_to_lossless(tmp_path):
    fake.SCORE[0] = score_by_q({80: 70.0, 90: 75.0})
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour())), ("002.png", png(gray()))])
    rec = ch.run(ch.plan())
    assert rec["outcome"] == "lossless"
    assert "scored 75.0 at q90" in rec["avif_rejected"]
    assert [o["name"] for o in rec["output"]] == ["001.jpg", "002.png"]
    assert rec["mean_score"] is None
    assert rec["scores"] == {}
    assert not list(ch.work.glob("*.avif")), "the rejected attempt's AVIF pages are removed"


@budget(load="pool")
def test_the_size_gate_rejects_avif_over_the_ratio(tmp_path, monkeypatch):
    monkeypatch.setattr(image, "MAX_SIZE_RATIO", 0.0)
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour()))])
    rec = ch.run(ch.plan())
    assert rec["outcome"] == "lossless"
    assert "of original > 0%" in rec["avif_rejected"]


@budget(load="pool")
def test_lossy_webp_chapter_stays_untouched_byte_for_byte(tmp_path):
    data = webp(colour())
    ch = Chapter(tmp_path, [("001.webp", data)])
    p = ch.plan()
    rec = ch.run(p)
    assert (p.route, rec["outcome"]) == ("untouched", "untouched")
    assert not calls("avifenc")
    assert (ch.out / "001.webp").read_bytes() == data


@budget(load="pool")
def test_jpeg_webp_mix_failing_the_gate_stays_untouched(tmp_path):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour())), ("002.webp", webp(colour(seed=5)))])
    rec = ch.run(ch.plan())
    assert rec["outcome"] == "untouched"
    assert [o["name"] for o in rec["output"]] == ["001.jpg", "002.webp"]


@budget(load="pool")
def test_dropped_pages_are_left_out(tmp_path):
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour())), ("002.jpg", jpeg(colour(seed=6)))])
    rec = ch.run(ch.plan({**NO_EDIT, "drop_pages": [1]}))
    assert [o["name"] for o in rec["output"]] == ["001.avif"]
    assert rec["dropped"] == ["002.jpg"]
    assert rec["pages"] == 1


# ---------------------------------------------------------------- lossless path


@budget(load="pool")
def test_lossless_output_not_smaller_keeps_the_source_bytes(tmp_path):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    data = jpeg(colour())
    ch = Chapter(tmp_path, [("001.jpg", data)])
    rec = ch.run(ch.plan())
    assert rec["lossless_pages"][0]["verify"] == "exact"
    assert (ch.out / "001.jpg").read_bytes() == data


@budget(load="pool")
def test_a_lossless_pixel_mismatch_names_the_page(tmp_path, monkeypatch):
    fake.SCORE[0] = lambda _ref, _dec: 50.0

    def bad_jpegtran(*args):
        args = [str(a) for a in args]
        if Path(args[0]).name.startswith("jpegtran"):
            Path(args[args.index("-outfile") + 1]).write_bytes(jpeg(gray()))
        else:
            fake.fake_run(*args)

    monkeypatch.setattr(tools, "run", bad_jpegtran)
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour()))])
    with pytest.raises(RuntimeError, match=r"lossless verification failed: 001\.jpg \(exact"):
        ch.run(ch.plan())


@budget(load="pool")
def test_gray_jpeg_on_the_lossless_path_is_grayscaled(tmp_path):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    ch = Chapter(tmp_path, [("001.jpg", jpeg(gray().convert("RGB")))])
    ch.run(ch.plan())
    assert "-grayscale" in calls("jpegtran")[0], "a gray YCbCr JPEG loses its chroma losslessly"


@budget(load="pure")
def test_tool_error_carries_stderr():
    err = tools.ToolError(1, ["avifenc"], b"", b"bad input")
    assert "stderr: bad input" in str(err)


# ---------------------------------------------------------------- rendered pages


@budget(load="pool")
def test_crop_and_join_render_numbered_avif_pages(tmp_path):
    ch = Chapter(
        tmp_path,
        [("001.jpg", jpeg(colour())), ("002.jpg", jpeg(colour((96, 40), seed=7))), ("003.jpg", jpeg(colour(seed=8)))],
    )
    edit = {**NO_EDIT, "bands": [{"page": 2, "box": [0, 0, 96, 100]}], "join": [[0, 1]]}
    p = ch.plan(edit)
    rec = ch.run(p)
    assert p.route == plan.RENDERED
    assert rec["outcome"] == "avif"
    assert [o["name"] for o in rec["output"]] == ["001.avif", "002.avif"]
    assert [r["src"] for r in p.render] == [["001.jpg", "002.jpg"], ["003.jpg"]]
    assert rec["reshape"] == {"kind": "rejoin", "pages_in": 3, "pages_out": 2}
    with Image.open(ch.rendered_dir / "001.png") as im:
        assert im.size == (96, 168), "the sliver is joined under its page"


@budget(load="pool")
def test_a_rendered_chapter_failing_the_floor_keeps_its_original_pages_minus_drops(tmp_path):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour())), ("002.jpg", jpeg(colour(seed=9))), ("003.jpg", jpeg(gray()))])
    rec = ch.run(ch.plan({**NO_EDIT, "drop_pages": [2], "bands": [{"page": 0, "box": [0, 0, 96, 64]}]}))
    assert rec["outcome"] == "untouched"
    assert [o["name"] for o in rec["output"]] == ["001.jpg", "002.jpg"]


@budget(load="pool")
def test_strip_chapter_restitches_and_keeps_a_cover_page_first(tmp_path):
    tall = [jpeg(colour((200, 1400), seed=s)) for s in range(3)]
    members = [("000_cover.jpg", jpeg(colour((200, 300), seed=9)))] + [
        (f"{i + 1:03d}.jpg", d) for i, d in enumerate(tall)
    ]
    ch = Chapter(tmp_path, members)
    p = ch.plan(layout="strip")
    rec = ch.run(p)
    assert p.edits["strip"]["pages"][0]["name"] == "001.jpg", "the cover-named page is not in the strip"
    names = [o["name"] for o in rec["output"]]
    assert names[0] == "000_cover.avif"
    assert names[1:] == [f"{n:03d}.avif" for n in range(1, len(names))]
    heights = [Image.open(ch.rendered_dir / f"{n:03d}.png").height for n in range(1, len(names))]
    assert sum(heights) == 4200, "cut into 1500-5000 px pages"
    assert all(1500 <= h <= 5000 for h in heights), "cut into 1500-5000 px pages"


@budget(load="pool")
def test_strip_removed_rows_are_cut_out(tmp_path):
    ch = Chapter(tmp_path, [(f"{i:03d}.jpg", jpeg(colour((200, 1400), seed=i))) for i in range(1, 4)])
    p = ch.plan({**NO_EDIT, "strip_removed": [{"start": 3900, "end": 4200, "why": "credits"}]}, layout="strip")
    assert p.edits["strip"]["kept"] == [(0, 3900)]
    assert p.edits["strip"]["cuts"][-1] == 3900


@budget(load="pool")
def test_a_split_chapter_without_a_page_aspect_takes_the_height_its_images_imply(tmp_path):
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour((100, 140)))), ("002.jpg", jpeg(colour((100, 420), seed=3)))])
    p = ch.plan({**NO_EDIT, "split": True})
    assert len(p.render) == 4, "the smallest image is 1 page: the 420 px one holds 3"


@budget(load="pool")
def test_a_split_chapter_is_cut_at_the_series_page_aspect(tmp_path):
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour((100, 280)))), ("002.jpg", jpeg(colour((100, 280), seed=3)))])
    p = ch.plan({**NO_EDIT, "split": True}, page_aspect=[1.4])
    rec = ch.run(p)
    assert rec["reshape"]["kind"] == "split", "2 stitched pages of 2 pages each"
    assert len(rec["output"]) == 4, "2 stitched pages of 2 pages each"


@budget(load="pure")
def test_a_cover_inside_the_chapter_is_a_name_problem():
    names = pages.target_names([("a.jpg", images.JPEG), ("Ch1_cover.jpg", images.JPEG), ("b.jpg", images.JPEG)])
    assert pages.name_problems(names) == ["the renamed pages sort differently by natural and by plain order"]
    leading = pages.target_names([("cover.jpg", images.JPEG), ("a.jpg", images.JPEG)])
    assert leading == ["000-cover.jpg", "001.jpg"], "a leading cover is fine"
    assert pages.name_problems(leading) == [], "a leading cover is fine"


@budget(load="fs")
@pytest.mark.parametrize("join", [[[0, 0]], [[0, 1], [1, 0]], [[0, 1], [0, 2]], [[0, 2], [1, 2]]])
def test_join_pairs_that_do_not_chain_are_refused(tmp_path, join):
    ch = Chapter(tmp_path, [(f"{i:03d}.jpg", jpeg(colour(seed=i))) for i in range(3)])
    with pytest.raises(plan.EditError, match="join pairs"):
        ch.plan({**NO_EDIT, "join": join})


@budget(load="pool")
def test_a_join_of_a_dropped_or_cover_page_is_refused(tmp_path):
    ch = Chapter(
        tmp_path,
        [("000_cover.jpg", jpeg(colour(seed=9)))] + [(f"{i:03d}.jpg", jpeg(colour(seed=i))) for i in range(1, 3)],
    )
    with pytest.raises(plan.EditError, match="dropped or cover-named"):
        ch.plan({**NO_EDIT, "join": [[0, 1]]})
    with pytest.raises(plan.EditError, match="dropped or cover-named"):
        ch.plan({**NO_EDIT, "drop_pages": [1], "join": [[1, 2]]})


@budget(load="pool")
@pytest.mark.parametrize(
    ("drop", "bands", "match"),
    [
        ([0], [], "cover-named"),  # page 0 is the cover
        ([], [{"page": 0, "box": [0, 0, 96, 100]}], "cover-named"),
        ([], [{"page": 1, "box": [0, 0, 96, 100]}, {"page": 1, "box": [0, 20, 96, 128]}], "twice or a dropped page"),
        ([1], [{"page": 1, "box": [0, 0, 96, 100]}], "twice or a dropped page"),
        ([], [{"page": 1, "box": [0, 0, 97, 100]}], "not inside page 1"),
        ([], [{"page": 1, "box": [0, 100, 96, 100]}], "not inside page 1"),
    ],
)
def test_a_drop_or_band_the_junk_stage_never_writes_is_refused(tmp_path, drop, bands, match):
    members = [("000_cover.jpg", jpeg(colour(seed=9))), ("001.jpg", jpeg(colour())), ("002.jpg", jpeg(colour(seed=3)))]
    ch = Chapter(tmp_path, members)
    with pytest.raises(plan.EditError, match=match):
        ch.plan({**NO_EDIT, "drop_pages": drop, "bands": bands})


@budget(load="pool")
def test_removed_rows_past_the_end_of_the_strip_are_refused(tmp_path):
    ch = Chapter(tmp_path, [(f"{i:03d}.jpg", jpeg(colour((200, 1400), seed=i))) for i in range(1, 4)])
    with pytest.raises(plan.EditError, match="past the strip's 4200 rows"):
        ch.plan({**NO_EDIT, "strip_removed": [{"start": 3900, "end": 4300, "why": "match"}]}, layout="strip")


@budget(load="pool")
def test_removed_rows_of_a_strip_chapter_with_only_a_cover_page_are_refused(tmp_path):
    ch = Chapter(tmp_path, [("000_cover.jpg", jpeg(colour(seed=9)))])
    with pytest.raises(plan.EditError, match="past the strip's 0 rows"):
        ch.plan({**NO_EDIT, "strip_removed": [{"start": 0, "end": 100, "why": "match"}]}, layout="strip")


@budget(load="pool")
def test_a_page_in_3_pieces_joins_its_2_pairs_into_1_page(tmp_path):
    ch = Chapter(tmp_path, [(f"{i:03d}.jpg", jpeg(colour((96, 40), seed=i))) for i in range(3)])
    p = ch.plan({**NO_EDIT, "join": [[0, 1], [1, 2]]})
    assert [r["src"] for r in p.render] == [["001.jpg", "002.jpg", "003.jpg"]]


# ---------------------------------------------------------------- logo blanks

BOX, FILL = [10, 10, 40, 30], [200, 30, 30]
BLANK = {**NO_EDIT, "blank": [{"page": 0, "box": BOX, "fill": FILL}]}


def filled(path, box=BOX, fill=FILL):
    """Is box of the PNG at path exactly fill?"""

    with Image.open(path) as im:
        a = np.asarray(im.convert("RGB"))
    x0, y0, x1, y1 = box
    return bool((a[y0:y1, x0:x1] == fill).all())


@budget(load="pool")
@pytest.mark.parametrize(
    ("blank", "drop", "match"),
    [
        ({"page": 1, "box": BOX, "fill": FILL}, [1], "repeated, dropped or cover-named"),
        ({"page": 0, "box": BOX, "fill": FILL}, [], "repeated, dropped or cover-named"),  # page 0 is the cover
        ({"page": 1, "box": [0, 0, 97, 10], "fill": FILL}, [], "not inside page 1"),
        ({"page": 1, "box": [10, 10, 10, 20], "fill": FILL}, [], "not inside page 1"),
        ({"page": 1, "box": BOX, "fill": [0, 0, 256]}, [], "not an RGB colour"),
    ],
)
def test_a_blank_that_cannot_apply_is_refused(tmp_path, blank, drop, match):
    ch = Chapter(tmp_path, [("000_cover.jpg", jpeg(colour(seed=9))), ("001.jpg", jpeg(colour()))])
    with pytest.raises(plan.EditError, match=match):
        ch.plan({**NO_EDIT, "drop_pages": drop, "blank": [blank]})


@budget(load="pool")
def test_2_blanks_on_1_page_are_refused(tmp_path):
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour()))])
    with pytest.raises(plan.EditError, match="repeated"):
        ch.plan({**NO_EDIT, "blank": BLANK["blank"] * 2})


@budget(load="pool")
def test_a_blanked_page_is_encoded_from_its_blanked_pixels_and_keeps_its_name_and_source_bytes(tmp_path):
    data = jpeg(colour())
    ch = Chapter(tmp_path, [("001.jpg", data), ("002.jpg", jpeg(colour(seed=4)))])
    rec = ch.run(ch.plan(BLANK))
    assert rec["outcome"] == "avif"
    assert rec["blanked"] == ["001.jpg"]
    blanked = ch.root / "blank" / "001.png"
    assert filled(blanked), "the box is filled with the fill colour"
    by_page = {Path(a[-1]).stem: a for a in calls("avifenc")}
    assert by_page["001"][-2].endswith("001.ref.png"), "no JPEG feed"
    assert "--ignore-exif" not in by_page["001"], "no JPEG feed"
    assert by_page["002"][-2].endswith("002.jpg"), "an unblanked page keeps its JPEG feed"
    first = rec["avif_pages"][0]
    assert (first["name"], first["bytes_in"], first["fmt"]) == ("001.jpg", len(data), image.RENDERED_FMT)
    assert [o["name"] for o in rec["output"]] == ["001.avif", "002.avif"]


@budget(load="pool")
def test_a_blank_is_not_applied_on_the_lossless_or_untouched_outcomes(tmp_path):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    data = jpeg(colour())
    ch = Chapter(tmp_path, [("001.jpg", data)])
    rec = ch.run(ch.plan(BLANK))
    assert rec["outcome"] == "lossless"
    assert rec["blanked"] == []
    assert pages.pixels_match(data, (ch.out / "001.jpg").read_bytes(), "exact")[0], "the logo is kept"


@budget(load="pool")
def test_a_chapter_whose_first_outcome_is_not_avif_is_never_blanked(tmp_path):
    data = webp(colour())
    ch = Chapter(tmp_path / "w", [("001.webp", data)])
    rec = ch.run(ch.plan(BLANK))
    assert rec["outcome"] == "untouched"
    assert rec["blanked"] == []
    assert not (ch.root / "blank").exists()
    assert (ch.out / "001.webp").read_bytes() == data


@budget(load="pool")
def test_a_rendered_chapter_is_rendered_from_its_blanked_page(tmp_path):
    ch = Chapter(tmp_path, [("001.jpg", jpeg(colour())), ("002.jpg", jpeg(colour(seed=4)))])
    edit = {**BLANK, "bands": [{"page": 0, "box": [0, 0, 96, 100]}]}
    rec = ch.run(ch.plan(edit))
    assert rec["outcome"] == "avif"
    assert rec["blanked"] == ["001.jpg"]
    assert filled(ch.rendered_dir / "001.png"), "the render reads the blanked page"


@budget(load="pool")
def test_a_strip_chapter_is_blanked_before_it_is_stacked(tmp_path):
    ch = Chapter(tmp_path, [(f"{i:03d}.jpg", jpeg(colour((200, 1400), seed=i))) for i in range(1, 4)])
    p = ch.plan({**BLANK, "blank": [{"page": 1, "box": [10, 10, 40, 30], "fill": FILL}]}, layout="strip")
    rec = ch.run(p)
    assert rec["blanked"] == ["002.jpg"]
    top = p.edits["strip"]["pages"][1]["top"]  # page 002 starts here in the stacked strip
    with Image.open(ch.rendered_dir / "001.png") as first:
        h = first.height
    assert top + 30 <= h
    assert filled(ch.rendered_dir / "001.png", [10, top + 10, 40, top + 30])


@budget(load="pool")
def test_a_blanked_page_keeps_its_alpha(tmp_path):
    im = colour().convert("RGBA")
    im.putalpha(128)
    ch = Chapter(tmp_path, [("001.png", png(im))])
    p = ch.plan({**NO_EDIT, "blank": [{"page": 0, "box": BOX, "fill": FILL}]})
    blanked = image.blank_pages(p, ch.x, tmp_path / "blank")
    with Image.open(blanked["001.png"]) as out:
        assert out.mode == "RGBA", "alpha kept"
        assert out.getchannel("A").getextrema() == (128, 128), "alpha kept"
    assert filled(blanked["001.png"])


@budget(load="pure")
def test_a_luma_check_refuses_pages_that_differ_only_in_alpha():
    page = gray()
    opaque = page.convert("LA")
    holed = page.convert("LA")
    a = np.full((page.height, page.width), 255, np.uint8)
    a[:32] = 0
    holed.putalpha(Image.fromarray(a))
    assert pages.pixels_match(png(opaque), png(opaque), "luma")[0], "the same gray page with its alpha matches"
    assert not pages.pixels_match(png(opaque), png(holed), "luma")[0], (
        "a lossless page whose alpha changed is not lossless: the luma check compares alpha too"
    )


# ---------------------------------------------------------------- page modes: the lossless and AVIF decisions
# The modes and their planned class, jpeg_feed and the routes they reach are the bulk converter's image matrix
# (avif-conversion tests/test_image_matrix.py PAGES and its jpeg_feed assertion); the tool, its arguments, jpeg_gray,
# keep_icc and the verify mode follow from its image.lossless_page / encode_page, which convert.image copies. The fakes
# do not run the real tools: what they make of these arguments is the convert sample check's.


def _matrix_colour():
    return fx.webtoon_page((96, 128), seed=3)


def _matrix_gray():
    return fx.manga_page((96, 128), seed=2, noise=3).convert("L")


def _gray16():
    a = np.asarray(_matrix_gray()).astype(np.uint16) * 257
    im = Image.fromarray(a)  # uint16 gray: Pillow's I;16
    assert im.mode == "I;16", im.mode
    return png(im)


def _transparent(im):
    """im with rows 0-31 fully transparent over black and rows 32-47 half transparent (the matrix's with_alpha)."""

    a = np.full(im.size[::-1], 255, np.uint8)
    rgb = np.asarray(im).copy()
    a[:32], a[32:48] = 0, 128
    rgb[:32] = 0
    return Image.merge("LA" if im.mode == "L" else "RGBA", [*Image.fromarray(rgb).split(), Image.fromarray(a)])


def _gray_icc():
    return (fx.PAGE_FIX / "icc_gray_azumi.icc").read_bytes()


# mode: (page bytes, extension, class, jpeg_feed, jpeg_gray,
#        AVIF (--yuv, what avifenc reads: "jpeg" the page itself, "ref" our decode, "gray" its gray version),
#        lossless (tool, its arguments before the output, the verify mode))
MATRIX = {
    "cmyk-jpeg-colour": (
        lambda: fx.jpeg(_matrix_colour().convert("CMYK")),
        ".jpg",
        images.COLOUR,
        False,
        False,
        ("444", "ref"),
        ("jpegtran", ["-copy", "none", "-optimize", "-progressive"], "exact"),
    ),
    "cmyk-jpeg-gray": (
        lambda: fx.jpeg(_matrix_gray().convert("RGB").convert("CMYK")),
        ".jpg",
        images.GRAY,
        False,
        False,
        ("400", "gray"),
        ("jpegtran", ["-copy", "none", "-optimize", "-progressive"], "exact"),
    ),
    "gray-icc-l-jpeg": (
        lambda: fx.jpeg(_matrix_gray(), icc_profile=_gray_icc()),
        ".jpg",
        images.GRAY,
        False,
        True,
        ("400", "gray"),
        ("jpegtran", ["-grayscale", "-copy", "icc", "-optimize", "-progressive"], "luma"),
    ),
    "gray-icc-rgb-jpeg": (
        lambda: fx.jpeg(_matrix_colour(), icc_profile=_gray_icc()),
        ".jpg",
        images.COLOUR,
        False,
        True,
        ("420", "ref"),
        ("jpegtran", ["-copy", "icc", "-optimize", "-progressive"], "exact"),
    ),
    "gray16-png": (
        _gray16,
        ".png",
        images.GRAY,
        False,
        False,
        ("400", "gray"),
        ("oxipng", ["-o", "4", "--strip", "safe", "-t", "1"], "luma"),
    ),
    "la-transparent-png": (
        lambda: png(_transparent(_matrix_gray())),
        ".png",
        images.GRAY,
        False,
        False,
        ("400", "gray"),
        ("oxipng", ["-o", "4", "--strip", "safe", "-t", "1"], "luma"),
    ),
    "rgba-transparent-png": (
        lambda: png(_transparent(_matrix_colour())),
        ".png",
        images.COLOUR,
        False,
        False,
        ("444", "ref"),
        ("oxipng", ["-o", "4", "--strip", "safe", "-t", "1"], "exact"),
    ),
}


@budget(load="pool")
@pytest.mark.parametrize("mode", list(MATRIX))
def test_a_page_mode_gets_the_bulk_converters_lossless_and_avif_decisions(tmp_path, monkeypatch, mode):
    make, ext, cls, feed, can_gray, (yuv, reads), (tool, args, verify) = MATRIX[mode]
    data, name = make(), "001" + ext
    monkeypatch.setattr(image, "MAX_SIZE_RATIO", 100.0)  # the decisions under test, not the size gate
    ch = Chapter(tmp_path / "avif", [(name, data)])
    p = ch.plan()
    [entry] = p.pages
    assert (p.classes[name], entry["jpeg_feed"], entry["jpeg_gray"]) == (cls, feed, can_gray), (
        f"{mode}: planned class, jpeg_feed, jpeg_gray"
    )
    rec = ch.run(p)
    [enc] = calls("avifenc")
    fed = Path(enc[-2]).name
    assert rec["outcome"] == "avif", f"{mode}: AVIF at --yuv {yuv}"
    assert enc[enc.index("--yuv") + 1] == yuv, f"{mode}: AVIF at --yuv {yuv}"
    assert fed == {"jpeg": name, "ref": "001.ref.png", "gray": "001.gray.png"}[reads], f"{mode}: avifenc reads {reads}"
    assert ("--ignore-exif" in enc) == (reads == "jpeg"), f"{mode}: only a fed JPEG has its EXIF ignored"

    fake.CALLS.clear()
    fake.SCORE[0] = lambda _ref, _dec: 50.0  # every AVIF fails: the lossless path
    modes = []
    real = pages.pixels_match
    monkeypatch.setattr(pages, "pixels_match", lambda a, b, m: modes.append(m) or real(a, b, m))
    ch = Chapter(tmp_path / "lossless", [(name, data)])
    rec = ch.run(ch.plan())
    [run] = calls(tool)
    assert rec["outcome"] == "lossless"
    assert not calls({"jpegtran": "oxipng", "oxipng": "jpegtran"}[tool])
    out_flag = "-outfile" if tool == "jpegtran" else "--out"
    assert run[: run.index(out_flag)] == args, f"{mode}: {tool} {' '.join(args)}"
    assert Path(run[-1]).name == name, f"{mode}: {tool} reads the page itself (no 8-bit colour container to gray)"
    assert modes == [verify], f"{mode}: verified {verify}"


@budget(load="pure")
def test_the_cover_width_is_the_most_common_non_cover_width_first_seen_on_a_tie():
    def p(*pages):
        return SimpleNamespace(pages=[{"size": [w, 10], "cover": c} for w, c in pages])

    assert covers.width(p((300, True), (80, False), (90, False), (90, False))) == 90
    assert covers.width(p((80, False), (90, False))) == 80, "a tie goes to the first seen"
    assert covers.width(p((300, True))) == 300, "only cover pages: their width"
