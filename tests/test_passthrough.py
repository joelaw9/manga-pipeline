"""A download of a series Kavita holds, through intake, process and export into its series folder, indexed and
confirmed (SPEC "Publishing", "Chapter stages", "Bad downloads"); PLAN Phase 3 "Done when" on the fakes."""

import json
import os
import time
import zipfile
from contextlib import closing

import pytest

from manga_pipeline import comicinfo, config, db, export, intake, process, workflow
from manga_pipeline.clients import http
from manga_pipeline.stages import comicinfo as comicinfo_stage

from tests.timing import budget
from tests.world import World, bulk_record, cbz
from tests.world import comicinfo as ci


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def through(world, *parts):
    for part in parts or (intake, process, export):
        world.run(part)


def published(world, name):
    return world.library / "Solo Leveling" / name


def comicinfo_of(path):
    with zipfile.ZipFile(path) as z:
        return comicinfo.read_all(z.read("ComicInfo.xml")), [n for n in z.namelist() if n != "ComicInfo.xml"]


def retained(world, path):
    return config.abs_path(world.cfg["paths"]["retention"]) / path.parent.parent.name / path.parent.name / path.name


@pytest.fixture
def solo(world):
    """Solo Leveling in Kavita (bulk chapter 1, a record) and its Suwayomi manga."""
    kavita_id = world.kavita_series("Solo Leveling", localized="Na Honjaman Level Up")
    return kavita_id, world.manga("Solo Leveling")


@budget(load="db", seconds=2)
def test_a_download_lands_in_its_series_indexed_and_its_original_retained(world, solo):
    kavita_id, manga = solo
    chapter_id, path = world.download(
        manga, "Chapter 2: Arise", scanlator="Grp", data=cbz(comicinfo=ci(Series="Solo Leveling (Asura)", Number="2"))
    )
    through(world)
    target = published(world, "Ch.2 - Arise.cbz")
    fields, pages = comicinfo_of(target)
    assert fields["Series"] == "Solo Leveling" and fields["LocalizedSeries"] == "Na Honjaman Level Up", (
        "Series is the record's name, LocalizedSeries Kavita's"
    )
    assert pages == ["001.png", "002.png", "003.png"], "converted (flat test pages: lossless), in reading order"
    assert world.rows("SELECT stage, status, waiting_on FROM chapter") == [("confirm", "waiting", "kavita_scan")], (
        "a scan is requested, not awaited"
    )
    assert world.kavita.pending == [world.cfg["kavita"]["library_id"]], "1 library scan was requested"
    assert path.exists(), "the download stays until Kavita has indexed the file"
    world.kavita.run_scans()
    world.run(export)
    assert world.rows("SELECT stage, status FROM chapter") == [("confirm", "done")]
    assert world.kavita.pending == [], "nothing published and nothing waits: no scan"
    assert world.kavita.series(kavita_id) is not None, "the series id is unchanged"
    assert target in [f["path"] for f in world.kavita.series_files(kavita_id)]
    assert not path.exists() and retained(world, path).exists(), "the original goes to the retention folder"
    assert world.suwayomi.chapter_rows[chapter_id]["isDownloaded"], "Suwayomi still shows it downloaded"
    assert world.rows(
        "SELECT state, origin FROM published_file WHERE library_path = %s", config.to_stored(world.cfg, target)
    ) == [("confirmed", "pipeline")]
    assert world.rows(
        "SELECT pages_in, median_page_width, strip_length, page_phashes FROM published_file WHERE library_path = %s",
        config.to_stored(world.cfg, target),
    ) == [(3, 16, 4500, "[]")], (
        "the download's own measurements are stored for later copies: 3 pages of 16 x 24, no pHash for a flat page"
    )


@budget(load="db", seconds=2)
def test_a_download_without_comicinfo_gets_a_minimal_one(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 3 - Shadows")
    through(world)
    fields, _ = comicinfo_of(published(world, "Ch.3 - Shadows.cbz"))
    assert {k: fields[k] for k in ("Series", "Number", "Title")} == {
        "Series": "Solo Leveling",
        "Number": "3",
        "Title": "Shadows",
    }
    world.kavita.run_scans()
    world.run(export)
    assert world.rows("SELECT status FROM chapter") == [("done",)], "it lands in the right series too"


@budget(load="db", seconds=2)
def test_a_retained_original_past_its_period_is_deleted_by_the_next_export(world, solo):
    _, manga = solo
    _, path = world.download(manga, "Chapter 2")
    through(world)
    world.kavita.run_scans()
    world.run(export)
    kept = retained(world, path)
    os.utime(kept, (time.time() - 8 * 86400,) * 2)
    world.run(export)
    assert not kept.exists(), "7 days are up"


@budget(load="db", seconds=2)
def test_a_second_download_of_a_handled_chapter_is_put_aside_not_published_twice(world, solo):
    _, manga = solo
    chapter_id, path = world.download(manga, "Chapter 2")
    data = path.read_bytes()
    through(world)
    world.kavita.run_scans()
    world.run(export)
    path.write_bytes(data)  # Suwayomi downloads the same chapter again
    through(world)
    assert world.rows("SELECT count(*) FROM published_file WHERE origin = 'pipeline'") == [(1,)]
    assert world.rows("SELECT stage, status FROM chapter ORDER BY id") == [("confirm", "done"), ("discover", "done")]
    assert not path.exists(), "the copy is put aside"


@budget(load="db", seconds=2)
def test_a_taken_target_name_needs_a_human_and_nothing_is_overwritten(world, solo):
    _, manga = solo
    published(world, "Ch.1.cbz").with_name("Ch.2.cbz").write_bytes(b"a file no record knows")
    world.download(manga, "Chapter 2", data=cbz(seed="another scan"))
    through(world)
    assert world.rows("SELECT stage, status FROM chapter") == [("publish", "needs_human")]
    assert published(world, "Ch.2.cbz").read_bytes() == b"a file no record knows"


@budget(load="db", seconds=2)
def test_a_target_of_another_size_with_the_staged_file_gone_needs_a_human_at_once(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world, intake, process)
    with closing(db.connect(world.cfg)) as conn:
        workflow.drain(world.context(conn, "export"), {"comicinfo": comicinfo_stage.run})
        row = conn.execute("SELECT * FROM chapter").fetchone()
    os.unlink(config.to_local(world.cfg, row["staged_path"]))
    config.to_local(world.cfg, row["library_path"]).write_bytes(b"another file")
    world.run(export)
    assert world.rows("SELECT stage, status, attempts FROM chapter") == [("publish", "needs_human", 0)]
    assert config.to_local(world.cfg, row["library_path"]).read_bytes() == b"another file", (
        "a library file is never overwritten"
    )


@budget(load="db", seconds=2)
def test_a_rename_done_before_a_crash_is_carried_on(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world, intake, process)
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "export")
        workflow.drain(ctx, {"comicinfo": comicinfo_stage.run})
        row = conn.execute("SELECT * FROM chapter").fetchone()
    os.replace(
        config.to_local(world.cfg, row["staged_path"]), config.to_local(world.cfg, row["library_path"])
    )  # the crash: renamed, outcome never recorded
    world.run(export)
    assert world.rows("SELECT stage, status FROM chapter") == [("confirm", "waiting")]


def scans(world):
    return [w for w in world.kavita.writes if w[0].startswith("scan")]


def an_hour_passes(world):
    """The last scan request and every wait so far are older than kavita.rescan_after_minutes."""
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET updated_at = '2000-01-01T00:00:00+00:00' WHERE status = 'waiting'")
        conn.execute("UPDATE meta SET value = '2000-01-01T00:00:01+00:00' WHERE key = %s", (export.REQUESTED_AT,))


@budget(load="db", seconds=4)
def test_a_run_publishing_into_3_series_requests_exactly_1_library_scan(world):
    for name in ("Alpha", "Beta", "Gamma"):
        world.kavita_series(name)
        world.download(world.manga(name), "Chapter 2")
    through(world)
    assert world.rows("SELECT COUNT(*) FROM chapter WHERE status = 'waiting'") == [(3,)], "all 3 published"
    assert scans(world) == [("scan_library", world.cfg["kavita"]["library_id"])], "1 library scan, no series scan"
    world.kavita.run_scans()
    world.run(export)
    assert world.rows("SELECT DISTINCT status FROM chapter") == [("done",)], "the 1 scan indexed all 3 series"


@budget(load="db", seconds=2)
def test_no_scan_is_requested_while_one_is_awaited_even_after_a_publish(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world)
    world.kavita.pending.clear()  # Kavita has not run the scan yet
    world.download(manga, "Chapter 3")
    through(world)
    assert len(scans(world)) == 1 and not world.kavita.pending, "back-to-back scans do not work"
    assert world.rows("SELECT status, attempts FROM chapter ORDER BY id") == [("waiting", 0), ("waiting", 0)]
    assert world.rows("SELECT scan_due FROM series") == [(1,)], "the new file waits for the next request"


@budget(load="db", seconds=2)
def test_a_publish_soon_after_an_indexed_scan_waits_for_the_next_request(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world)
    world.kavita.run_scans()
    world.download(manga, "Chapter 3")
    through(world)
    assert world.rows("SELECT status FROM chapter ORDER BY id") == [("done",), ("waiting",)]
    assert len(scans(world)) == 1, "the new chapter waits on kavita_scan, so the young request is still awaited"


@budget(load="db", seconds=2)
def test_chapters_not_indexed_an_hour_after_the_scan_get_1_library_scan_and_1_attempt(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world)
    world.kavita.pending.clear()  # Kavita never runs the scan
    world.run(export)
    assert not world.kavita.pending and world.rows("SELECT attempts FROM chapter") == [(0,)], "not yet due"
    an_hour_passes(world)
    world.download(manga, "Chapter 3")
    through(world)
    assert world.kavita.pending == [world.cfg["kavita"]["library_id"]], "1 new library scan"
    assert world.rows("SELECT status, attempts FROM chapter ORDER BY id") == [("waiting", 1), ("waiting", 0)], (
        "only the chapter already waiting at the earlier request is charged"
    )
    world.run(export)
    assert len(scans(world)) == 2, "the new request is awaited"


@budget(load="db", seconds=2)
def test_a_chapter_never_indexed_needs_a_human_at_the_attempt_limit(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world)
    for _ in range(world.cfg["workflow"]["attempt_limit"]):
        world.kavita.pending.clear()
        an_hour_passes(world)
        world.run(export)
    assert world.rows("SELECT status FROM chapter") == [("needs_human",)]


@budget(load="db", seconds=2)
def test_a_series_gone_from_kavita_needs_a_human_and_its_chapter_keeps_waiting(world, solo):
    kavita_id, manga = solo
    world.download(manga, "Chapter 2")
    through(world)
    del world.kavita.series_rows[kavita_id]
    world.run(export)
    world.run(export)
    assert world.rows("SELECT status FROM series") == [("needs_human",)]
    assert world.rows("SELECT stage, status FROM chapter") == [("confirm", "waiting")], "and on later runs too"


@budget(load="db", seconds=2)
def test_a_deleted_library_file_is_replaced_and_its_record_with_it(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world, intake, process)  # baseline: the bulk Ch.1.cbz is a recorded published file
    published(world, "Ch.1.cbz").unlink()  # a person removes a bad bulk file
    world.download(manga, "Chapter 1", data=cbz(seed="fresh scan"))
    through(world)
    world.kavita.run_scans()
    world.run(export)
    assert world.rows("SELECT status FROM chapter ORDER BY id") == [("done",), ("done",)]
    assert world.rows("SELECT origin, state FROM published_file WHERE library_path LIKE '%Ch.1.cbz'") == [
        ("pipeline", "confirmed")
    ], "the record of the gone file is replaced"


@budget(load="db", seconds=2)
@pytest.mark.parametrize("layout", [{}, {"layout": "scroll"}])
def test_a_record_without_a_known_layout_keeps_the_chapter_waiting(world, layout):
    kavita_id = world.kavita_series("Solo Leveling", record=False)
    record = bulk_record("Solo Leveling", kavita_id, [])
    record["series"]["layout"] = layout
    (world.library / "Solo Leveling" / ".optimizer.json").write_text(json.dumps(record), encoding="utf-8")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    through(world, intake, process, process)
    assert world.rows("SELECT stage, status, waiting_on, attempts FROM chapter") == [
        ("junk", "waiting", "bulk_conversion", 0)
    ], "a record the baseline cannot use is a record that does not parse yet"


@budget(load="db", seconds=2)
def test_a_record_naming_another_series_keeps_the_chapter_waiting(world):
    kavita_id = world.kavita_series("Solo Leveling", record=False)
    world.write_record("Other Name", kavita_id, "Solo Leveling", [])
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    through(world, intake, process, process)
    assert world.rows("SELECT status FROM series") == [("needs_human",)]
    assert world.rows("SELECT stage, status, waiting_on FROM chapter") == [("junk", "waiting", "bulk_conversion")]


@budget(load="db", seconds=2)
def test_state_reset_keeps_a_held_series_held(world):
    world.kavita_series("Solo Leveling!")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    through(world, intake)
    with closing(db.connect(world.cfg)) as conn:
        workflow.reset(conn)
    through(world)
    assert world.rows("SELECT status FROM series") == [("needs_human",)], "only series link settles a near hit"
    assert world.rows("SELECT stage, status FROM chapter") == [("discover", "ready")], "its chapter is held"


@budget(load="db", seconds=2)
def test_a_stale_size_is_not_indexed(world, solo):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world)
    world.kavita.run_scans()
    target = published(world, "Ch.2.cbz")
    with zipfile.ZipFile(target, "a") as z:
        z.writestr("extra.txt", b"grown after the scan")
    world.run(export)
    assert world.rows("SELECT status FROM chapter") == [("waiting",)], "Kavita's size must be the size on disk"


@budget(load="db", seconds=2)
def test_a_failed_scan_request_is_logged_left_due_and_charges_nothing(world, solo, monkeypatch):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world, intake, process)

    def refuse(library_id):
        raise http.HttpError(500, "http://kavita/api/Library/scan", b"")

    monkeypatch.setattr(world.kavita, "scan_library", refuse)
    world.run(export)
    assert world.rows("SELECT scan_due FROM series") == [(1,)], "the next run requests it"
    assert world.rows("SELECT value FROM meta WHERE key = %s", export.REQUESTED_AT) == [], "no request on record"
    assert world.rows("SELECT stage, status, attempts FROM chapter") == [("confirm", "waiting", 0)]
    monkeypatch.undo()
    world.run(export)
    assert world.kavita.pending == [world.cfg["kavita"]["library_id"]] and world.rows(
        "SELECT scan_due FROM series"
    ) == [(0,)]


@budget(load="db", seconds=2)
def test_a_failed_repeat_request_keeps_the_earlier_request_and_charges_nothing(world, solo, monkeypatch):
    _, manga = solo
    world.download(manga, "Chapter 2")
    through(world)
    world.kavita.pending.clear()
    an_hour_passes(world)

    def refuse(library_id):
        raise http.HttpError(500, "http://kavita/api/Library/scan", b"")

    monkeypatch.setattr(world.kavita, "scan_library", refuse)
    world.run(export)
    assert world.rows("SELECT value FROM meta WHERE key = %s", export.REQUESTED_AT) == [("2000-01-01T00:00:01+00:00",)]
    assert world.rows("SELECT status, attempts FROM chapter") == [("waiting", 0)], "a charge needs a request"
