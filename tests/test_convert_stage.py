"""The convert stage (stages/convert.py; SPEC "Conversion", "Calibration"): its staged file and record, the program
errors that need a human, scratch life, the post-check, and the calibration of a series arriving a few chapters at a
time. Tools and scorer are the fakes; each test inserts its own chapter_edit row."""

import errno
import json
import re
import zipfile
from contextlib import closing

import pytest

from manga_pipeline import archive, config, db, page_store, workflow
from manga_pipeline.convert import calibrate, check
from manga_pipeline.stages import convert

from tests.builders import add_chapter, add_series, adopted, chapter, download
from tests.convert_pages import NO_EDIT, colour, gray, jpeg
from tests.fakes import convert as fake
from tests.timing import budget


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        yield Site(cfg, conn)


class Site:
    def __init__(self, cfg, conn):
        self.cfg, self.conn = cfg, conn
        self.ctx = workflow.Context(cfg, conn, "process", suwayomi=object(), kavita=object())
        self.n = 0

    def series(self, name="S", layout="pages", aspect=None):
        series_id = add_series(self.conn, name)
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO series_conversion (series_id, layout, page_aspect, origin) VALUES (?, ?, ?, 'computed')",
                (series_id, layout, None if aspect is None else json.dumps(aspect)),
            )
        return series_id

    def chapter(self, series_id, members, edit=NO_EDIT):
        self.n += 1
        path = download(self.cfg, "S", f"Ch {self.n}")
        archive.write(path, members)
        chapter_id = add_chapter(
            self.conn, series_id, f'["", null, "", "{self.n}", ""]', stage="convert", download_path=str(path)
        )
        if edit is not None:
            with db.transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO chapter_edit (chapter_id, edit) VALUES (?, ?)",
                    (chapter_id, edit if isinstance(edit, str) else json.dumps(edit)),
                )
        return chapter_id

    def run(self, chapter_id):
        return convert.run(self.ctx, chapter(self.conn, chapter_id))

    def record(self, outcome):
        with db.transaction(self.conn):
            outcome.record(self.conn)

    def scratch(self, chapter_id):
        return config.abs_path(self.cfg["paths"]["scratch"]) / str(chapter_id)


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
    staged = config.abs_path(site.cfg["paths"]["staging"]) / f"{db.database_id(site.conn)[:8]}-{chapter_id}.cbz"
    assert row["staged_path"] == str(staged) and row["staged_size"] == staged.stat().st_size
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


@budget(load="pool")
def test_a_scratch_folder_left_by_a_killed_run_is_replaced(site):
    chapter_id = site.chapter(site.series(), pages(1))
    left = site.scratch(chapter_id) / "x" / "999.jpg"
    left.parent.mkdir(parents=True)
    left.write_bytes(b"left over")
    site.record(site.run(chapter_id))
    assert json.loads(chapter(site.conn, chapter_id)["convert_result"])["pages_in"] == 1, "the leftover is not a page"
    assert not site.scratch(chapter_id).exists()


# ---------------------------------------------------------------- program errors and failures


@budget(load="pool")
def test_a_chapter_with_no_edit_row_needs_a_human(site):
    chapter_id = site.chapter(site.series(), pages(1), edit=None)
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman) and "no chapter_edit row" in outcome.reason


@budget(load="pool")
def test_a_malformed_edit_needs_a_human_naming_the_key(site):
    chapter_id = site.chapter(site.series(), pages(1), edit={**NO_EDIT, "split": "no"})
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman) and "split must be a bool" in outcome.reason


@budget(load="pool")
def test_a_series_without_a_layout_fails(site):
    chapter_id = site.chapter(add_series(site.conn, "No layout"), pages(1))
    with pytest.raises(RuntimeError, match="has no layout"):
        site.run(chapter_id)


@budget(load="pool")
def test_an_exif_rotated_page_needs_a_human_and_leaves_no_scratch(site):
    from PIL import Image

    exif = Image.Exif()
    exif[0x0112] = 6
    import io

    buf = io.BytesIO()
    colour().save(buf, "JPEG", exif=exif.tobytes())
    chapter_id = site.chapter(site.series(), [("001.jpg", buf.getvalue())])
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman) and "EXIF orientation 6" in outcome.reason
    assert not site.scratch(chapter_id).exists()


@budget(load="pool")
def test_a_stale_stored_measurement_fails_the_chapter(site):
    chapter_id = site.chapter(site.series(), pages(1))
    from manga_pipeline import images

    stale = images.measure_pages(chapter(site.conn, chapter_id)["download_path"])
    stale[0]["sha1"] = "0" * 40
    with db.transaction(site.conn):
        page_store.save(site.conn, chapter_id, stale)
    with pytest.raises(ValueError, match="known sha1"):
        site.run(chapter_id)


@budget(load="pool")
def test_a_failed_post_check_deletes_the_staged_file_and_fails(site, monkeypatch):
    monkeypatch.setattr(check, "check", lambda *a: ["page-count: 1 != expected 2"])
    chapter_id = site.chapter(site.series(), pages(1))
    with pytest.raises(RuntimeError, match="post-check failed: page-count"):
        site.run(chapter_id)
    assert list(config.abs_path(site.cfg["paths"]["staging"]).glob("*.cbz")) == []
    assert not site.scratch(chapter_id).exists()


@budget(load="pool")
def test_a_share_error_while_staging_reaches_the_workflow_as_an_oserror(site, monkeypatch):
    def eio(*a):
        raise OSError(errno.EIO, "Input/output error")

    chapter_id = site.chapter(site.series(), pages(1))
    monkeypatch.setattr(archive, "write", eio)
    with pytest.raises(OSError) as e:
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
    rec = {
        "outcome": "untouched",
        "gate": None,
        "lossless": {},
        "scores": {},
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}, {"name": "002.jpg", "sha1": archive.sha1(b)}],
    }
    kept = [{"name": "001.jpg", "bytes": len(a)}, {"name": "002.jpg", "bytes": len(b)}]
    assert check.check(staged(tmp_path, [("001.jpg", a), ("002.jpg", b)]), rec, tmp_path, kept) == []
    assert any("page-count" in f for f in check.check(staged(tmp_path, [("001.jpg", a)]), rec, tmp_path, kept))
    swapped = check.check(staged(tmp_path, [("001.jpg", b), ("002.jpg", a)]), rec, tmp_path, kept)
    assert "page-content-mismatch" in swapped
    extra = check.check(staged(tmp_path, [("001.jpg", a), ("002.jpg", b), ("notes.txt", b"x")]), rec, tmp_path, kept)
    assert any(f.startswith("unexpected-files") for f in extra)
    wrong_ext = check.check(staged(tmp_path, [("001.png", a), ("002.jpg", b)]), rec, tmp_path, kept)
    assert any("extension-mismatch: 001.png" in f for f in wrong_ext)
    broken = check.check(staged(tmp_path, [("001.jpg", a[:40]), ("002.jpg", b)]), rec, tmp_path, kept)
    assert any("undecodable-page: 001.jpg" in f for f in broken)


@budget(load="pool")
def test_the_post_check_holds_avif_scores_lossless_pixels_and_size(tmp_path):
    a = jpeg(colour())
    avif = {
        "outcome": "avif",
        "gate": "floor+size",
        "lossless": {},
        "scores": {},
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}],
    }
    fails = check.check(staged(tmp_path, [("001.jpg", a)]), avif, tmp_path, [{"name": "001.jpg", "bytes": 1}])
    assert "score-missing: 001.jpg" in fails and "mixed-formats: JPEG" in fails
    assert any(f.startswith("output-larger-than-input") for f in fails), "page bytes above the source pages'"
    (tmp_path / "001.jpg").write_bytes(jpeg(gray().convert("RGB")))
    lossless = {
        "outcome": "lossless",
        "gate": None,
        "lossless": {"001.jpg": {"source": "001.jpg", "mode": "exact"}},
        "scores": {},
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}],
    }
    fails = check.check(staged(tmp_path, [("001.jpg", a)]), lossless, tmp_path, [{"name": "001.jpg", "bytes": 10**6}])
    assert fails == ["lossless-mismatch (exact): 001.jpg"]


@budget(load="pool")
def test_the_post_check_refuses_a_deflated_archive(tmp_path):
    a = jpeg(colour())
    path = tmp_path / "d.cbz"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("001.jpg", a)
    rec = {
        "outcome": "untouched",
        "gate": None,
        "lossless": {},
        "scores": {},
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}],
    }
    assert check.check(path, rec, tmp_path, [{"name": "001.jpg", "bytes": len(a)}]) == ["not-store-mode"]


# ---------------------------------------------------------------- calibration


def q_scores(by_q):
    """Fake scores from the encode quality in the decode's name: by_q(q)."""

    def f(ref, dec):
        return by_q(int(re.search(r"\.q(\d+)\.png$", dec.name).group(1)))

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


@budget(load="pool")
def test_a_retried_chapter_adds_no_samples_twice(site):
    series_id = site.series()
    chapter_id = site.chapter(series_id, pages(3))
    site.run(chapter_id)
    site.run(chapter_id)
    assert len(calibration(site, series_id)["colour"]["samples"]) == 3


@budget(load="pool", seconds=15)
def test_samples_failing_at_q80_are_not_counted(site):
    low = {"001", "002", "003"}
    fake.SCORE[0] = lambda ref, dec: (
        70.0 if dec.name.split(".")[0] in low else 95 - (80 - int(dec.name.split(".q")[1][:2])) * 0.5
    )
    series_id = site.series()
    for n in range(3):
        site.record(site.run(site.chapter(series_id, pages(7, seed=n))))
    row = calibration(site, series_id)["colour"]
    assert len(row["samples"]) == 21 and row["fixed"] == 0, "18 counted samples are not enough to fix the group"


@budget(load="pure")
def test_a_record_fixes_only_the_groups_it_probed():
    record = {
        "series": {
            "calibration": {
                "groups": {
                    "gray": {"q": 80, "n": 0, "why": "too few sample pages (0)", "probes": []},
                    "colour": {"q": 70, "n": 40, "why": "q65 fails", "probes": [{"q": 75}, {"q": 70}, {"q": 65}]},
                }
            }
        }
    }
    assert calibrate.from_record(record) == {"colour": (70, "record: q65 fails")}
    assert calibrate.from_record({"series": {"calibration": {}}}) == {}, "a record without calibration fixes nothing"


@budget(load="pure")
def test_choose_follows_the_bulk_converters_rule():
    def sample(chapter, by_q):
        return {"chapter_id": chapter, "scores": {str(q): [by_q(q), 1] for q in (80, *calibrate.LADDER)}}

    passing = [sample(c % 3, lambda q: 90.0) for c in range(20)]
    assert calibrate.choose(passing) == (50, "ladder floor q50 passes (20 samples)")
    assert calibrate.choose(passing[:19]) is None, "fewer than 20 counted samples"
    two_chapters = [sample(c % 2, lambda q: 90.0) for c in range(20)]
    assert calibrate.choose(two_chapters) is None, "samples from fewer than 3 chapters"
    one_low = passing[:-1] + [sample(0, lambda q: 90.0 if q >= 70 else 79.0)]
    assert calibrate.choose(one_low)[0] == 70, "a sample under the floor at q65 fails that probe"


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
    assert result["route"] == "rendered-avif" and result["reshape"]["kind"] == "restitch"
    assert out[0]["name"] == "000_cover.avif" and out[0]["height"] == 300, "the cover page stays a page of its own"
    assert sum(p["height"] for p in out[1:]) == 3900, "the strip without its removed 300 rows"


@budget(load="pool")
def test_a_split_chapter_is_split_at_its_series_page_aspect(site):
    edit = {**NO_EDIT, "split": True}
    split_id = site.chapter(site.series("T", aspect=[1.4]), [("001.jpg", jpeg(colour((100, 280))))], edit=edit)
    site.record(site.run(split_id))
    assert json.loads(chapter(site.conn, split_id)["convert_result"])["pages_out"] == 2


@budget(load="pure")
def test_a_group_takes_no_more_than_40_samples():
    units = [{"sha1": str(i)} for i in range(20)]
    assert len(calibrate.pick(units, {f"s{i}" for i in range(30)})) == 10, "room for 10 more"
    assert calibrate.pick(units, {f"s{i}" for i in range(40)}) == [], "a full group takes none"


@budget(load="pool")
def test_2_pages_whose_names_share_a_stem_need_a_human_through_the_stage(site):
    chapter_id = site.chapter(site.series(), [("cover.jpg", jpeg(colour())), ("cover.png", jpeg(gray()))])
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman) and "share a name stem" in outcome.reason
    assert not site.scratch(chapter_id).exists()


@budget(load="fs")
def test_the_post_check_names_a_low_score_folders_and_an_ambiguous_order(tmp_path):
    import io

    buf = io.BytesIO()
    colour().save(buf, "AVIF", quality=80, speed=10)
    avif = buf.getvalue()
    rec = {
        "outcome": "avif",
        "gate": "floor",
        "lossless": {},
        "scores": {"001.avif": 79.5},
        "output": [{"name": "001.avif", "sha1": archive.sha1(avif)}],
    }
    assert check.check(staged(tmp_path, [("001.avif", avif)]), rec, tmp_path, []) == ["score-below-min: 001.avif 79.50"]
    nested = check.check(staged(tmp_path, [("a/001.avif", avif)]), dict(rec, scores={"a/001.avif": 90.0}), tmp_path, [])
    assert "nested-entries" in nested
    a, b, c = (jpeg(colour(seed=s)) for s in (1, 2, 3))
    names = [("1.jpg", a), ("2.jpg", b), ("10.jpg", c)]
    rec = {
        "outcome": "untouched",
        "gate": None,
        "lossless": {},
        "scores": {},
        "output": [{"name": n, "sha1": archive.sha1(d)} for n, d in names],
    }
    fails = check.check(staged(tmp_path, names), rec, tmp_path, [])
    assert fails == ["page-order-ambiguous: natural and lexical order differ"]


@budget(load="pure")
def test_a_repeated_page_is_sampled_once():
    units = [{"sha1": "a"}, {"sha1": "b"}, {"sha1": "a"}, {"sha1": "c"}]
    assert [u["sha1"] for u in calibrate.pick(units, set())] == ["a", "b", "c"]


@budget(load="pool")
def test_lossless_and_untouched_chapters_through_the_stage(site):
    from tests.convert_pages import webp

    fake.SCORE[0] = lambda ref, dec: 50.0
    series_id = site.series()
    lossless = site.chapter(series_id, pages(2))
    untouched = site.chapter(series_id, [("001.webp", webp(colour()))])
    for chapter_id, want in ((lossless, ("avif", "lossless")), (untouched, ("untouched", "untouched"))):
        site.record(site.run(chapter_id))
        result = json.loads(chapter(site.conn, chapter_id)["convert_result"])
        assert (result["route"], result["outcome"]) == want and result["mean_score"] is None


@budget(load="fs")
def test_a_luma_check_on_a_colour_page_is_done_exactly(tmp_path):
    a = jpeg(colour())
    (tmp_path / "001.jpg").write_bytes(a)
    rec = {
        "outcome": "lossless",
        "gate": None,
        "lossless": {"001.jpg": {"source": "001.jpg", "mode": "luma"}},
        "scores": {},
        "output": [{"name": "001.jpg", "sha1": archive.sha1(a)}],
    }
    fails = check.check(staged(tmp_path, [("001.jpg", a)]), rec, tmp_path, [{"name": "001.jpg", "bytes": len(a)}])
    assert fails == ["luma-check-on-colour-page: 001.jpg"], "a colour page may not be compared in luma only"


# ---------------------------------------------------------------- review fixes: edge outcomes and sampling rules


@budget(load="pool")
def test_an_edit_dropping_every_page_needs_a_human(site):
    chapter_id = site.chapter(site.series(), pages(2), edit={**NO_EDIT, "drop_pages": [0, 1]})
    outcome = site.run(chapter_id)
    assert isinstance(outcome, workflow.NeedsHuman) and "drops every page" in outcome.reason


@budget(load="pool")
def test_a_post_check_that_raises_still_removes_the_staged_file(site, monkeypatch):
    def broken(*a):
        raise OSError("source page unreadable")

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
    from tests.convert_pages import webp

    series_id = site.series()
    site.run(site.chapter(series_id, [("001.webp", webp(colour()))]))
    assert calibration(site, series_id) == {}


@budget(load="pool")
def test_a_fixed_group_is_not_probed(site):
    series_id = site.series()
    with db.transaction(site.conn):
        site.conn.execute(
            "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why) "
            "VALUES (?, 'colour', 70, 1, 'record', 'r')",
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
    assert samples and all(s["rendered"] for s in samples), "a rendered chapter samples its rendered pages"


@budget(load="pool")
def test_1_scorer_serves_every_chapter_of_the_process(site):
    convert._scorer.cache_clear()
    fake.FakeScorer.made.clear()
    series_id = site.series()
    for n in range(2):
        site.run(site.chapter(series_id, pages(1, seed=n)))
    assert len(fake.FakeScorer.made) == 1, "the library and its GPU context are loaded once, not per chapter"


@budget(load="pool")
def test_a_leftover_scratch_folder_that_cannot_be_removed_fails_the_stage(site, monkeypatch):
    import shutil

    chapter_id = site.chapter(site.series(), pages(1))
    site.scratch(chapter_id).mkdir(parents=True)

    def locked(path, *a, **k):
        raise PermissionError(13, "in use", str(path))

    monkeypatch.setattr(shutil, "rmtree", locked)
    with pytest.raises(PermissionError):
        site.run(chapter_id)


@budget(load="pool")
def test_output_pages_not_as_many_as_the_outcome_writes_fail(site, monkeypatch):
    from manga_pipeline.convert import image

    real = image._Chapter.untouched

    def extra(self):
        real(self)
        (self.out / "999.webp").write_bytes(b"stray")

    monkeypatch.setattr(image._Chapter, "untouched", extra)
    from tests.convert_pages import webp

    chapter_id = site.chapter(site.series(), [("001.webp", webp(colour()))])
    with pytest.raises(RuntimeError, match="2 output pages, the outcome writes 1"):
        site.run(chapter_id)
