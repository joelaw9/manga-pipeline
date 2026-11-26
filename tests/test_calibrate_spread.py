"""The calibration spread (scripts/calibrate_spread.py; SPEC "Calibration").

An adopted series calibrated as the bulk converter did, from 3 of its files spread over the series. Tools and scorer
are the fakes.
"""

import errno
import json
import re
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import archive, config, db, images, preflight
from manga_pipeline.convert import calibrate, image, tools, vship
from manga_pipeline.stages import convert
from scripts import calibrate_spread

from tests.builders import add_chapter, add_series, adopted, config_file, present, running
from tests.convert_pages import colour, gray, jpeg, webp
from tests.fakes import convert as fake
from tests.timing import budget

LADDER_SCORES = {80: 95.0, 75: 92.5, 70: 90.0, 65: 87.5, 60: 85.0, 55: 82.5, 50: 80.0}  # q55: mean 82.5 < 85


def by_quality(_ref, dec):
    """Score a decode by the quality in its name (`<stem>.q<Q>.png`): q60 is the last step to pass."""
    return LADDER_SCORES[int(present(re.search(r"\.q(\d+)\.png$", dec.name)).group(1))]


@pytest.fixture
def site(tmp_path):
    cfg = adopted(tmp_path)
    fake.SCORE[0] = by_quality
    with closing(db.connect(cfg)) as conn:
        yield Site(cfg, conn, tmp_path)


class Site:
    """An adopted site with builders of adopted series and their files, and the script's run."""

    def __init__(self, cfg, conn, tmp_path):
        """Store the site's config, connection and test folder."""
        self.cfg, self.conn, self.tmp_path = cfg, conn, tmp_path

    def series(self, name="S", layout="pages", aspect=None):
        """Add an adopted series with its series_conversion row; return its id."""
        series_id = add_series(self.conn, name, origin="kavita")
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO series_conversion (series_id, layout, page_aspect, origin)"
                " VALUES (%s, %s, %s, 'computed')",
                (series_id, layout, None if aspect is None else json.dumps(aspect)),
            )
        return series_id

    def file(self, series_id, n, members):
        """Add adopted file `Ch <n>.cbz` of the series holding `members` (bytes: the file's own); return its id."""
        path = config.abs_path(self.cfg["paths"]["library"]) / f"S{series_id}" / f"Ch {n}.cbz"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(members, bytes):
            path.write_bytes(members)
        else:
            archive.write(path, members)
        with db.transaction(self.conn):
            return self.conn.execute(
                """INSERT INTO published_file (library_path, series_id, identity, state, origin, comicinfo_number)
                VALUES (%s, %s, %s, 'confirmed', 'library', %s) RETURNING id""",
                (config.to_stored(self.cfg, path), series_id, f'["", null, "", "{n}", ""]', str(n)),
            ).fetchone()["id"]

    def fixed_group(self, series_id, q, origin, why):
        """Store the series' colour group, fixed."""
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO series_calibration (series_id, grp, q, fixed, origin, why)"
                " VALUES (%s, 'colour', %s, 1, %s, %s)",
                (series_id, q, origin, why),
            )

    def main(self, *args):
        """Run the script's main on this site; return its exit code."""
        return calibrate_spread.main([*args, "--config", str(config_file(self.tmp_path, self.cfg))])

    def groups(self, series_id):
        """Return the series' stored calibration groups."""
        return calibrate.rows(self.conn, series_id)


def pages(n, seed=0, page=colour, size=(96, 128)):
    return [(f"{i:03d}.jpg", jpeg(page(size=size, seed=seed * 100 + i))) for i in range(1, n + 1)]


def lossy_webp(n, seed=0, size=(96, 128)):
    return [(f"{i:03d}.webp", webp(colour(size=size, seed=seed * 100 + i))) for i in range(1, n + 1)]


# ---------------------------------------------------------------- the bulk converter's selection


@budget(load="pure")
def test_spread_and_share_follow_the_bulk_converter():
    assert calibrate_spread.spread(46) == [0, 22, 45], "first, middle and last of 46"
    assert calibrate_spread.spread(2) == [0, 1], "fewer than 3: all"
    lengths = [len(c) for c in calibrate_spread.share([list(range(10)), list(range(30)), list(range(30))])]
    assert lengths == [10, 15, 15], "40 shared shortest first: a short file's shortfall goes to the others"


@budget(load="pool", seconds=20)
def test_a_page_series_is_fixed_from_3_of_its_usable_files_and_every_group_gets_a_row(site, capsys):
    series_id = site.series()
    first = site.file(series_id, 1, pages(8, seed=1))
    site.file(series_id, 2, lossy_webp(8, seed=2))  # untouched route: no AVIF page, not usable
    for n in (3, 4, 5):
        site.file(series_id, n, pages(8, seed=n))
    add_chapter(site.conn, series_id, '["", null, "", "1", ""]', "junk", published_file_id=first)  # an open conversion
    assert site.main() == 0
    rows = site.groups(series_id)
    assert (rows["colour"]["q"], rows["colour"]["fixed"], rows["colour"]["samples"]) == (60, 1, [])
    assert rows["colour"]["why"] == (
        "spread: q55 fails: mean 82.5 < 85 (24 samples); files Ch 1.cbz, Ch 4.cbz, Ch 5.cbz"
    ), "3 of the 4 usable files, first, middle and last; the open conversion's file is read"
    assert (rows["gray"]["q"], rows["gray"]["fixed"]) == (80, 1), "a group with no unit in the 3 files: q80, fixed"
    assert rows["gray"]["why"].startswith("spread: q80: 0 sample pages < 20")
    assert "series: 1; spread and written: 1; failed (run again): 0; no usable file: 0" in capsys.readouterr().out


@budget(load="pool", seconds=20)
def test_exactly_20_units_in_the_3_files_are_probed(site):
    series_id = site.series()
    for n, count in ((1, 7), (2, 7), (3, 6)):
        site.file(series_id, n, pages(count, seed=n))
    assert site.main() == 0
    assert site.groups(series_id)["colour"]["q"] == 60, "20 sample pages fix the group by the rule, not at q80"


@budget(load="pool", seconds=20)
def test_a_series_with_no_usable_file_writes_nothing(site, capsys):
    series_id = site.series()
    site.file(series_id, 1, lossy_webp(4, seed=1))
    site.file(series_id, 2, b"not a zip archive")  # zipfile's own error, archive.UNREADABLE
    assert site.main() == 0
    assert site.groups(series_id) == {}
    out = capsys.readouterr().out
    assert (
        f"series {series_id} (S, pages): no usable file; nothing written; left out Ch 2.cbz: cannot be measured" in out
    )
    assert "no usable file: 1" in out


@budget(load="pool", seconds=20)
def test_every_group_samples_from_the_same_3_files_picked_once(site):
    series_id = site.series()
    for n in range(1, 6):
        extra = pages(8, seed=50 + n, page=gray) if n in (2, 3, 4) else []
        site.file(series_id, n, pages(8, seed=n) + [(f"g{name}", data) for name, data in extra])
    assert site.main() == 0
    rows = site.groups(series_id)
    assert rows["colour"]["why"].endswith("files Ch 1.cbz, Ch 3.cbz, Ch 5.cbz")
    assert rows["gray"]["q"] == 80, "gray takes its pages from the 3 files only: 8 of its 24, fewer than 20"
    assert rows["gray"]["why"].startswith("spread: q80: 8 sample pages < 20")


@budget(load="pool", seconds=20)
def test_a_page_repeated_in_2_picked_files_is_probed_once_and_counted_each_time(site):
    series_id = site.series()
    shared = pages(2, seed=9)
    site.file(series_id, 1, pages(5, seed=1) + [(f"x{n}", d) for n, d in shared])
    site.file(series_id, 2, pages(7, seed=2))
    site.file(series_id, 3, pages(5, seed=3) + [(f"x{n}", d) for n, d in shared])
    fake.CALLS.clear()
    assert site.main() == 0
    row = site.groups(series_id)["colour"]
    assert (row["q"], row["why"].split(";")[0]) == (60, "spread: q55 fails: mean 82.5 < 85 (21 samples)")
    avifenc = sum(name == "avifenc" for name, _ in fake.CALLS)
    assert avifenc == 19 * 7, "19 distinct pages, each encoded at q80 and the 6 ladder steps once"


@budget(load="pool", seconds=20)
def test_no_probe_holds_2_units_of_1_page_name(site, monkeypatch):
    probe, calls = calibrate.probe, []

    def recorded(encoder, picked, work_dir):  # probe names its work files by the unit's stem
        calls.append([Path(u["path"]).stem for u in picked])
        return probe(encoder, picked, work_dir)

    monkeypatch.setattr(calibrate, "probe", recorded)
    series_id = site.series()
    for n in (1, 2, 3):
        site.file(series_id, n, pages(8, seed=n))  # every file's pages are 001 ... 008
    assert site.main() == 0
    assert site.groups(series_id)["colour"]["q"] == 60
    assert len(calls) == 3, "1 probe per picked file"
    assert all(len(set(stems)) == len(stems) for stems in calls), "no 2 units of 1 stem in 1 probe"


@budget(load="pool", seconds=20)
def test_a_stitched_file_of_lossy_webp_pages_is_usable_at_the_aspect_its_series_files_give(site):
    series_id = site.series()  # no stored aspect: square pages give 1.0, so a page 2.0 tall is 2 stitched pages
    site.file(series_id, 1, pages(8, seed=1, size=(96, 96)))
    site.file(series_id, 2, lossy_webp(6, seed=2, size=(96, 192)))
    site.file(series_id, 3, pages(8, seed=3, size=(96, 96)))
    assert site.main() == 0
    row = site.groups(series_id)["colour"]
    assert row["why"].endswith("files Ch 1.cbz, Ch 2.cbz, Ch 3.cbz"), "split, it renders: usable (unsplit: untouched)"
    assert "(28 samples)" in row["why"], "8 + 6 split in 2 + 8 units"


@budget(load="pool", seconds=20)
def test_a_picked_strip_file_that_gives_no_unit_is_replaced_by_another(site, capsys):
    series_id = site.series(layout="strip")
    site.file(series_id, 1, [("cover.jpg", jpeg(colour(seed=7)))])  # cover pages only
    site.file(series_id, 2, pages(8, seed=2))
    site.file(series_id, 3, [("001.jpg", b"not an image")])
    site.file(series_id, 4, pages(8, seed=4))
    site.file(series_id, 5, pages(8, seed=5))
    assert site.main() == 0
    assert site.groups(series_id)["colour"]["why"].endswith("files Ch 2.cbz, Ch 4.cbz, Ch 5.cbz")
    out = capsys.readouterr().out
    assert "left out Ch 1.cbz: no AVIF page" in out
    assert "left out Ch 3.cbz: cannot be" in out


@budget(load="pool", seconds=20)
def test_a_picked_file_that_cannot_be_rendered_is_replaced_by_another(site, monkeypatch, capsys):
    series_id = site.series(layout="strip")
    for n in range(1, 5):
        site.file(series_id, n, pages(8, seed=n))
    render = image.render

    def render_or_fail(chapter_plan, x_dir, out_dir):
        if chapter_plan.name == "Ch 1.cbz":
            msg = "image file is truncated"  # Pillow's: an OSError with no errno, the file's fault
            raise OSError(msg)
        return render(chapter_plan, x_dir, out_dir)

    monkeypatch.setattr(image, "render", render_or_fail)
    assert site.main() == 0
    assert site.groups(series_id)["colour"]["why"].endswith("files Ch 2.cbz, Ch 3.cbz, Ch 4.cbz")
    assert "left out Ch 1.cbz: cannot be rendered" in capsys.readouterr().out


@budget(load="pool", seconds=20)
def test_a_machine_error_on_a_file_fails_its_series_and_the_run_goes_on(site, monkeypatch, capsys):
    denied, fine = site.series("Denied", layout="strip"), site.series("Fine", layout="strip")
    for series_id in (denied, fine):
        for n in (1, 2, 3):
            site.file(series_id, n, pages(8, seed=series_id * 10 + n))
    render = image.render

    def render_or_deny(chapter_plan, x_dir, out_dir):
        if f"s{denied}" in Path(x_dir).parts:
            msg = "scratch denied"
            raise OSError(errno.EACCES, msg)
        return render(chapter_plan, x_dir, out_dir)

    monkeypatch.setattr(image, "render", render_or_deny)
    assert site.main() == 0
    assert site.groups(denied) == {}, "an errno is the machine's: nothing written, not a file left out"
    assert site.groups(fine)["colour"]["fixed"] == 1
    out = capsys.readouterr().out
    assert f"series {denied} (Denied, strip): failed, nothing written:" in out
    assert "failed (run again): 1" in out


@budget(load="pool", seconds=20)
def test_a_strip_series_whose_every_file_fails_writes_nothing(site, capsys):
    series_id = site.series(layout="strip")
    site.file(series_id, 1, [("cover.jpg", jpeg(colour(seed=7)))])
    site.file(series_id, 2, b"not a zip archive")
    assert site.main() == 0
    assert site.groups(series_id) == {}
    out = capsys.readouterr().out
    assert f"series {series_id} (S, strip): no usable file; nothing written; left out Ch 1.cbz: no AVIF page" in out


@budget(load="pool", seconds=20)
def test_2_groups_of_20_or_more_are_probed_in_1_call_per_file(site, monkeypatch):
    probe, calls = calibrate.probe, []

    def recorded(encoder, picked, work_dir):
        calls.append(len(picked))
        return probe(encoder, picked, work_dir)

    monkeypatch.setattr(calibrate, "probe", recorded)
    series_id = site.series()
    for n in (1, 2, 3):
        mixed = pages(7, seed=n) + [(f"g{name}", data) for name, data in pages(7, seed=50 + n, page=gray)]
        site.file(series_id, n, mixed)
    assert site.main() == 0
    rows = site.groups(series_id)
    assert (rows["gray"]["q"], rows["colour"]["q"]) == (60, 60), "21 units each: both probed"
    assert calls == [14, 14, 14], "1 probe per picked file, both groups' units in it"


@budget(load="pure")
def test_results_cancels_the_queued_jobs_when_one_fails():
    failed, queued = Future(), Future()
    failed.set_exception(ValueError("broken"))
    with pytest.raises(ValueError, match="broken"):
        calibrate_spread.results([failed, queued])
    assert queued.cancelled(), "the queued job is cancelled, not left to run"


@budget(load="pool", seconds=20)
def test_a_rerun_skips_a_spread_series_refixes_an_own_chapter_group_and_leaves_a_record_group(site):
    done, own, record = site.series("Done"), site.series("Own"), site.series("Record")
    for series_id in (done, own, record):
        for n in (1, 2, 3):
            site.file(series_id, n, pages(8, seed=series_id * 10 + n))
    site.fixed_group(done, 70, "computed", "spread: kept")
    site.fixed_group(own, 75, "computed", "q70 fails: ...")
    site.fixed_group(record, 65, "record", "record: q60 fails")
    assert site.main() == 0
    assert site.groups(done)["colour"]["why"] == "spread: kept", "a series holding a spread: group is done"
    assert "gray" not in site.groups(done)
    assert site.groups(own)["colour"]["q"] == 60, "own-chapter sampling's q is replaced (D2 (a))"
    kept = site.groups(record)["colour"]
    assert (kept["q"], kept["origin"], kept["why"]) == (65, "record", "record: q60 fails"), "a record's group stands"
    assert site.groups(record)["gray"]["why"].startswith("spread: q80")


@budget(load="pool", seconds=20)
def test_a_share_error_or_an_encode_error_writes_nothing_for_its_series_and_the_run_goes_on(site, monkeypatch, capsys):
    dropped, failing, fine = site.series("Dropped"), site.series("Failing"), site.series("Fine")
    for series_id in (dropped, failing, fine):
        for n in (1, 2, 3):
            site.file(series_id, n, pages(8, seed=series_id * 10 + n))
    measure, probe = images.measure_pages, calibrate.probe

    def measure_or_drop(cbz, *args, **kwargs):
        if Path(cbz).parent.name == f"S{dropped}":
            raise OSError(errno.EIO, "share gone")
        return measure(cbz, *args, **kwargs)

    def probe_or_fail(encoder, picked, work_dir):
        if f"s{failing}" in Path(work_dir).parts:
            raise tools.ToolError(1, ["avifenc"], "", "broken")
        return probe(encoder, picked, work_dir)

    monkeypatch.setattr(images, "measure_pages", measure_or_drop)
    monkeypatch.setattr(calibrate, "probe", probe_or_fail)
    assert site.main() == 0
    assert site.groups(dropped) == {}
    assert site.groups(failing) == {}
    assert site.groups(fine)["colour"]["q"] == 60
    out = capsys.readouterr().out
    assert f"series {dropped} (Dropped, pages): failed, nothing written: share" in out
    assert f"series {failing} (Failing, pages): failed, nothing written:" in out


@budget(load="pool", seconds=20)
def test_a_dry_run_writes_nothing_and_series_narrows_the_run(site, capsys):
    one, other = site.series("One"), site.series("Other")
    for series_id in (one, other):
        for n in (1, 2, 3):
            site.file(series_id, n, pages(8, seed=series_id * 10 + n))
    assert site.main("--dry-run", "--series", str(one)) == 0
    assert site.groups(one) == {}
    out = capsys.readouterr().out
    assert f"series {one} (One, pages): gray q80" in out
    assert "colour q60 (q55 fails" in out
    assert f"series {other}" not in out
    assert "series: 1; spread (dry run: nothing written): 1" in out


@budget(load="pool")
def test_a_listed_series_that_is_not_taken_is_named(site, capsys):
    series_id = site.series(layout="strip")  # no adopted file
    assert site.main("--series", str(series_id)) == 0
    assert f"series {series_id}: not taken" in capsys.readouterr().out


@budget(load="pool")
def test_a_failed_preflight_check_exits_4_before_anything(site, monkeypatch):
    series_id = site.series()
    site.file(series_id, 1, pages(8, seed=1))
    full = preflight.Check("scratch space", preflight.HARD, lambda _cfg: "no space")
    monkeypatch.setattr(calibrate_spread, "CHECKS", (full,))
    assert site.main() == 4
    assert site.groups(series_id) == {}


@budget(load="pool")
def test_a_part_running_exits_3_and_a_scorer_that_does_not_load_exits_1_before_any_read(site, monkeypatch):
    series_id = site.series()
    site.file(series_id, 1, pages(8, seed=1))
    with running(site.cfg, "process"):
        assert site.main() == 3
    read = []
    monkeypatch.setattr(images, "measure_pages", lambda *a, **_k: read.append(a) or [])

    def no_gpu(_kind, _path):
        msg = "no GPU"
        raise vship.VshipError(msg)

    monkeypatch.setattr(convert, "loaded_scorer", no_gpu)
    assert site.main() == 1
    assert read == [], "nothing read"
    assert site.groups(series_id) == {}
