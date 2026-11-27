"""The convert stage (stages/convert.py; SPEC "Conversion", "Calibration").

Its staged file and record, the program errors that need a human, scratch life, the post-check, and the calibration
of a series arriving a few chapters at a time. Tools and scorer are the fakes; each test inserts its own chapter_edit
row.
"""

import errno
import hashlib
import io
import json
import os
import pathlib
import re
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import archive, config, db, errors, images, page_store, process, status, strip, workflow
from manga_pipeline.convert import calibrate, check, covers, image, record_reader
from manga_pipeline.stages import convert

from tests import pages as fx
from tests.builders import (
    WinFileNotFoundError,
    add_chapter,
    add_series,
    adopted,
    chapter,
    claimed,
    db_row,
    download,
    present,
)
from tests.convert_pages import NO_EDIT, colour, encoded, gray, jpeg, png, webp
from tests.fakes import convert as fake
from tests.timing import budget


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield Site(cfg, conn)


class Site:
    """An adopted site's connection and process Context, with builders of series and convert-stage chapters."""

    def __init__(self, cfg: config.Config, conn: db.Connection):
        """Store the site's config and connection and build its process Context."""
        self.cfg, self.conn = cfg, conn
        self.ctx = workflow.context(cfg, conn, "process")
        self.n = 0

    def series(self, name="S", layout="pages", aspect=None):
        """Add series `name` with its computed series_conversion row (layout, page aspect); return its id."""
        series_id = add_series(self.conn, name)
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO series_conversion (series_id, layout, page_aspect, origin) VALUES (%s,"
                " %s, %s, 'computed')",
                (series_id, layout, None if aspect is None else json.dumps(aspect)),
            )
        return series_id

    def chapter(self, series_id, members, edit=NO_EDIT):
        """Add a convert-stage chapter of the series whose download holds `members`; return its id.

        Its chapter_edit row holds `edit`; edit=None adds none.
        """
        self.n += 1
        path = download(self.cfg, "S", f"Ch {self.n}")
        archive.write(path, members)
        chapter_id = add_chapter(
            self.conn,
            series_id,
            f'["", null, "", "{self.n}", ""]',
            stage="convert",
            download_path=config.to_stored(self.cfg, path),
        )
        if edit is not None:
            with db.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO chapter_edit (chapter_id, edit) VALUES (%s, %s)",
                    (chapter_id, edit if isinstance(edit, str) else json.dumps(edit)),
                )
        return chapter_id

    def run(self, chapter_id):
        """Run the convert stage on the chapter, claimed as workflow.claim claims it."""
        return convert.run(self.ctx, claimed(self.conn, self.ctx, chapter_id))

    def record(self, outcome):
        """Record a stage outcome in its own transaction."""
        with db.transaction(self.conn):
            outcome.record(self.conn)

    def scratch(self, chapter_id):
        """Return the scratch folder of the chapter's last claim."""
        return config.abs_path(self.cfg["paths"]["scratch"]) / workflow.claim_name(chapter(self.conn, chapter_id))


def pages(n, seed=0, page=colour):
    return [(f"{i:03d}.jpg", jpeg(page(seed=seed * 100 + i))) for i in range(1, n + 1)]


# ---------------------------------------------------------------- the staged file and the record


@budget(load="pool")
def test_a_converted_chapter_is_staged_and_recorded_and_its_scratch_is_gone(site):
    chapter_id = site.chapter(site.series(), pages(2))
    library = config.abs_path(site.cfg["paths"]["library"])
    (library / "S").mkdir()
    (library / "S" / ".optimizer.json").write_text("{}", encoding="utf-8")
    before = sorted(p.relative_to(library) for p in library.rglob("*"))
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.Done)
    site.record(outcome)
    row = chapter(site.conn, chapter_id)
    name = f"{db.database_id(site.conn)[:8]}-{workflow.claim_name(chapter(site.conn, chapter_id))}.cbz"
    staged = config.abs_path(site.cfg["paths"]["staging"]) / name
    assert name.endswith(f"-{chapter_id}-1.cbz"), "the staged name carries the chapter id and its claim count"
    assert row["staged_path"] == config.to_stored(site.cfg, staged)
    assert row["staged_size"] == staged.stat().st_size
    with zipfile.ZipFile(staged) as z:
        assert [i.filename for i in z.infolist()] == ["001.avif", "002.avif"]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()), "store mode"
    result = json.loads(row["convert_result"])
    assert (result["route"], result["outcome"], result["pages_in"], result["pages_out"]) == ("avif", "avif", 2, 2)
    assert result["quality"] == {"gray": 80, "colour": 80}, "an open group encodes at q80"
    out = json.loads(row["output_pages"])
    assert [(p["index"], p["name"], p["width"], p["height"]) for p in out] == [
        (0, "001.avif", 96, 128),
        (1, "002.avif", 96, 128),
    ]
    assert not site.scratch(chapter_id).exists(), "the scratch folder is removed"
    assert sorted(p.relative_to(library) for p in library.rglob("*")) == before, "nothing is written in the library"


# ---------------------------------------------------------------- program errors and failures


@budget(load="pool")
def test_a_chapter_with_no_edit_row_needs_a_human(site):
    chapter_id = site.chapter(site.series(), pages(1), edit=None)
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman)
    assert "no chapter_edit row" in outcome.reason


@budget(load="pool")
def test_a_malformed_edit_needs_a_human_naming_the_key(site):
    chapter_id = site.chapter(site.series(), pages(1), edit={**NO_EDIT, "split": "no"})
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman)
    assert "split must be a bool" in outcome.reason


@budget(load="pool")
def test_a_series_without_a_layout_fails(site):
    chapter_id = site.chapter(add_series(site.conn, "No layout"), pages(1))
    with pytest.raises(RuntimeError, match="has no layout"):
        site.run(chapter_id)


@budget(load="pool")
def test_an_exif_rotated_page_needs_a_human_and_leaves_no_scratch(site):
    exif = Image.Exif()
    exif[0x0112] = 6

    buf = io.BytesIO()
    colour().save(buf, "JPEG", exif=exif.tobytes())
    chapter_id = site.chapter(site.series(), [("001.jpg", buf.getvalue())])
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman)
    assert "EXIF orientation 6" in outcome.reason
    assert not site.scratch(chapter_id).exists()


@budget(load="pool")
def test_a_stale_stored_measurement_fails_the_chapter(site):
    chapter_id = site.chapter(site.series(), pages(1))

    stale = images.measure_pages(config.to_local(site.cfg, chapter(site.conn, chapter_id)["download_path"]))
    stale[0]["sha1"] = "0" * 40
    with db.transaction(site.conn):
        page_store.save(site.conn, chapter_id, stale)
    with pytest.raises(ValueError, match="known sha1"):
        site.run(chapter_id)


@budget(load="pool")
def test_a_failed_post_check_deletes_the_staged_file_and_fails(site, monkeypatch):
    monkeypatch.setattr(check, "check", lambda *_a: ["page-count: 1 != expected 2"])
    chapter_id = site.chapter(site.series(), pages(1))
    with pytest.raises(RuntimeError, match="post-check failed: page-count"):
        site.run(chapter_id)
    assert list(config.abs_path(site.cfg["paths"]["staging"]).glob("*.cbz")) == []
    assert not site.scratch(chapter_id).exists()


@budget(load="pool")
def test_a_share_error_while_staging_reaches_the_workflow_as_an_oserror(site, monkeypatch):
    def eio(*_a):
        raise OSError(errno.EIO, "Input/output error")

    chapter_id = site.chapter(site.series(), pages(1))
    monkeypatch.setattr(archive, "write", eio)
    with pytest.raises(OSError, match="Input/output error") as e:
        site.run(chapter_id)
    assert e.value.errno == errno.EIO, "workflow.share_errors turns it into Unreachable('share')"
    assert not site.scratch(chapter_id).exists()


# ---------------------------------------------------------------- the post-check


def staged(tmp_path, members):
    path = tmp_path / "s.cbz"
    archive.write(path, members)
    return path


@budget(load="pool")
def test_the_post_check_names_what_is_wrong(tmp_path):
    a, b = jpeg(colour()), jpeg(gray().convert("RGB"))
    rec: check.Staged = {
        "outcome": "untouched",
        "gate": None,
        "edits": "drops",
        "cover": None,
        "lossless": {},
        "scores": {},
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}, {"name": "002.jpg", "sha1": archive.sha1(b)}],
    }
    kept: list[check.SourceBytes] = [{"bytes": len(a)}, {"bytes": len(b)}]
    assert check.check(staged(tmp_path, [("001.jpg", a), ("002.jpg", b)]), rec, kept) == []
    assert any("page-count" in f for f in check.check(staged(tmp_path, [("001.jpg", a)]), rec, kept))
    swapped = check.check(staged(tmp_path, [("001.jpg", b), ("002.jpg", a)]), rec, kept)
    assert "page-content-mismatch" in swapped
    extra = check.check(staged(tmp_path, [("001.jpg", a), ("002.jpg", b), ("notes.txt", b"x")]), rec, kept)
    assert any(f.startswith("unexpected-files") for f in extra)
    wrong_ext = check.check(staged(tmp_path, [("001.png", a), ("002.jpg", b)]), rec, kept)
    assert any("extension-mismatch: 001.png" in f for f in wrong_ext)
    broken = check.check(staged(tmp_path, [("001.jpg", a[:40]), ("002.jpg", b)]), rec, kept)
    assert any("undecodable-page: 001.jpg" in f for f in broken)
    x = fx.jxl(colour())
    rec_x: check.Staged = {**rec, "output": [{"name": "001.jxl", "sha1": archive.sha1(x)}]}
    jxl_page = check.check(staged(tmp_path, [("001.jxl", x)]), rec_x, [{"bytes": len(x)}])
    assert jxl_page == ["unreadable-format: 001.jxl is JXL"], "Kavita shows no JPEG XL page: none goes out"


@budget(load="pool")
def test_the_post_check_holds_avif_scores_lossless_pixels_and_size(tmp_path):
    a = jpeg(colour())
    avif: check.Staged = {
        "outcome": "avif",
        "gate": "floor+size",
        "edits": "drops",
        "lossless": {},
        "scores": {},
        "cover": None,
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}],
    }
    fails = check.check(staged(tmp_path, [("001.jpg", a)]), avif, [{"bytes": 1}])
    assert "score-missing: 001.jpg" in fails
    assert "mixed-formats: JPEG" in fails
    assert any(f.startswith("output-larger-than-input") for f in fails), "page bytes above the source pages'"
    (tmp_path / "001.jpg").write_bytes(jpeg(gray().convert("RGB")))
    lossless: check.Staged = {
        "outcome": "lossless",
        "gate": None,
        "edits": "drops",
        "lossless": {"001.jpg": {"source": str(tmp_path / "001.jpg"), "mode": "exact"}},
        "scores": {},
        "cover": None,
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}],
    }
    fails = check.check(staged(tmp_path, [("001.jpg", a)]), lossless, [{"bytes": 10**6}])
    assert fails == ["lossless-mismatch (exact): 001.jpg"]


@budget(load="pool")
def test_the_post_check_compares_a_rendered_lossless_page_with_its_render_and_skips_the_size_rule(tmp_path):
    render = tmp_path / "render"
    render.mkdir()
    (render / "001.png").write_bytes(png(colour()))
    altered = colour()
    altered.putpixel((0, 0), (0, 0, 0))
    data = png(altered)
    rec: check.Staged = {
        "outcome": "lossless",
        "gate": None,
        "edits": "all",
        "lossless": {"001.png": {"source": str(render / "001.png"), "mode": "exact"}},
        "scores": {},
        "cover": None,
        "output": [{"name": "001.png", "sha1": archive.sha1(data)}],
    }
    fails = check.check(staged(tmp_path, [("001.png", data)]), rec, [{"bytes": 1}])
    assert fails == ["lossless-mismatch (exact): 001.png"], "checked against the render; no size rule on rendered pages"


@budget(load="pool")
def test_the_post_check_refuses_a_deflated_archive(tmp_path):
    a = jpeg(colour())
    path = tmp_path / "d.cbz"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("001.jpg", a)
    rec: check.Staged = {
        "outcome": "untouched",
        "gate": None,
        "edits": "drops",
        "lossless": {},
        "scores": {},
        "cover": None,
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}],
    }
    assert check.check(path, rec, [{"bytes": len(a)}]) == ["not-store-mode"]


# ---------------------------------------------------------------- calibration


def q_scores(by_q):
    """Fake scores from the encode quality in the decode's name: by_q(q)."""

    def f(_ref, dec):
        return by_q(int(present(re.search(r"\.q(\d+)\.png$", dec.name)).group(1)))

    return f


def calibration(site, series_id):
    return calibrate.rows(site.conn, series_id)


@budget(load="pool", seconds=15)
def test_a_new_series_arriving_3_chapters_at_a_time_is_calibrated_by_the_third(site):
    fake.SCORE[0] = q_scores(lambda q: 95 - (80 - q) * 0.5)  # q75 92.5 ... q60 85, q55 82.5 (mean < 85)
    series_id = site.series()
    results = []
    for n in range(3):
        chapter_id = site.chapter(series_id, pages(7, seed=n))
        site.record(site.run(chapter_id))
        results.append(json.loads(chapter(site.conn, chapter_id)["convert_result"])["quality"]["colour"])
    rows = calibration(site, series_id)
    assert results == [80, 80, 60], "open groups encode at q80; the chapter that fixes the group uses the fixed q"
    assert (rows["colour"]["fixed"], rows["colour"]["q"], len(rows["colour"]["samples"])) == (1, 60, 21)
    assert rows["colour"]["why"].startswith("q55 fails: mean 82.5 < 85")
    assert "gray" not in rows, "a group no page encodes gathers nothing"


@budget(load="pool", seconds=10)
def test_a_series_of_2_full_chapters_fixes_the_group_at_the_second(site):
    fake.SCORE[0] = q_scores(lambda q: 95 - (80 - q) * 0.5)
    series_id = site.series()
    results = []
    for n in range(2):
        chapter_id = site.chapter(series_id, pages(calibrate.PER_CHAPTER, seed=n))
        site.record(site.run(chapter_id))
        results.append(json.loads(chapter(site.conn, chapter_id)["convert_result"])["quality"]["colour"])
    row = calibration(site, series_id)["colour"]
    assert results == [80, 60], "D3 (f): 14 + 14 samples reach 20, so the second chapter fixes the group and uses it"
    assert (row["fixed"], row["q"], len(row["samples"])) == (1, 60, 2 * calibrate.PER_CHAPTER)


@budget(load="pool")
def test_a_retried_chapter_adds_no_samples_twice(site):
    series_id = site.series()
    chapter_id = site.chapter(series_id, pages(3))
    site.run(chapter_id)
    site.run(chapter_id)
    assert len(calibration(site, series_id)["colour"]["samples"]) == 3


@budget(load="pool", seconds=15)
def test_20_samples_with_fewer_than_20_counted_fix_the_group_at_q80(site):
    low = {"001", "002", "003"}
    fake.SCORE[0] = lambda _ref, dec: (
        70.0 if dec.name.split(".")[0] in low else 95 - (80 - int(dec.name.split(".q")[1][:2])) * 0.5
    )
    series_id = site.series()
    for n in range(3):
        site.record(site.run(site.chapter(series_id, pages(7, seed=n))))
    row = calibration(site, series_id)["colour"]
    assert len(row["samples"]) == 21
    assert (row["fixed"], row["q"]) == (1, 80), "the bulk converter's rule: 20 samples fix it; 18 counted fail q75"
    assert row["why"] == "q75 fails: 12 counted < 20 (21 samples)", "3 low pages in each of 3 chapters"


def stored_samples(series_id, n, chapters, *, counted):
    """Return n stored samples of the series' colour group over these chapter ids, the first `counted` passing q80."""
    return [
        {
            "sha1": f"s{series_id}-{i}",
            "rendered": False,
            "bytes_in": 1,
            "scores": {str(q): [90.0 if i < counted else 79.0, 1] for q in (80, *calibrate.LADDER)},
            "chapter_id": chapters[i % len(chapters)],
        }
        for i in range(n)
    ]


@pytest.mark.parametrize(("n", "chapters", "counted", "q"), [(28, (901, 902), 28, 50), (40, (901, 902, 903), 19, 80)])
@budget(load="pool")
def test_an_open_group_stored_under_the_3_chapter_rule_fixes_at_the_next_convert(site, n, chapters, counted, q):
    series_id = site.series()
    with db.transaction(site.conn):
        site.conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why, samples)"
            " VALUES (%s, 'colour', 80, 0, 'computed', 'open', %s)",
            (series_id, json.dumps(stored_samples(series_id, n, chapters, counted=counted))),
        )
    chapter_id = site.chapter(series_id, pages(3, seed=7))
    (line,) = converted(site, chapter_id)
    row = calibration(site, series_id)["colour"]
    assert (row["fixed"], row["q"]) == (1, q), "the new rule fixes it before the chapter samples"
    assert line.count(f"calibration: colour fixed at q{q}") == 1, "the chapter that settled the group is noted once"


@budget(load="pool")
def test_a_group_holding_20_samples_of_this_chapter_fixes_at_its_retry(site):
    series_id = site.series()
    chapter_id = site.chapter(series_id, pages(3, seed=7))
    with db.transaction(site.conn):  # samples it added under the 3-chapter rule: it takes no more, picks nothing new
        site.conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why, samples)"
            " VALUES (%s, 'colour', 80, 0, 'computed', 'open', %s)",
            (series_id, json.dumps(stored_samples(series_id, 24, (901, chapter_id), counted=24))),
        )
    (line,) = converted(site, chapter_id)
    assert calibration(site, series_id)["colour"]["fixed"] == 1, "20 samples fix the group, whoever added them"
    assert line.count("calibration: colour fixed") == 1, "settled by this chapter and holding its samples: 1 line"


def calibrated_record(calibration: record_reader.RecordCalibration) -> record_reader.Record:
    """Return a bulk record holding this series.calibration, its other keys minimal."""
    return {
        "schema": record_reader.SCHEMA,
        "stages": list(record_reader.STAGES),
        "kavita": {},
        "folders": [],
        "runs": [],
        "errors": [],
        "flags": [],
        "series": {
            "layout": {"layout": "pages"},
            "naming": {"series_name": "S", "restart": False},
            "calibration": calibration,
            "covers": {"volumes": {}},
            "decisions": {},
        },
        "sources": [],
        "chapters": [],
    }


@budget(load="pure")
def test_a_record_fixes_only_the_groups_it_probed():
    record = calibrated_record(
        {
            "groups": {
                "gray": {"q": 80, "why": "too few sample pages (0)", "probes": []},
                "colour": {"q": 70, "why": "q65 fails", "probes": [{"q": 75}, {"q": 70}, {"q": 65}]},
            }
        }
    )
    assert calibrate.from_record(record) == {"colour": (70, "record: q65 fails")}
    assert calibrate.from_record(calibrated_record({})) == {}, "a record without calibration fixes nothing"


@budget(load="pure")
def test_choose_follows_the_bulk_converters_rule():
    def sample(chapter, by_q) -> calibrate.Sample:
        scores = {str(q): [by_q(q), 1] for q in (80, *calibrate.LADDER)}
        return {"sha1": "", "rendered": False, "bytes_in": 1, "scores": scores, "chapter_id": chapter}

    passing = [sample(c % 3, lambda _q: 90.0) for c in range(20)]
    assert calibrate.choose(passing) == (50, "ladder floor q50 passes (20 samples)")
    assert calibrate.choose(passing[:19]) is None, "fewer than 20 samples leave the group open"
    one_chapter = [sample(0, lambda _q: 90.0) for _ in range(20)]
    assert calibrate.choose(one_chapter) == (50, "ladder floor q50 passes (20 samples)"), "no chapter minimum"
    one_under = [*passing[:-1], sample(2, lambda _q: 79.0)]
    assert calibrate.choose(one_under) == (80, "q75 fails: 19 counted < 20 (20 samples)"), (
        "20 samples fix the group; with fewer than 20 counted it stays at q80"
    )
    one_low = [*passing[:-1], sample(0, lambda q: 90.0 if q >= 70 else 79.0)]
    assert present(calibrate.choose(one_low))[0] == 70, "a sample under the floor at q65 fails that probe"


# ---------------------------------------------------------------- strip and page repairs through the stage


@budget(load="pool")
def test_a_strip_chapter_is_restitched_with_its_removed_rows_cut_out(site):
    tall = [(f"{i:03d}.jpg", jpeg(colour((200, 1400), seed=i))) for i in range(1, 4)]
    members = [("000_cover.jpg", jpeg(colour((200, 300), seed=9))), *tall]
    edit = {**NO_EDIT, "strip_removed": [{"start": 3900, "end": 4200, "why": "credits"}]}
    chapter_id = site.chapter(site.series(layout="strip"), members, edit=edit)
    site.record(site.run(chapter_id))
    result = json.loads(chapter(site.conn, chapter_id)["convert_result"])
    out = json.loads(chapter(site.conn, chapter_id)["output_pages"])
    assert result["route"] == "rendered-avif"
    assert result["reshape"]["kind"] == "restitch"
    assert out[0]["name"] == "000_cover.avif", "the cover page stays a page of its own"
    assert out[0]["height"] == 300, "the cover page stays a page of its own"
    assert sum(p["height"] for p in out[1:]) == 3900, "the strip without its removed 300 rows"


@budget(load="pool")
def test_a_split_chapter_is_split_at_its_series_page_aspect(site):
    edit = {**NO_EDIT, "split": True}
    split_id = site.chapter(site.series("T", aspect=[1.4]), [("001.jpg", jpeg(colour((100, 280))))], edit=edit)
    site.record(site.run(split_id))
    assert json.loads(chapter(site.conn, split_id)["convert_result"])["pages_out"] == 2


@budget(load="pool")
def test_a_re_sliced_chapter_is_cut_into_its_recorded_pages_whatever_the_aspect_at_convert(site):
    edit = {**NO_EDIT, "split": True, "split_pages": 4}
    members = [(f"00{k}.jpg", jpeg(colour((100, 186), seed=k))) for k in (1, 2, 3)]  # 3 x 186 = 4 pages of 139.5
    chapter_id = site.chapter(site.series("R", aspect=[1.0]), members, edit=edit)  # the aspect changed since junk
    (line,) = converted(site, chapter_id)
    result = json.loads(chapter(site.conn, chapter_id)["convert_result"])
    assert result["pages_out"] == 4, "the recorded count, not the aspect's"
    assert "split: re-sliced into 4 pages" in line, "the chapter is noted"


@budget(load="pool")
def test_a_jxl_page_chapter_is_published_as_avif(site):
    members = [(f"00{i}.jxl", fx.jxl_of_jpeg(jpeg(colour(seed=i)))) for i in (1, 2)]
    result, out = staged_result(site, site.chapter(site.series(), members))
    assert (result["outcome"], result["reshape"]["kind"]) == ("avif", "decode")
    assert [p["name"] for p in out] == ["001.avif", "002.avif"], "every page AVIF, in order"


@budget(load="pool")
def test_a_jxl_page_chapter_whose_avif_fails_is_published_as_png(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    members = [("001.jxl", fx.jxl(colour(seed=1))), ("002.webp", webp(colour(seed=2)))]
    result, out = staged_result(site, site.chapter(site.series(), members))
    assert result["outcome"] == "lossless"
    assert [p["name"] for p in out] == ["001.png", "002.png"], "decoded, never the JPEG XL page Kavita cannot show"


@budget(load="pool")
def test_a_strip_chapter_of_jxl_pages_is_published_restitched(site):
    tall = [(f"00{i}.jxl", fx.jxl(colour((200, 1400), seed=i))) for i in (1, 2, 3)]
    result, out = staged_result(site, site.chapter(site.series(layout="strip"), tall))
    assert (result["outcome"], result["reshape"]["kind"]) == ("avif", "restitch")
    assert sum(p["height"] for p in out) == 4200, "the whole strip, cut into pages"


@budget(load="pool")
def test_a_strip_chapter_whose_avif_fails_is_published_restitched_and_lossless(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    tall = [(f"{i:03d}.jpg", jpeg(colour((200, 1400), seed=i))) for i in range(1, 4)]
    members = [("000_cover.jpg", jpeg(colour((200, 300), seed=9))), *tall]
    edit = {**NO_EDIT, "strip_removed": [{"start": 3900, "end": 4200, "why": "credits"}]}
    chapter_id = site.chapter(site.series(layout="strip"), members, edit=edit)
    result, out = staged_result(site, chapter_id)
    assert result["outcome"] == "lossless", "A4: never the original slices with their junk"
    assert result["reshape"]["kind"] == "restitch"
    assert result["avif_rejected"], "why the AVIF was rejected"
    assert out[0]["name"] == "000_cover.png"
    assert sum(p["height"] for p in out[1:]) == 3900, "the strip without its removed 300 rows"


def unit(sha1) -> calibrate.Unit:
    """Return a calibration unit of this sha1; its other fields are placeholders."""
    return {
        "path": Path(f"{sha1}.png"),
        "sha1": sha1,
        "class": images.COLOUR,
        "yuv": "444",
        "fmt": images.PNG,
        "jpeg_feed": False,
        "rendered": False,
    }


@budget(load="pure")
def test_a_group_takes_no_more_than_40_samples():
    units = [unit(str(i)) for i in range(20)]
    assert len(calibrate.pick(units, {f"s{i}" for i in range(30)})) == 10, "room for 10 more"
    assert calibrate.pick(units, {f"s{i}" for i in range(40)}) == [], "a full group takes none"


@budget(load="pool")
def test_2_pages_whose_names_share_a_stem_need_a_human_through_the_stage(site):
    chapter_id = site.chapter(site.series(), [("cover.jpg", jpeg(colour())), ("cover.png", jpeg(gray()))])
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman)
    assert "share a name stem" in outcome.reason
    assert not site.scratch(chapter_id).exists()


@budget(load="fs")
def test_the_post_check_names_a_low_score_folders_and_an_ambiguous_order(tmp_path):
    buf = io.BytesIO()
    colour().save(buf, "AVIF", quality=80, speed=10)
    avif = buf.getvalue()
    rec: check.Staged = {
        "outcome": "avif",
        "gate": "floor",
        "edits": "drops",
        "cover": None,
        "lossless": {},
        "scores": {"001.avif": 79.5},
        "output": [{"name": "001.avif", "sha1": archive.sha1(avif)}],
    }
    assert check.check(staged(tmp_path, [("001.avif", avif)]), rec, []) == ["score-below-min: 001.avif 79.50"]
    nested = check.check(staged(tmp_path, [("a/001.avif", avif)]), {**rec, "scores": {"a/001.avif": 90.0}}, [])
    assert "nested-entries" in nested
    a, b, c = (jpeg(colour(seed=s)) for s in (1, 2, 3))
    names = [("1.jpg", a), ("2.jpg", b), ("10.jpg", c)]
    rec = {
        "outcome": "untouched",
        "gate": None,
        "edits": "drops",
        "cover": None,
        "lossless": {},
        "scores": {},
        "output": [{"name": n, "sha1": archive.sha1(d)} for n, d in names],
    }
    fails = check.check(staged(tmp_path, names), rec, [])
    assert fails == ["page-order-ambiguous: natural and lexical order differ"]


@budget(load="pure")
def test_a_repeated_page_is_sampled_once():
    units = [unit(sha1) for sha1 in "abac"]
    assert [u["sha1"] for u in calibrate.pick(units, set())] == ["a", "b", "c"]


@budget(load="pool")
def test_lossless_and_untouched_chapters_through_the_stage(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    series_id = site.series()
    lossless = site.chapter(series_id, pages(2))
    untouched = site.chapter(series_id, [("001.webp", webp(colour()))])
    for chapter_id, want in ((lossless, ("avif", "lossless")), (untouched, ("untouched", "untouched"))):
        site.record(site.run(chapter_id))
        result = json.loads(chapter(site.conn, chapter_id)["convert_result"])
        assert (result["route"], result["outcome"]) == want
        assert result["mean_score"] is None


@budget(load="fs")
def test_a_luma_check_on_a_colour_page_is_done_exactly(tmp_path):
    a = jpeg(colour())
    (tmp_path / "001.jpg").write_bytes(a)
    rec: check.Staged = {
        "outcome": "lossless",
        "gate": None,
        "edits": "drops",
        "lossless": {"001.jpg": {"source": str(tmp_path / "001.jpg"), "mode": "luma"}},
        "scores": {},
        "cover": None,
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}],
    }
    fails = check.check(staged(tmp_path, [("001.jpg", a)]), rec, [{"bytes": len(a)}])
    assert fails == ["luma-check-on-colour-page: 001.jpg"], "a colour page may not be compared in luma only"


# ---------------------------------------------------------------- review fixes: edge outcomes and sampling rules


@budget(load="pool")
def test_an_edit_dropping_every_page_needs_a_human(site):
    chapter_id = site.chapter(site.series(), pages(2), edit={**NO_EDIT, "drop_pages": [0, 1]})
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman)
    assert "drops every page" in outcome.reason


@budget(load="pool")
def test_a_strip_edit_removing_every_row_needs_a_human(site):
    members = [(f"{i:03d}.jpg", jpeg(colour((200, 1400), seed=i))) for i in range(1, 4)]
    edit = {**NO_EDIT, "strip_removed": [{"start": 0, "end": 4200, "why": "credits"}]}
    chapter_id = site.chapter(site.series(layout="strip"), members, edit=edit)
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman), (
        "removed rows covering the whole strip leave no page: needs_human at once, no attempt spent"
    )
    assert outcome.reason == "the chapter edit drops every page", (
        "removed rows covering the whole strip leave no page: needs_human at once, no attempt spent"
    )
    assert not site.scratch(chapter_id).exists()


@budget(load="pool")
def test_a_post_check_that_raises_still_removes_the_staged_file(site, monkeypatch):
    def broken(*_a):
        msg = "source page unreadable"
        raise OSError(msg)

    monkeypatch.setattr(check, "check", broken)
    chapter_id = site.chapter(site.series(), pages(1))
    with pytest.raises(OSError, match="unreadable"):
        site.run(chapter_id)
    assert list(config.abs_path(site.cfg["paths"]["staging"]).glob("*.cbz")) == []


@budget(load="pool")
def test_cover_pages_are_never_samples(site):
    series_id = site.series()
    site.run(site.chapter(series_id, [("000_cover.jpg", jpeg(colour(seed=50))), *pages(2)]))
    assert len(calibration(site, series_id)["colour"]["samples"]) == 2, "the cover page is left out"


@budget(load="pool")
def test_a_chapter_whose_first_outcome_is_not_avif_adds_no_samples(site):
    series_id = site.series()
    site.run(site.chapter(series_id, [("001.webp", webp(colour()))]))
    assert calibration(site, series_id) == {}


@budget(load="pool")
def test_a_fixed_group_is_not_probed(site):
    series_id = site.series()
    with db.transaction(site.conn):
        site.conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why) "
            "VALUES (%s, 'colour', 70, 1, 'record', 'r')",
            (series_id,),
        )
    chapter_id = site.chapter(series_id, pages(2))
    site.record(site.run(chapter_id))
    qs = {a[a.index("-q") + 1] for name, a in fake.CALLS if name == "avifenc"}
    assert qs == {"70"}, "only the chapter's own encodes, at the fixed q: no probe"


@budget(load="pool")
def test_a_chapter_feeds_both_groups_and_a_rendered_chapter_samples_its_rendered_pages(site):
    series_id = site.series()
    site.run(site.chapter(series_id, [*pages(2), ("003.jpg", jpeg(gray().convert("RGB")))]))
    rows = calibration(site, series_id)
    assert (len(rows["colour"]["samples"]), len(rows["gray"]["samples"])) == (2, 1)
    strip_series = site.series("Strip", layout="strip")
    site.run(
        site.chapter(strip_series, [(f"{i:03d}.jpg", jpeg(colour((200, 1400), seed=60 + i))) for i in range(1, 4)])
    )
    samples = calibration(site, strip_series)["colour"]["samples"]
    assert samples, "a rendered chapter samples its rendered pages"
    assert all(s["rendered"] for s in samples), "a rendered chapter samples its rendered pages"


@budget(load="pool")
def test_1_scorer_serves_every_chapter_of_the_process(site):
    convert.loaded_scorer.cache_clear()
    fake.FakeScorer.made.clear()
    series_id = site.series()
    for n in range(2):
        site.run(site.chapter(series_id, pages(1, seed=n)))
    assert len(fake.FakeScorer.made) == 1, "the library and its GPU context are loaded once, not per chapter"


@budget(load="pool")
def test_a_process_start_removes_chapter_scratch_folders_and_nothing_else(site: Site):
    root = config.abs_path(site.cfg["paths"]["scratch"])
    for name in ("7-1", "7-2", "junk-review", "notes"):
        (root / name).mkdir(parents=True)
    cfg: config.Config = {**site.cfg, "lock": {**site.cfg["lock"], "enabled": False}}
    process.clean_scratch(cfg)
    assert sorted(p.name for p in root.iterdir()) == ["7-1", "7-2", "junk-review", "notes"], (
        "with locks off another run may be using them: nothing is removed"
    )
    process.clean_scratch(site.cfg)
    assert sorted(p.name for p in root.iterdir()) == ["junk-review", "notes"], (
        "chapter folders (<id>-<count>) go; junk review's folder and anything else stay"
    )


@budget(load="db")
def test_a_process_start_deletes_day_old_staged_files_no_chapter_names(site):
    root = config.abs_path(site.cfg["paths"]["staging"])
    chapter_id = site.chapter(site.series(), pages(1))
    named, orphan, fresh, other = (root / n for n in ("named.cbz", "orphan.cbz", "fresh.cbz", "notes.txt"))
    for path in (named, orphan, fresh, other):
        path.write_bytes(b"x")
    old = 2 * 86400
    for path in (named, orphan, other):
        os.utime(path, (path.stat().st_atime - old, path.stat().st_mtime - old))
    with db.transaction(site.conn):
        site.conn.execute(
            "UPDATE chapter SET staged_path = %s WHERE id = %s", (config.to_stored(site.cfg, named), chapter_id)
        )
    process.clean_staging(site.ctx)
    assert sorted(p.name for p in root.iterdir() if p.name != ".pipeline-owner") == [
        "fresh.cbz",
        "named.cbz",
        "notes.txt",
    ], "only a day-old .cbz no chapter names goes; the owner file, a named file and a fresh one stay"
    assert (root / ".pipeline-owner").exists()


@budget(load="db")
def test_a_process_start_deletes_a_day_old_part_file(site):
    root = config.abs_path(site.cfg["paths"]["staging"])
    old, fresh = root / "old.cbz.part", root / "fresh.cbz.part"
    for path in (old, fresh):
        path.write_bytes(b"x")
    os.utime(old, (old.stat().st_atime - 2 * 86400, old.stat().st_mtime - 2 * 86400))
    process.clean_staging(site.ctx)
    assert not old.exists(), "a killed archive write's .part goes once a day old, as a .cbz does"
    assert fresh.exists(), "a killed archive write's .part goes once a day old, as a .cbz does"


@budget(load="db")
@pytest.mark.parametrize("winerror", [None, 53])
def test_a_staged_file_that_vanishes_during_the_clean_up(site, monkeypatch, winerror):
    root = config.abs_path(site.cfg["paths"]["staging"])
    (root / "gone.cbz").write_bytes(b"x")
    real = pathlib.Path.stat

    def stat(self, **kwargs):
        if self.name == "gone.cbz":  # another machine's process start removed it after this one listed it
            error = WinFileNotFoundError(2, "No such file or directory")
            error.winerror = winerror
            raise error
        return real(self, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", stat)
    if winerror is None:
        process.clean_staging(site.ctx)  # skipped: nothing left to delete
    else:
        with pytest.raises(FileNotFoundError):
            process.clean_staging(site.ctx)  # a dropped share is not a vanished file: share_errors reports it


@budget(load="pool")
def test_output_pages_not_as_many_as_the_outcome_writes_fail(site):
    chapter_id = site.chapter(site.series(), [("001.webp", webp(colour()))])
    row = chapter(site.conn, chapter_id)
    claim = workflow.claim_name(db_row(id=chapter_id, claim_count=row["claim_count"] + 1))  # the claim site.run takes
    out_dir = config.abs_path(site.cfg["paths"]["scratch"]) / claim / convert.OUTPUT
    out_dir.mkdir(parents=True)
    (out_dir / "999.webp").write_bytes(b"stray")  # an output page the outcome did not write
    with pytest.raises(RuntimeError, match="2 output pages, the outcome writes 1"):
        site.run(chapter_id)


@budget(load="pool")
def test_a_blanked_rendered_chapter_samples_its_unblanked_render_and_records_the_blank(site, monkeypatch):
    seen = []
    real = calibrate.units

    def units(chapter_plan, x_dir, rendered):
        seen.extend(p.parent.name for p in rendered)
        return real(chapter_plan, x_dir, rendered)

    monkeypatch.setattr(calibrate, "units", units)
    edit = {
        **NO_EDIT,
        "bands": [{"page": 0, "box": [0, 0, 96, 100]}],
        "blank": [{"page": 1, "box": [10, 10, 40, 30], "fill": [200, 30, 30]}],
    }
    chapter_id = site.chapter(site.series(), pages(2), edit=edit)
    outcome = site.run(chapter_id)
    site.record(outcome)
    assert seen, "calibration samples come from the unblanked render"
    assert set(seen) == {"render"}, "calibration samples come from the unblanked render"
    result = json.loads(chapter(site.conn, chapter_id)["convert_result"])
    assert (result["outcome"], result["blanked"]) == ("avif", ["002.jpg"])


# ---------------------------------------------------------------- volume covers


def add_cover(site, chapter_id, data, rel="vol/1.jpg", sha256=None):
    """Hand chapter_id a volume cover as intake does: the file under paths.covers and its volume_cover row."""
    path = config.abs_path(site.cfg["paths"]["covers"]) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with db.transaction(site.conn):
        site.conn.execute(
            "INSERT INTO volume_cover (chapter_id, path, sha256, volume) VALUES (%s, %s, %s, %s)",
            (chapter_id, rel, sha256 or hashlib.sha256(data).hexdigest(), "1"),
        )


def staged_result(site, chapter_id):
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.Done), outcome
    site.record(outcome)
    row = chapter(site.conn, chapter_id)
    return json.loads(row["convert_result"]), json.loads(row["output_pages"])


COVER = jpeg(colour((200, 300), seed=77))  # wider than the 96 px test pages: scaled down to them


@budget(load="pool")
def test_a_volume_cover_goes_in_first_as_avif_at_the_page_width_and_bw_ignores_it(site):
    chapter_id = site.chapter(site.series(), pages(2, page=lambda seed: gray(seed=seed).convert("RGB")))
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert [p["name"] for p in out] == ["000_cover.avif", "001.avif", "002.avif"]
    assert (out[0]["width"], out[0]["height"]) == (96, 144), "scaled down to the pages' width, aspect kept"
    assert result["cover"]["format"] == "AVIF"
    assert result["cover"]["volume"] == "1"
    assert result["cover_skipped"] is None
    assert result["pages_out"] == 3, "a colour cover leaves a gray chapter gray"
    assert result["gray"] is True, "a colour cover leaves a gray chapter gray"


@budget(load="pool")
def test_a_cover_page_already_there_is_replaced(site):
    members = [("000_cover.jpg", jpeg(colour(seed=5))), *pages(2)]
    chapter_id = site.chapter(site.series(), members)
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert [p["name"] for p in out] == ["000_cover.avif", "001.avif", "002.avif"]
    assert result["pages_out"] == 3
    assert (out[0]["width"], out[0]["height"]) == (96, 144), "the inserted cover, not the chapter's own"


@budget(load="pool")
def test_a_rendered_chapter_whose_avif_fails_takes_the_cover_as_png_in_place_of_its_cover_page(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0  # every AVIF fails: the cover's too
    members = [("000_cover.jpg", jpeg(colour(seed=5))), *pages(2)]
    edit = {**NO_EDIT, "bands": [{"page": 1, "box": [0, 0, 96, 64]}]}
    chapter_id = site.chapter(site.series(), members, edit=edit)
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert result["outcome"] == "lossless"
    assert [p["name"] for p in out] == ["000_cover.png", "001.png", "002.png"], "the cover replaces the cover page"
    assert result["cover"]["format"] == "PNG", "the rendered pages' format"
    assert result["pages_out"] == 3
    assert result["cover_skipped"] is None


@budget(load="pool")
def test_a_lossless_chapter_takes_the_cover_as_jpeg(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0  # every AVIF fails: the cover's too
    chapter_id = site.chapter(site.series(), pages(2))
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert result["outcome"] == "lossless"
    assert [p["name"] for p in out] == ["000_cover.jpg", "001.jpg", "002.jpg"]
    assert result["cover"]["format"] == "JPEG"
    assert result["cover"]["score"] is None


@budget(load="pool")
def test_pages_of_2_formats_convert_to_avif_for_the_cover_without_the_size_gate(site, monkeypatch):
    monkeypatch.setattr(image, "MAX_SIZE_RATIO", 0.0)  # the size gate would reject any AVIF
    members = [("001.jpg", jpeg(colour(seed=1))), ("002.png", png(colour(seed=2)))]
    chapter_id = site.chapter(site.series(), members)
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert (result["route"], result["outcome"]) == ("avif", "avif"), "force_avif: the floor gate only"
    assert [p["name"] for p in out] == ["000_cover.avif", "001.avif", "002.avif"]


@budget(load="pool")
def test_an_untouched_chapter_takes_the_cover_in_its_own_format(site):
    chapter_id = site.chapter(site.series(), [("001.webp", webp(colour(seed=1))), ("002.webp", webp(colour(seed=2)))])
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert result["outcome"] == "untouched"
    assert [p["name"] for p in out] == ["000_cover.webp", "001.webp", "002.webp"]


@budget(load="pool")
def test_a_cover_whose_avif_fails_is_left_out_with_the_reason(site, caplog):
    fake.SCORE[0] = lambda _ref, dec: 50.0 if dec.name.startswith("000_cover") else 90.0
    chapter_id = site.chapter(site.series(), pages(2))
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert result["outcome"] == "avif"
    assert [p["name"] for p in out] == ["001.avif", "002.avif"]
    assert result["cover"] is None
    assert "below the floor" in result["cover_skipped"]
    assert "cover not inserted" in caplog.text


@budget(load="pool")
def test_pages_sharing_no_cover_format_go_out_without_it(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    chapter_id = site.chapter(site.series(), [("001.jpg", jpeg(colour(seed=1))), ("002.webp", webp(colour(seed=2)))])
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert result["outcome"] == "untouched"
    assert len(out) == 2
    assert result["cover_skipped"] == "its untouched pages (JPEG, WEBP_LOSSY) share no cover format"


def fails_the_attempt(site, chapter_id, match):
    """Check that the cover fails the attempt with an error matching match, and the scratch folder is gone."""
    with pytest.raises(covers.CoverError, match=match):
        site.run(chapter_id)
    assert not site.scratch(chapter_id).exists()


@budget(load="pool")
def test_a_cover_outside_paths_covers_fails_the_attempt_naming_its_path(site):
    chapter_id = site.chapter(site.series(), pages(1))
    add_cover(site, chapter_id, COVER, rel="../outside.jpg")
    fails_the_attempt(site, chapter_id, "not inside paths.covers")


@budget(load="pool")
def test_an_absolute_cover_path_fails_the_attempt_naming_its_path(site, tmp_path):
    chapter_id = site.chapter(site.series(), pages(1))
    rel = str(tmp_path / "abs.jpg")
    add_cover(site, chapter_id, COVER, rel=rel)
    (config.abs_path(site.cfg["paths"]["covers"]) / rel).unlink()
    fails_the_attempt(site, chapter_id, "not inside paths.covers")


@budget(load="pool")
def test_a_cover_of_another_sha256_fails_the_attempt_naming_its_path(site):
    chapter_id = site.chapter(site.series(), pages(1))
    add_cover(site, chapter_id, COVER, sha256="0" * 64)
    fails_the_attempt(site, chapter_id, "another sha256")


@budget(load="pool")
def test_a_missing_cover_fails_the_attempt_naming_its_path(site):
    chapter_id = site.chapter(site.series(), pages(1))
    add_cover(site, chapter_id, COVER)
    (config.abs_path(site.cfg["paths"]["covers"]) / "vol/1.jpg").unlink()
    fails_the_attempt(site, chapter_id, "is missing")


@budget(load="pool")
def test_an_avif_only_chapter_whose_avif_cover_fails_names_the_floor(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0

    chapter_id = site.chapter(site.series(), [("001.avif", encoded(colour(seed=1), "AVIF"))])
    add_cover(site, chapter_id, COVER)
    result, _ = staged_result(site, chapter_id)
    assert result["outcome"] == "untouched"
    assert "below the floor" in result["cover_skipped"]


@budget(load="pool")
def test_a_lossless_png_chapter_replaces_its_cover_page_with_a_png_cover(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    members = [("000_cover.png", png(colour(seed=5))), ("001.png", png(colour(seed=1))), ("002.png", png(gray()))]
    chapter_id = site.chapter(site.series(), members)
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert result["outcome"] == "lossless"
    assert [p["name"] for p in out] == ["000_cover.png", "001.png", "002.png"]
    assert result["cover"]["format"] == "PNG"
    assert (out[0]["width"], out[0]["height"]) == (96, 144)


@budget(load="pool")
def test_a_blanked_rendered_chapter_of_a_fixed_series_is_rendered_once(site, monkeypatch):
    series_id = site.series()
    with db.transaction(site.conn):
        site.conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why) "
            "VALUES (%s, 'colour', 70, 1, 'record', 'r')",
            (series_id,),
        )
    renders = []
    real = image.render
    monkeypatch.setattr(image, "render", lambda p, src, out: renders.append(out.name) or real(p, src, out))
    edit = {
        **NO_EDIT,
        "bands": [{"page": 0, "box": [0, 0, 96, 100]}],
        "blank": [{"page": 1, "box": [10, 10, 40, 30], "fill": [200, 30, 30]}],
    }
    result, _ = staged_result(site, site.chapter(series_id, pages(2), edit=edit))
    assert renders == ["render-blanked"], "no group wants samples: 1 render"
    assert result["blanked"] == ["002.jpg"], "no group wants samples: 1 render"


BLANKED_RENDER_EDIT = {
    **NO_EDIT,
    "bands": [{"page": 0, "box": [0, 0, 96, 100]}],
    "blank": [{"page": 1, "box": [10, 10, 40, 30], "fill": [200, 30, 30]}],
}


@budget(load="pool")
def test_a_blanked_rendered_chapter_of_a_series_whose_open_group_is_full_is_rendered_once(site, monkeypatch):
    series_id = site.series()
    full = stored_samples(series_id, 40, (999,), counted=40)
    with db.transaction(site.conn):
        site.conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why, samples) "
            "VALUES (%s, 'colour', 80, 0, 'computed', 'open: 40 samples', %s)",
            (series_id, json.dumps(full)),
        )
    renders = []
    real = image.render
    monkeypatch.setattr(image, "render", lambda p, src, out: renders.append(out.name) or real(p, src, out))
    staged_result(site, site.chapter(series_id, pages(2), edit=BLANKED_RENDER_EDIT))
    assert renders == ["render-blanked"], "a group holding calibrate.MAX_SAMPLES takes no sample: no unblanked render"


@budget(load="pool")
def test_a_blanked_rendered_chapter_rendered_twice_stages_the_blanked_render(site, monkeypatch):
    encoded = []
    real = image.process_chapter

    def spy(pages, *rest, **named):
        encoded.extend(p.parent.name for p in pages.rendered)
        return real(pages, *rest, **named)

    monkeypatch.setattr(image, "process_chapter", spy)
    renders = []
    real_render = image.render
    monkeypatch.setattr(image, "render", lambda p, src, out: renders.append(out.name) or real_render(p, src, out))
    result, _ = staged_result(site, site.chapter(site.series(), pages(2), edit=BLANKED_RENDER_EDIT))
    assert renders == ["render", "render-blanked"], "an open group wants samples: the unblanked render is made too"
    assert encoded, "the staged pages are encoded from the blanked render"
    assert set(encoded) == {"render-blanked"}, "the staged pages are encoded from the blanked render"
    assert result["blanked"] == ["002.jpg"]


@budget(load="pool")
@pytest.mark.parametrize("winerror", [53, None])
def test_a_dropped_share_while_reading_a_cover_reaches_the_caller_and_a_missing_file_is_a_cover_error(
    site, monkeypatch, winerror
):
    path = config.abs_path(site.cfg["paths"]["covers"]) / "vol" / "1.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(COVER)
    error = WinFileNotFoundError(errno.ENOENT, "The network path was not found", str(path))
    error.winerror = winerror

    def gone(_self):
        raise error

    monkeypatch.setattr(Path, "read_bytes", gone)
    row = db_row(path="vol/1.jpg", sha256=hashlib.sha256(COVER).hexdigest())
    if winerror is None:
        with pytest.raises(covers.CoverError, match="is missing"):
            covers.load(site.cfg, row)
        return
    with pytest.raises(FileNotFoundError) as e:
        covers.load(site.cfg, row)
    assert e.value is error, "a dropped share reaches workflow.share_errors as it is, to become Unreachable('share')"
    assert errors.dropped_share(e.value), (
        "a dropped share reaches workflow.share_errors as it is, to become Unreachable('share')"
    )


@budget(load="pool")
def test_a_gray_chapter_whose_colour_cover_page_is_replaced_stays_gray(site):
    members = [("000_cover.jpg", jpeg(colour(seed=5))), *pages(2, page=lambda seed: gray(seed=seed).convert("RGB"))]
    chapter_id = site.chapter(site.series(), members)
    add_cover(site, chapter_id, COVER)
    result, _ = staged_result(site, chapter_id)
    assert result["cover"], "neither the cover nor the page it replaces counts"
    assert result["gray"] is True, "neither the cover nor the page it replaces counts"


@budget(load="pool")
def test_a_strip_chapter_takes_the_cover_in_place_of_its_own(site):
    members = [("000_cover.jpg", jpeg(colour((200, 300), seed=9)))] + [
        (f"{i:03d}.jpg", jpeg(colour((200, 1400), seed=70 + i))) for i in range(1, 4)
    ]
    chapter_id = site.chapter(site.series("Strip", layout="strip"), members)
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    names = [p["name"] for p in out]
    assert result["route"] == "rendered-avif"
    assert names[0] == "000_cover.avif"
    assert names.count("000_cover.avif") == 1
    assert out[0]["width"] == 200, "the cover at the strip's page width"
    assert result["pages_out"] == len(names), "the cover at the strip's page width"


@budget(load="pool")
def test_a_forced_avif_that_fails_goes_out_untouched_without_the_cover(site):
    fake.SCORE[0] = lambda _ref, dec: 90.0 if dec.name.startswith("000_cover") else 50.0
    members = [("001.jpg", jpeg(colour(seed=1))), ("002.png", png(colour(seed=2)))]
    chapter_id = site.chapter(site.series(), members)
    add_cover(site, chapter_id, COVER)
    result, out = staged_result(site, chapter_id)
    assert (result["route"], result["outcome"]) == ("avif", "untouched")
    assert len(out) == 2
    assert result["cover_skipped"] == "its untouched pages (JPEG, PNG) share no cover format"


# ---------------------------------------------------------------- convert notes (R15) and the status bound (D7)


def listed(site, chapter_id):
    """Return the notes status lists for the chapter ([] when it lists none)."""
    head = f"  chapter {chapter_id} ("
    return [line.split("): ", 1)[1] for line in status.report(site.conn) if line.startswith(head)]


def converted(site, chapter_id):
    """Run convert on the chapter, record its outcome and return the notes status lists for it."""
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.Done), outcome
    site.record(outcome)
    return listed(site, chapter_id)


@budget(load="pool")
def test_a_rejected_avif_is_noted_with_what_was_published(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    chapter_id = site.chapter(site.series(), [("001.jpg", jpeg(colour())), ("002.png", png(colour(seed=3)))])
    (line,) = converted(site, chapter_id)
    assert "convert: AVIF rejected (page " in line, "why the AVIF was rejected"
    assert "published lossless" in line
    assert "convert: lossless chapter stays mixed-format: JPEG+PNG" in line, "R15: mixed-format lossless"


@budget(load="pool")
def test_a4_is_noted_with_its_rendered_pages(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    edit = {**NO_EDIT, "bands": [{"page": 0, "box": [0, 0, 96, 64]}]}
    chapter_id = site.chapter(site.series(), [("001.jpg", jpeg(colour())), ("002.png", png(colour(seed=3)))], edit)
    (line,) = converted(site, chapter_id)
    assert "published its rendered pages lossless" in line
    assert "mixed-format" not in line, "rendered pages are all PNGs"


@budget(load="pool")
def test_a_group_scoring_low_at_its_calibrated_quality_is_noted_as_drift(site):
    fake.SCORE[0] = lambda _ref, _dec: 82.0  # passes the floor, under the mean 85 - 2
    series_id = site.series()
    with db.transaction(site.conn):
        site.conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why, samples)"
            " VALUES (%s, 'colour', 75, 1, 'computed', 'test', '[]')",
            (series_id,),
        )
    (line,) = converted(site, site.chapter(series_id, pages(2)))
    assert "convert: quality-drift: colour AVIF mean score 82.0 < 83 at q75" in line


@budget(load="pool")
def test_an_irregular_split_image_is_noted(site):
    edit = {**NO_EDIT, "split": True}
    chapter_id = site.chapter(site.series("T", aspect=[1.4]), [("001.jpg", jpeg(colour((100, 330))))], edit=edit)
    (line,) = converted(site, chapter_id)
    assert "convert: split: 1 irregular image(s): 001.jpg" in line, "2.36 pages tall: off the snap window"


@budget(load="pool")
def test_a_staged_file_under_10_percent_of_its_download_is_noted(site):
    noise = Image.fromarray(np.random.default_rng(1).integers(0, 255, (900, 900, 3), dtype=np.uint8), "RGB")
    members = [("001.jpg", jpeg(colour())), ("002.png", png(noise))]
    (line,) = converted(site, site.chapter(site.series(), members, {**NO_EDIT, "drop_pages": [1]}))
    assert "convert: staged file " in line
    assert "< 10% of the download's" in line


@budget(load="pool")
def test_an_untouched_flag_mix_is_noted(site):
    members = [("001.jpg", jpeg(colour())), ("002.gif", encoded(colour(seed=3), "GIF"))]
    (line,) = converted(site, site.chapter(site.series(), members))
    assert "convert: untouched-flag: formats GIF+JPEG mixed" in line


@budget(load="pool")
def test_a_jpeg_of_another_chroma_subsampling_is_noted(site, monkeypatch):
    monkeypatch.setattr(images, "odd_jpeg_chroma", lambda _data: True)  # Pillow writes no 4:4:0 JPEG
    members = [("001.jpg", encoded(colour(), "JPEG", quality=92, subsampling=0))]
    (line,) = converted(site, site.chapter(site.series(), members))
    assert "convert: JPEG chroma subsampling encoded as 4:4:4: 001.jpg" in line


@budget(load="pool")
def test_a_cover_page_a_volume_cover_replaced_is_no_chroma_note(site, monkeypatch):
    monkeypatch.setattr(images, "odd_jpeg_chroma", lambda _data: True)
    full = {"quality": 92, "subsampling": 0}
    members = [
        ("000_cover.jpg", encoded(colour(seed=5), "JPEG", **full)),
        ("001.jpg", encoded(colour(), "JPEG", **full)),
    ]
    chapter_id = site.chapter(site.series(), members)
    add_cover(site, chapter_id, COVER)
    (line,) = converted(site, chapter_id)
    assert "convert: JPEG chroma subsampling encoded as 4:4:4: 001.jpg" in line, "the replaced 000_cover is not named"


@budget(load="pool")
def test_odd_chroma_is_noted_only_for_colour_pages_of_an_avif_outcome(site, monkeypatch):
    monkeypatch.setattr(images, "odd_jpeg_chroma", lambda _data: True)
    full = {"quality": 92, "subsampling": 0}
    gray_rgb = [("001.jpg", encoded(gray().convert("RGB"), "JPEG", **full))]
    found = converted(site, site.chapter(site.series("G"), gray_rgb))
    assert not any("chroma" in n for n in found), "a gray page encodes 4:0:0"
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    colour_jpeg = [("001.jpg", encoded(colour(), "JPEG", **full))]
    found = converted(site, site.chapter(site.series("L"), colour_jpeg))
    assert not any("chroma" in n for n in found), "a lossless page keeps its own subsampling"


@budget(load="pool")
def test_a_cover_page_a_volume_cover_replaced_is_left_out_of_the_drift_mean(site):
    series_id = site.series()
    with db.transaction(site.conn):
        site.conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why, samples)"
            " VALUES (%s, 'colour', 75, 1, 'computed', 'test', '[]')",
            (series_id,),
        )
    fake.SCORE[0] = lambda ref, _dec: 80.0 if "000_cover" in Path(ref).name else 84.0  # the pages' mean is 84
    members = [("000_cover.jpg", jpeg(colour(seed=5))), *pages(2)]
    chapter_id = site.chapter(series_id, members)
    add_cover(site, chapter_id, COVER)
    assert not any("quality-drift" in n for n in converted(site, chapter_id)), "the replaced page is not the chapter's"


@budget(load="pure")
def test_layout_claims_name_their_source():
    assert strip.layout_claims(["Long Strip", "Manga"], "stored") == [
        ("stored genre", "strip", "Long Strip"),
        ("stored origin", "pages", "Manga"),
    ]
    assert strip.layout_claims(["Manhwa", "Manhua"], "stored") == [], "2 origins: no origin claim"


@budget(load="pure")
def test_page_aspects_differ_to_2_decimals():
    assert strip.same_aspects([1.421, 1.5], [1.424, 1.5]), "the same to 2 decimals"
    assert not strip.same_aspects([1.42], [1.435]), "1.42 and 1.44"
    assert not strip.same_aspects([1.42, 1.5], [1.5, 1.42]), "the order counts: the first is the split's"


@budget(load="pure")
def test_a_4_4_0_jpeg_header_is_an_odd_chroma():
    data = bytearray(encoded(colour(), "JPEG", quality=92, subsampling=0))
    sof = data.index(b"\xff\xc0")
    assert data[sof + 11] == 0x11, "4:4:4: the luma component samples 1x1"
    assert not images.odd_jpeg_chroma(bytes(data))
    data[sof + 11] = 0x12  # luma 1x2: 4:4:0
    assert images.odd_jpeg_chroma(bytes(data)), "4:4:0 is no subsampling avifenc keeps"
    assert not images.odd_jpeg_chroma(jpeg(gray())), "a gray JPEG has no chroma"


@budget(load="pool")
def test_animated_pages_converted_for_a_volume_cover_are_noted(site):
    frames = [colour(seed=1).convert("P"), colour(seed=2).convert("P")]
    gif = encoded(frames[0], "GIF", save_all=True, append_images=frames[1:])
    chapter_id = site.chapter(site.series(), [("001.gif", gif)])
    add_cover(site, chapter_id, COVER)
    (line,) = converted(site, chapter_id)
    assert "convert: animated pages converted to 1 frame for the volume cover: 001.gif" in line


@budget(load="pool")
def test_the_chapter_that_fixes_a_calibration_group_is_noted(site):
    series_id = site.series()
    lines = [converted(site, site.chapter(series_id, pages(8, seed=s))) for s in range(1, 4)]
    fixing = [line for found in lines for line in found if "calibration: colour fixed at q" in line]
    assert len(fixing) == 1, "1 chapter fixes the group, and only it is noted"
    assert "; samples of chapters " in fixing[0], "the chapters its samples came from"


@budget(load="pool")
def test_a_retried_chapter_that_fixed_its_group_is_still_noted(site, monkeypatch):
    series_id = site.series()
    for s in (1, 2):
        converted(site, site.chapter(series_id, pages(8, seed=s)))
    third = site.chapter(series_id, pages(8, seed=3))
    real, failed = check.check, []

    def first_fails(*args):
        if not failed:  # the fixing attempt fails after its samples are stored
            failed.append(1)
            return ["forced"]
        return real(*args)

    monkeypatch.setattr(check, "check", first_fails)
    with pytest.raises(RuntimeError, match="forced"):
        site.run(third)
    assert calibration(site, series_id)["colour"]["fixed"] == 1, "the failed attempt fixed the group"
    (line,) = converted(site, third)
    assert "calibration: colour fixed at q" in line, "the retry of the fixing chapter writes its note"


def specks():
    """Return a gray page with 2 faint colour specks, each split across the 8 px colour tiles: it classes gray.

    A crop of 1 to 7 rows lines them up with the tiles: the cropped page classes colour (2 marks are no badge).
    """
    a = np.full((256, 96, 3), 200, np.uint8)
    for y in (4, 204):
        a[y : y + 8, 4:12] = (240, 160, 200)  # spread 80: 20 per tile when split 4 ways
    return a


@budget(load="pool")
def test_a_gray_rendered_page_that_classes_colour_after_its_crop_is_kept_exact(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    page = specks()
    classify = images.classify
    assert classify(page, images.COLOUR_BLOCK, images.TINT_P99, images.MARK_LIMITS)[0] == images.GRAY
    assert classify(page[4:], images.COLOUR_BLOCK, images.TINT_P99, images.MARK_LIMITS)[0] == images.COLOUR
    members = [("001.png", png(Image.fromarray(page, "RGB"))), ("002.png", png(gray(seed=4).convert("RGB")))]
    edit = {**NO_EDIT, "bands": [{"page": 0, "box": [0, 4, 96, 256]}]}  # the region kept: 4 rows cut
    result, out = staged_result(site, site.chapter(site.series(), members, edit))
    assert result["outcome"] == "lossless", "the post-check passes: the page is kept colour, compared exactly"
    assert result["gray"] is False, "BlackAndWhite: a page kept in colour is not gray"
    assert out[0]["height"] == 252


@budget(load="pool")
def test_a_page_a_volume_cover_replaced_counts_in_no_note(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    members = [
        ("000_cover.jpg", jpeg(colour(seed=5))),
        ("001.png", png(colour(seed=1))),
        ("002.png", png(colour(seed=2))),
    ]
    chapter_id = site.chapter(site.series(), members)
    add_cover(site, chapter_id, COVER)
    (line,) = converted(site, chapter_id)
    assert "published lossless" in line
    assert "mixed-format" not in line, "the replaced JPEG cover page is not a page of the staged file"


@budget(load="pool")
def test_a_chapter_whose_only_page_a_volume_cover_replaced_gets_no_mixed_format_note(site):
    # the volume cover's AVIF passes (work/cover), the chapter's own page's fails (work/encode): it goes out lossless
    fake.SCORE[0] = lambda ref, _dec: 50.0 if "encode" in Path(ref).parts else 90.0
    chapter_id = site.chapter(site.series(), [("000_cover.png", png(colour(seed=5)))])
    add_cover(site, chapter_id, COVER)
    found = converted(site, chapter_id)
    result = json.loads(chapter(site.conn, chapter_id)["convert_result"])
    assert (result["outcome"], result["cover"]["format"]) == ("lossless", "AVIF"), "the cover replaced the only page"
    assert not any("mixed-format" in line for line in found), "no page of its own is left: no format to list"


@budget(load="pool")
def test_an_animated_page_of_a_rendered_chapter_is_no_cover_note(site):
    edit = {**NO_EDIT, "bands": [{"page": 0, "box": [0, 0, 96, 64]}]}
    members = [("001.jpg", jpeg(colour())), ("002.gif", encoded(colour(seed=3), "GIF"))]
    found = converted(site, site.chapter(site.series(), members, edit))
    assert not any("volume cover" in line for line in found), "only a volume cover's force_avif is noted"


@budget(load="pool")
def test_a_retried_convert_replaces_its_notes_and_keeps_junks(site):
    fake.SCORE[0] = lambda _ref, _dec: 50.0
    chapter_id = site.chapter(site.series(), [("001.jpg", jpeg(colour())), ("002.png", png(colour(seed=3)))])
    with db.transaction(site.conn):
        site.conn.execute(
            "UPDATE chapter SET notes = %s WHERE id = %s",
            (json.dumps(["rest of card mixed: piece 1", "convert: an old note"]), chapter_id),
        )
    site.record(site.run(chapter_id))
    kept = json.loads(chapter(site.conn, chapter_id)["notes"])
    assert kept[0] == "rest of card mixed: piece 1", "junk's note stays"
    assert "convert: an old note" not in kept, "convert's earlier notes are replaced"
    assert any(n.startswith("convert: AVIF rejected") for n in kept)


@budget(load="db")
def test_notes_of_another_prefix_are_refused(site):
    chapter_id = site.chapter(site.series(), pages(1))
    with pytest.raises(ValueError, match="not starting with"), db.transaction(site.conn):
        workflow.replace_notes(site.conn, chapter_id, "convert: ", ["junk: x"])


@budget(load="db")
def test_status_lists_done_chapters_notes_of_the_last_7_days_and_counts_older_ones(site):
    series_id = site.series()
    old, recent, open_ = (site.chapter(series_id, pages(1)) for _ in range(3))
    now = datetime.fromisoformat(db.now(site.conn))
    with db.transaction(site.conn):
        for chapter_id, days, state in ((old, 8, "done"), (recent, 1, "done"), (open_, 30, "ready")):
            site.conn.execute(
                "UPDATE chapter SET notes = %s, status = %s, updated_at = %s WHERE id = %s",
                (json.dumps([f"note {chapter_id}"]), state, (now - timedelta(days=days)).isoformat(), chapter_id),
            )
    assert listed(site, old) == [], "a done chapter updated 8 days ago is not listed"
    assert listed(site, recent) == [f"note {recent}"]
    assert listed(site, open_) == [f"note {open_}"], "an open chapter is listed however old"
    assert "  1 done chapter(s) with notes, updated over 7 days ago: not listed" in status.report(site.conn)
