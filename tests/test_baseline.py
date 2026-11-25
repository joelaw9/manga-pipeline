"""The junk stage's bulk_conversion wait and a series' baseline from its record.

SPEC "State", "Bad downloads", "Publishing".
"""

import errno
import io
import json
import os
import shutil
import time
import zipfile
from contextlib import closing
from types import SimpleNamespace

import pytest

from manga_pipeline import adoption, archive, baseline, config, db, export, intake, preflight, process, state, workflow
from manga_pipeline import comicinfo as comicinfo_xml
from manga_pipeline.clients import http
from manga_pipeline.metadata import naming, step
from manga_pipeline.stages import comicinfo as comicinfo_stage
from manga_pipeline.stages import validate

from tests import builders
from tests.builders import WinFileNotFoundError, add_chapter, db_row, one
from tests.fakes.suwayomi import page as fake_page
from tests.timing import budget
from tests.world import LIBRARY_ID, World, bulk_record, cbz, comicinfo


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


PARTIAL = {"schema": 4, "stages": ["structure"]}  # a record that does not parse (one left partial)


def at_junk(world, *, record=None, name="Chapter 2", data=None, **series):
    """Add a Kavita series with 2 bulk chapters and 1 download of it taken through intake.

    Its record whole, or record(kavita id): the record written in its place. Intake takes the baseline at validate.
    """
    bulk = (("Ch.1.cbz", "Grp_Chapter 1", "1"), ("Ch.1.5 - Extra.cbz", "Grp_Chapter 1.5 - Extra", "1.5"))
    if record is None:
        kavita_id = world.kavita_series("Solo Leveling", bulk=bulk, **series)
    else:
        kavita_id = world.series_without_record("Solo Leveling", bulk=bulk, **series)
        world.write_record("Solo Leveling", record(kavita_id))
    _, path = world.download(world.manga("Solo Leveling"), name, scanlator="Grp", data=data)
    world.run(intake)
    return kavita_id, path


@budget(load="db")
def test_a_record_that_does_not_parse_makes_the_series_need_a_human(world):
    kavita_id, _ = at_junk(world, record=lambda _kid: PARTIAL)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human", "nothing writes a record any more: no wait would end"
    assert reason == "record: 'kavita' missing or not a dict -> fix or remove the record, then retry --series", (
        "it names the flaw and the fix"
    )
    assert world.rows("SELECT stage FROM chapter") == [("discover",)], "held by the baseline pass: nothing goes on"
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)], "a series with a record is never adopted"
    world.write_record("Solo Leveling", bulk_record("Solo Leveling", kavita_id, []))
    with closing(db.connect(world.cfg)) as conn:
        workflow.retry_series(conn, one(conn, "SELECT id FROM series"))
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")], "the fixed record, retried, ends it"


@budget(load="db")
def test_the_baseline_records_the_series_published_and_handled_chapters_once(world):
    at_junk(world)
    world.run(process)
    world.run(process)
    published = world.rows(
        "SELECT library_path, state, origin, comicinfo_number, comicinfo_series FROM published_file ORDER BY 1"
    )
    folder = world.library / "Solo Leveling"
    assert published == [
        (config.to_stored(world.cfg, folder / "Ch.1.5 - Extra.cbz"), "confirmed", "bulk", "1.5", "Solo Leveling"),
        (config.to_stored(world.cfg, folder / "Ch.1.cbz"), "confirmed", "bulk", "1", "Solo Leveling"),
    ], "the record's chapters are the series' published files, taken once"
    handled = world.rows("SELECT identity, outcome FROM handled_chapter ORDER BY identity")
    assert handled == [
        ('["grp", null, "", "1", ""]', "published"),
        ('["grp", null, "", "1.5", "extra"]', "published"),
    ]
    assert world.rows("SELECT layout, origin FROM series_conversion") == [("pages", "record")]
    assert world.rows("SELECT comicinfo_series, baseline_at IS NOT NULL FROM series") == [("Solo Leveling", 1)]


@budget(load="db")
def test_a_download_with_the_pages_of_a_bulk_source_is_put_aside_once_the_baseline_exists(world):
    _, path = at_junk(world, data=cbz(seed="Grp_Chapter 1"))  # the same pages as bulk chapter 1's source
    world.run(process)
    # the daily baseline pass took the baseline before the download arrived: dropped at discover as handled
    assert world.rows("SELECT stage, status FROM chapter") == [("discover", "done")], "dropped, never published twice"
    assert not path.exists(), "put aside the retention way"


@budget(load="db")
def test_a_record_naming_another_series_records_nothing_and_needs_a_human(world):
    at_junk(world, record=lambda kid: bulk_record("Some Other Name", kid, []))
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human"
    assert "Some Other Name" in reason
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)], "nothing of the baseline is written"
    assert world.rows("SELECT stage FROM chapter") == [("discover",)], "its chapter stays held"


@budget(load="db")
def test_the_record_name_may_be_a_localized_kavita_name(world):
    at_junk(world, record=lambda kid: bulk_record("na honjaman level-up", kid, []), localized="Na Honjaman Level Up")
    assert world.rows("SELECT comicinfo_series FROM series") == [("na honjaman level-up",)], (
        "the record's name is written exactly, once it normalizes to a Kavita name"
    )


@budget(load="db")
def test_the_baseline_imports_the_junk_decisions_the_record_carries(world):
    world.kavita_series("Solo Leveling")
    path = world.library / "Solo Leveling" / ".optimizer.json"
    record = json.loads(path.read_bytes())
    record["series"]["decisions"]["rows"]["junk"] = [
        {
            "row": 1,
            "series": "Solo Leveling",
            "key": ["0123456789abcdef", 1.0],
            "decision": "approve",
            "review_id": "j1",
            "note": None,
            "added": "2026-11-07 10:00:00",
            "origin": "block",
            "chain": None,
            "credit": False,
        }
    ]
    path.write_bytes(json.dumps(record).encode())
    world.download(world.manga("Solo Leveling"), "Chapter 2", scanlator="Grp")
    world.run(intake)
    assert world.rows("SELECT series_key, kind, verdict, decider FROM junk_decision") == [
        ("Solo Leveling", "block", "junk", "import")
    ], "a decision the bulk converter recorded for the series is known to the pipeline from the baseline on"


@budget(load="db")
def test_the_baseline_stores_the_records_probed_calibration_groups_and_a_reset_keeps_them(world):
    at_junk(world)
    world.run(process)
    want = [("colour", 75, 1, "record")]
    got = world.rows("SELECT grp, q, fixed, origin FROM series_calibration")
    assert got == want, "the record's probed colour group is fixed; its unprobed gray group stays open (no row)"
    with closing(db.connect(world.cfg)) as conn:
        state.reset(world.cfg, conn)
    assert world.rows("SELECT grp, q, fixed, origin FROM series_calibration") == want, "calibration is knowledge"


@budget(load="pure")
def test_a_dropped_share_is_no_missing_record(monkeypatch):
    def dropped(_cfg, _series):
        error = WinFileNotFoundError(2, "network name gone")
        error.winerror = 64
        raise error

    monkeypatch.setattr(baseline, "_load", dropped)
    with pytest.raises(FileNotFoundError):
        baseline.record_problem(config.load(), db_row(folder="library/Solo Leveling"))


# --- adoption: a library series with no record takes its baseline from its own files (SPEC "State")


def archive_of(members):
    """Return CBZ bytes holding members ({name: bytes or text}) in that order."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


def chapter_file(*numbers, info=None):
    """Return a CBZ of pages numbered `numbers` (fake pages, other numbers = other content) and a ComicInfo.xml."""
    members = {f"{n:03}.png": fake_page(n, "adopt") for n in numbers}
    return archive_of(members if info is None else {**members, "ComicInfo.xml": info})


def library_series(world, files, name="Solo Leveling", **names):
    """Add a series Kavita holds with no record: files ({file name: CBZ bytes}) in <library>/<name>.

    names: its other Kavita names (localized, original). Returns the Kavita series id.
    """
    folder = world.library / name
    folder.mkdir(parents=True)
    for file, data in files.items():
        (folder / file).write_bytes(data)
    return world.kavita.add_series(name, name, LIBRARY_ID, **names)


def kavita_lists(world, kavita_id, path):
    """Have Kavita list path (a file under the library, there or not) as 1 more file of the series."""
    kavita_path = f"/kavita/library/{path.relative_to(world.library).as_posix()}"
    n = len(world.kavita.files) + 1
    world.kavita.files[kavita_path] = {
        "series": kavita_id, "id": 9000 + n, "chapter": 9500 + n, "bytes": 1, "pages": 1, "pagesRead": 0,
        "volume": None, "mtime": 0, "modified": "2026-01-01T00:00:00.0000000",
    }  # fmt: skip


def checks_down(world, monkeypatch):
    """Run intake with a metadata source down: the series row and its folder are stored, no check completes.

    The day's baseline pass is due again after it.
    """

    def down(_name):
        msg = "mangabaka"
        raise http.Unreachable(msg, "down")

    with monkeypatch.context() as m:
        m.setattr(world.sources.mangabaka, "search", down)
        world.run(intake)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("DELETE FROM meta WHERE key = %s", (step.BASELINE_PASS_AT,))


def series_row(conn):
    return db.one_row(conn, "SELECT * FROM series", ())


def checked(conn):
    """Record the series' metadata check as completed (data version 1)."""
    conn.execute("INSERT INTO series_check (series_id, data_version) VALUES ((SELECT id FROM series), 1)")


def adopted_rows(world):
    return world.rows(
        """SELECT source_name, identity, kind, comicinfo_number, comicinfo_volume, volume_source, comicinfo_title,
        pages_out, cover_state FROM published_file ORDER BY source_name"""
    )


def write_override(world, text):
    overrides = config.abs_path(world.cfg["paths"]["naming_overrides"])
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / "Solo Leveling.yaml").write_text(text, encoding="utf-8", newline="\n")


THREE = {f"Ch.{n}.cbz": chapter_file(n * 10 + 1, n * 10 + 2) for n in (1, 2, 3)}


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "info", "want"),
    [
        ("Vol.2 Ch.5 - The Fall", None, ("5", "2", "filename", "The Fall", None)),
        ("Chapter 7", comicinfo(Number="-1", Volume="-1"), ("7", None, None, None, None)),
        ("Oneshot", comicinfo(Number="3", Volume="4"), ("3", "4", "filename", "Oneshot", None)),
        ("Vol.1 Chapter 2", comicinfo(Volume="9"), ("2", "1", "filename", None, None)),
        ("Ch.3 - Fire", "<ComicInfo><Title>broken", ("3", None, None, "Fire", None)),
        (
            "Ch.3 - Fire",
            '<?xml version="1.0" encoding="x-unknown"?><ComicInfo><Title>Ice</Title></ComicInfo>',
            ("3", None, None, "Fire", None),
        ),
        (
            "Ch.3 - Fire",
            '<?xml version="1.0" encoding="shift_jis"?><ComicInfo><Title>Ice</Title></ComicInfo>',
            ("3", None, None, "Fire", None),
        ),
        ("Chapter 5", comicinfo(Title="Chapter 5"), ("5", None, None, None, None)),
        ("Ch.6", comicinfo(Year="2024", Month="3"), ("6", None, None, None, "2024-03-01")),
    ],
    ids=[
        "name-volume",
        "minus-one",
        "comicinfo-number",
        "name-volume-first",
        "malformed",
        "encoding",
        "multi-byte-encoding",
        "bare",
        "date",
    ],
)
def test_a_library_file_is_read_as_a_download_is(stem, info, want):
    found = adoption.plan(f"library/S/{stem}.cbz", ["001.png"], None if info is None else info.encode())
    assert isinstance(found, adoption.File), "a numbered file with pages is adopted"
    got = (found.number, found.volume, found.volume_source, found.title, found.date)
    assert got == want, "number from name then ComicInfo Number, its own volume, a download's title"
    assert found.source_name == stem, "its source name is its file stem"


@budget(load="pure")
def test_an_adopted_files_page_count_leaves_out_its_cover_and_its_cover_state_is_its_member_lists():
    members = ["000_cover.jpg", "001.jpg", "002.JPG", "Thumbs.db", "__MACOSX/._001.jpg", "ComicInfo.xml", "notes.txt"]
    found = adoption.plan("library/S/Vol.2 Ch.1.cbz", members, None)
    assert isinstance(found, adoption.File)
    assert (found.pages_out, found.cover_state) == (2, "volume:2"), "the archive's pages but 000_cover; its cover"
    plain = adoption.plan("library/S/Ch.1.cbz", ["001.jpg"], None)
    assert isinstance(plain, adoption.File)
    assert plain.cover_state == "none", "no 000_cover page: no cover"


@budget(load="pure")
@pytest.mark.parametrize(
    ("stem", "members", "info", "kind"),
    [
        ("Ch.4", ["000_cover.jpg"], None, "no pages"),
        ("Extras", ["001.png"], None, "no number"),
        ("Extras", ["001.png"], comicinfo(Number="-1"), "no number"),
        ("Ch.26.10", ["001.png"], None, "two readings"),
    ],
)
def test_a_library_file_without_pages_or_a_single_number_is_not_adopted(stem, members, info, kind):
    found = adoption.plan(f"library/S/{stem}.cbz", members, None if info is None else info.encode())
    assert isinstance(found, adoption.Problem), "a person must fix the file"
    assert found.kind == kind
    assert f"{stem}.cbz" in found.text, "the problem names the file"


@budget(load="pure")
def test_a_side_story_file_is_adopted_as_one():
    found = adoption.plan("library/S/Side Story 2.cbz", ["001.png"], None)
    assert isinstance(found, adoption.File)
    assert found.kind == "side"


@budget(load="db", seconds=3)
def test_a_series_with_no_record_is_adopted_from_its_files_and_its_chapter_goes_on(world):
    files = {
        "Vol.1 Ch.1.cbz": archive_of({"000_cover.png": fake_page(0, "c"), "001.png": fake_page(1, "a")}),
        "Vol.1 Ch.2.cbz": chapter_file(2, 3, info=comicinfo(Title="Into the Gate", Number="2")),
    }
    library_series(world, files)
    world.download(world.manga("Solo Leveling"), "Chapter 3", scanlator="Grp")
    world.run(intake)
    assert adopted_rows(world) == [
        ("Vol.1 Ch.1", '["", null, "", "1", ""]', "main", "1", "1", "filename", None, 1, "volume:1"),
        ("Vol.1 Ch.2", '["", null, "", "2", ""]', "main", "2", "1", "filename", "Into the Gate", 2, "none"),
    ], "each file read as a download is"
    rows = world.rows(
        """SELECT DISTINCT origin, state, comicinfo_series, pages_in, bpp, median_page_width, strip_length
        FROM published_file"""
    )
    assert rows == [("library", "confirmed", "Solo Leveling", None, None, None, None)], (
        "adopted, confirmed, Kavita's name; measured from the file when a copy is checked"
    )
    assert world.rows("SELECT layout, origin FROM series_conversion") == [("strip", "computed")], (
        "measured over its files (1-colour fake pages join smoothly)"
    )
    series = world.rows("SELECT comicinfo_series, naming_basis, baseline_at IS NOT NULL, status FROM series")
    assert series == [("Solo Leveling", "filename", 1, "ok")], "its name, its basis from its filename volumes"
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")], "its new chapter goes on"
    assert not list(config.abs_path(world.cfg["paths"]["scratch"]).glob("adopt-*")), "the layout sample is removed"


@budget(load="db", seconds=4)
def test_a_record_written_after_adoption_is_ignored(world):
    kavita_id = library_series(world, THREE)
    world.run(intake)
    world.write_record("Solo Leveling", bulk_record("Solo Leveling", kavita_id, []))
    world.download(world.manga("Solo Leveling"), "Chapter 4", scanlator="Grp")
    world.run(intake)
    assert world.rows("SELECT DISTINCT origin FROM published_file") == [("library",)], "never baselined again"
    assert world.rows("SELECT origin FROM series_conversion") == [("computed",)]


@budget(load="db", seconds=3)
@pytest.mark.parametrize("caller", ["validate", "bulk_conversion_over", "comicinfo"])
@pytest.mark.parametrize("wait", ["check", "discover"])
def test_adoption_waits_for_a_completed_check_and_no_chapter_at_discover_reading_no_file(
    world, monkeypatch, caller, wait
):
    library_series(world, THREE)
    checks_down(world, monkeypatch)

    def no_read(*_):
        raise AssertionError

    monkeypatch.setattr(adoption, "prepare", no_read)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("UPDATE series SET comicinfo_series = name")  # as an older version left it
        series_id = series_row(conn)["id"]
        if wait == "discover":
            checked(conn)
            add_chapter(conn, series_id, '["grp", null, "", "9", ""]')
        stage = "junk" if caller == "bulk_conversion_over" else caller
        path = config.to_stored(world.cfg, world.library / "x.cbz")
        chapter_id = add_chapter(conn, series_id, '["grp", null, "", "4", ""]', stage=stage, download_path=path)
        ctx = world.context(conn, "intake")
        row = builders.chapter(conn, chapter_id)
        if caller == "bulk_conversion_over":
            assert not validate.bulk_conversion_over(ctx, row), "the wait goes on"
        else:
            outcome = (validate.run if caller == "validate" else comicinfo_stage.run)(ctx, row)
            assert isinstance(outcome, workflow.Waiting), "the chapter waits"
            assert outcome.on == "bulk_conversion"
            assert ("metadata check" if wait == "check" else "discover") in str(outcome.data["why"]), "it says why"
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)]
    assert world.rows("SELECT baseline_at FROM series") == [(None,)]
    assert world.rows("SELECT status FROM series") == [("ok",)], "a wait for adoption is no needs_human"


BAD_FILES = {
    "unreadable": ("Ch.4.cbz", b"not a zip"),
    "no pages": ("Ch.4.cbz", archive_of({"ComicInfo.xml": comicinfo(Number="4")})),
    "no number": ("Extras.cbz", chapter_file(7)),
    "two readings": ("Ch.26.10.cbz", chapter_file(7)),
    "same chapter": ("Grp_Ch.2.cbz", chapter_file(7)),
    "undecodable": ("Ch.4.cbz", archive_of({"001.png": b"not an image", "002.png": b"nor this"})),
}


@budget(load="db", seconds=3)
@pytest.mark.parametrize(
    "case",
    [*BAD_FILES, "missing", "not cbz", "override", "outside folder", "no files"],
)
def test_a_series_whose_files_cannot_be_adopted_needs_a_human_naming_them(world, monkeypatch, case):
    files = dict(THREE)
    if case in BAD_FILES:
        name, data = BAD_FILES[case]
        files[name] = data
    if case == "undecodable":
        files = {name: data}
    if case == "no files":
        files = {}
    kavita_id = library_series(world, files)
    checks_down(world, monkeypatch)  # the row and its folder are stored
    folder = world.library / "Solo Leveling"
    named = {
        "missing": "Ch.4.cbz",
        "not cbz": "Ch.4.cbr",
        "outside folder": "Elsewhere/Ch.4.cbz",
        "override": "override file",
        "no files": "lists no file",
        "unreadable": "Ch.4.cbz",
    }
    if case in ("unreadable", "missing", "not cbz"):  # a file Kavita lists as it is (its scan skips these here)
        kavita_lists(world, kavita_id, folder / named[case])
    if case == "not cbz":
        (folder / "Ch.4.cbr").write_bytes(chapter_file(7))
    if case == "outside folder":
        (world.library / "Elsewhere").mkdir()
        (world.library / "Elsewhere" / "Ch.4.cbz").write_bytes(chapter_file(7))
        kavita_lists(world, kavita_id, world.library / "Elsewhere" / "Ch.4.cbz")
    if case == "override":
        write_override(world, "layout: scroll\n")
    if case == "no files":
        with closing(db.connect(world.cfg)) as conn:
            conn.execute("UPDATE series SET folder = %s", (config.to_stored(world.cfg, folder),))
    world.run(intake)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human", "a person fixes the files, rescans the library in Kavita, then retries"
    assert (named[case] if case in named else BAD_FILES[case][0]) in reason, "the reason names what is wrong"
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)], "nothing is recorded"
    assert world.rows("SELECT baseline_at FROM series") == [(None,)]


@budget(load="db", seconds=3)
def test_a_chapter_restarting_per_volume_is_not_2_files_of_1_chapter(world):
    library_series(world, {"Vol.1 Ch.1.cbz": chapter_file(1), "Vol.2 Ch.1.cbz": chapter_file(2)})
    world.run(intake)
    assert world.rows("SELECT count(*) FROM published_file WHERE origin = 'library'") == [(2,)], (
        "1 key, 2 volumes from the files: 2 chapters"
    )


@budget(load="db", seconds=3)
@pytest.mark.parametrize("mounted", [True, False], ids=["share mounted", "share not mounted"])
def test_a_missing_file_is_the_series_problem_while_the_share_is_mounted_else_the_shares(world, monkeypatch, mounted):
    kavita_id = library_series(world, THREE)
    checks_down(world, monkeypatch)
    folder = world.library / "Solo Leveling"
    kavita_lists(world, kavita_id, folder / "Ch.4.cbz")
    shutil.rmtree(folder)  # its folder gone too
    if not mounted:  # the share not mounted on Linux: the owner file on it is missing as well
        preflight.owner_file(world.cfg).unlink()
    with closing(db.connect(world.cfg)) as conn:
        checked(conn)
        ctx = world.context(conn, "intake")
        if mounted:
            assert baseline.take(ctx, series_row(conn)), "held"
        else:
            with pytest.raises(workflow.Unreachable):
                baseline.take(ctx, series_row(conn))
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    if mounted:
        assert status == "needs_human", "a file gone while the share is there is the series' to fix"
        assert "Ch.4.cbz: Kavita lists it, it is not on disk" in reason
    else:
        assert status == "ok", "not held: the share is the run's problem"


@budget(load="db", seconds=3)
def test_a_file_that_cannot_be_opened_for_another_reason_holds_the_series(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)

    def refused(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(adoption, "zipfile", SimpleNamespace(ZipFile=refused))  # adoption's reads only
    with closing(db.connect(world.cfg)) as conn:
        checked(conn)
        assert baseline.take(world.context(conn, "intake"), series_row(conn)), "held"
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human"
    assert "Ch.1.cbz: cannot be opened" in reason, "the file is named"


@budget(load="db", seconds=3)
def test_a_share_error_while_reading_records_nothing(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)

    def share_down(*_args, **_kwargs):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(adoption, "zipfile", SimpleNamespace(ZipFile=share_down))  # adoption's reads only
    with closing(db.connect(world.cfg)) as conn:
        checked(conn)
        with pytest.raises(workflow.Unreachable):
            baseline.take(world.context(conn, "intake"), series_row(conn))
    assert world.rows("SELECT status, baseline_at FROM series") == [("ok", None)]
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)]


@budget(load="db", seconds=3)
@pytest.mark.parametrize("many", [True, False], ids=["4 files", "1 file"])
def test_the_layout_is_measured_over_the_content_pages_of_the_first_middle_and_last_files(world, monkeypatch, many):
    last = {"Ch.9.cbz": archive_of({"000_cover.png": fake_page(0, "c"), "001.png": fake_page(9, "x")})}
    library_series(world, {**THREE, **last} if many else last)
    sampled = []

    def classify(chapters, *_args):
        sampled.extend([p.name for p in pages] for pages in chapters)
        return "strip", "measured", []

    monkeypatch.setattr(adoption.strip, "classify_layout", classify)
    world.run(intake)
    want = [["0000.png", "0001.png"], ["0000.png", "0001.png"], ["0001.png"]] if many else [["0001.png"]]
    assert sampled == want, "Ch.1, Ch.3 and Ch.9 (first, middle, last by number), each once, cover pages left out"
    assert world.rows("SELECT layout, origin FROM series_conversion") == [("strip", "computed")]


@budget(load="db", seconds=3)
def test_the_override_files_layout_is_taken_and_nothing_is_decoded(world):
    library_series(world, {"Ch.1.cbz": archive_of({"001.png": b"not an image"})})
    write_override(world, "layout: strip\n")
    world.run(intake)
    assert world.rows("SELECT layout, origin FROM series_conversion") == [("strip", "override")]


@budget(load="db", seconds=3)
def test_a_layout_and_a_basis_already_stored_are_kept(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)
    with closing(db.connect(world.cfg)) as conn:
        conn.execute(
            """INSERT INTO series_conversion (series_id, layout, origin)
            VALUES ((SELECT id FROM series), 'strip', 'computed')"""
        )
        conn.execute("UPDATE series SET naming_basis = 'mangadex'")
    monkeypatch.setattr(adoption.strip, "classify_layout", None)  # never measured
    monkeypatch.setattr(adoption.chapter_names, "inputs", None)  # never chosen: no naming input read
    world.run(intake)
    assert world.rows("SELECT layout FROM series_conversion") == [("strip",)]
    assert world.rows("SELECT naming_basis, baseline_at IS NOT NULL FROM series") == [("mangadex", 1)]


@budget(load="db", seconds=3)
def test_the_basis_keeps_the_stored_pipeline_files_in_view_and_a_held_path_is_not_adopted_again(world, monkeypatch):
    library_series(world, {**THREE, "Side Story 1.cbz": chapter_file(5)})
    checks_down(world, monkeypatch)
    side = config.to_stored(world.cfg, world.library / "Solo Leveling" / "Side Story 1.cbz")
    with closing(db.connect(world.cfg)) as conn:  # published by the pipeline before a rollback
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, state, origin, kind, comicinfo_number)
            VALUES (%s, (SELECT id FROM series), '["", null, "side story", "1", ""]', 'confirmed', 'pipeline',
            'side', '1')""",
            (side,),
        )
    world.run(intake)
    assert world.rows("SELECT origin, count(*) FROM published_file GROUP BY 1 ORDER BY 1") == [
        ("library", 3),
        ("pipeline", 1),
    ], "the held path is skipped"
    assert world.rows("SELECT naming_basis FROM series") == [(naming.SIDE_DEFAULT,)], "its side story counts for it"


@budget(load="db", seconds=3)
@pytest.mark.parametrize("where", ["reads", "transaction"])
def test_a_crash_during_adoption_records_nothing_and_the_next_try_adopts(world, monkeypatch, where):
    library_series(world, THREE)
    checks_down(world, monkeypatch)

    def crash(*_args, **_kwargs):
        raise RuntimeError

    def half_written(conn, *_args):
        conn.execute("UPDATE series SET folder = 'library/x'")
        crash()

    with closing(db.connect(world.cfg)) as conn:
        checked(conn)
        with monkeypatch.context() as m:
            if where == "reads":
                m.setattr(adoption.chapter_names, "inputs", crash)
            else:
                m.setattr(baseline, "_write_adopted", half_written)
            with pytest.raises(RuntimeError):
                baseline.take(world.context(conn, "intake"), series_row(conn))
        assert world.rows("SELECT count(*) FROM published_file") == [(0,)]
        assert world.rows("SELECT baseline_at, folder FROM series") == [(None, "library/Solo Leveling")], "rolled back"
        assert baseline.take(world.context(conn, "intake"), series_row(conn)) is None
    assert world.rows("SELECT count(*) FROM published_file") == [(3,)]


@budget(load="db", seconds=3)
def test_a_crashed_adoptions_sample_folder_is_removed_once_a_day_old(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)
    scratch = config.abs_path(world.cfg["paths"]["scratch"])
    old, fresh = scratch / "adopt-7-111", scratch / "adopt-7-222"
    for folder in (old, fresh):
        (folder / "0").mkdir(parents=True)
    day_old = time.time() - 2 * 86400
    os.utime(old, (day_old, day_old))
    world.run(intake)
    assert (old.exists(), fresh.exists()) == (False, True), "a day-old one is a crashed run's; a fresh one may be live"


@budget(load="db", seconds=3)
def test_2_runs_adopting_at_once_record_once(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)
    with closing(db.connect(world.cfg)) as conn:
        stale = series_row(conn)
    world.run(intake)  # the other run
    with closing(db.connect(world.cfg)) as conn:  # its stamp told apart from a second write in the same second
        conn.execute("UPDATE series SET baseline_at = '2026-01-01T00:00:00+00:00'")
    first = world.rows("SELECT baseline_at FROM series")
    with closing(db.connect(world.cfg)) as conn:
        assert baseline.take(world.context(conn, "intake"), stale) is None, "the series has its baseline"
    assert world.rows("SELECT count(*) FROM published_file") == [(3,)]
    assert world.rows("SELECT baseline_at FROM series") == first, "the second run writes nothing"


@budget(load="db", seconds=3)
def test_a_series_with_a_baseline_and_old_bulk_rows_reads_no_library_file(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)
    monkeypatch.setattr(adoption, "prepare", None)  # never adopted
    with closing(db.connect(world.cfg)) as conn:
        checked(conn)
        conn.execute("UPDATE series SET baseline_at = '2026-01-01T00:00:00+00:00'")
        conn.execute(
            """INSERT INTO published_file (library_path, series_id, identity, state, origin)
            VALUES ('library/Solo Leveling/Ch.1.cbz', (SELECT id FROM series), '["", null, "", "1", ""]',
            'confirmed', 'bulk')"""
        )
        why = baseline.take(world.context(conn, "intake"), series_row(conn))
    assert why is not None, "its record is what it waits for"
    assert ".optimizer.json" in why


@budget(load="db", seconds=3)
def test_a_held_series_is_not_read_again_for_its_other_waiting_chapters(world, monkeypatch):
    library_series(world, {**THREE, "Extras.cbz": chapter_file(7)})
    manga = world.manga("Solo Leveling")
    world.download(manga, "Chapter 4", scanlator="Grp")
    world.download(manga, "Chapter 5", scanlator="Grp")
    checks_down(world, monkeypatch)  # both wait at validate for the check
    with closing(db.connect(world.cfg)) as conn:  # the day's pass already ran: the waits are what adopts
        conn.execute("INSERT INTO meta (key, value) VALUES (%s, %s)", (step.BASELINE_PASS_AT, workflow.now(conn)))
    reads = []
    prepare = adoption.prepare

    def counted(ctx, series, folder):
        reads.append(series["id"])
        return prepare(ctx, series, folder)

    monkeypatch.setattr(adoption, "prepare", counted)
    world.run(intake)
    assert len(reads) == 1, "the first waiting chapter holds the series; the second reads nothing"
    assert world.rows("SELECT status FROM series") == [("needs_human",)]


@budget(load="db", seconds=3)
def test_a_record_series_with_no_stored_folder_takes_its_record_and_is_never_adopted(world, monkeypatch):
    kavita_id = world.series_without_record("Solo Leveling")
    world.write_record("Solo Leveling", PARTIAL)  # no baseline taken yet
    checks_down(world, monkeypatch)
    world.write_record("Solo Leveling", bulk_record("Solo Leveling", kavita_id, []))
    with closing(db.connect(world.cfg)) as conn:  # as identify left a held series that a person retried
        conn.execute("UPDATE series SET folder = NULL")
        checked(conn)
        assert baseline.take(world.context(conn, "intake"), series_row(conn)) is None, "the baseline is taken"
    assert world.rows("SELECT origin FROM series_conversion") == [("record",)], "from its record, not adopted"
    assert world.rows("SELECT count(*) FROM published_file WHERE origin = 'library'") == [(0,)]
    assert world.rows("SELECT folder FROM series") == [("library/Solo Leveling",)], "its Kavita folder is stored"


@budget(load="db", seconds=3)
def test_2_side_stories_of_1_number_in_2_volumes_are_1_chapter(world):
    sides = {"Vol.1 Side Story 1.cbz": chapter_file(7), "Vol.2 Side Story 1.cbz": chapter_file(8)}
    library_series(world, {**THREE, **sides})
    world.run(intake)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human", "a side story has no volume of its own: its key alone tells it apart"
    assert "are 1 chapter" in reason


@budget(load="db", seconds=3)
def test_a_kavita_error_reply_leaves_the_chapter_waiting_with_no_attempt_charged(world, monkeypatch):
    library_series(world, THREE)
    world.download(world.manga("Solo Leveling"), "Chapter 4", scanlator="Grp")

    def error_reply(_series_id):
        raise http.HttpError(500, "http://kavita.invalid/api/Series/volumes", b"oops")

    monkeypatch.setattr(world.kavita, "series_files", error_reply)
    world.run(intake)
    ((stage, status, waiting_on, attempts, data),) = world.rows(
        "SELECT stage, status, waiting_on, attempts, wait_data FROM chapter"
    )
    assert (stage, status, waiting_on, attempts) == ("validate", "waiting", "bulk_conversion", 0), "tried again"
    assert "Kavita replied with an error" in json.loads(data)["why"]
    assert world.rows("SELECT status FROM series") == [("ok",)]


@budget(load="db", seconds=3)
def test_a_series_another_run_adopted_meanwhile_is_not_held(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)

    def adopted_meanwhile(_ctx, _series, _folder):
        with closing(db.connect(world.cfg)) as other:
            other.execute("UPDATE series SET baseline_at = '2026-01-01T00:00:00+00:00'")
        return [adoption.Problem("missing", "a file read while another run adopted the series")]

    monkeypatch.setattr(adoption, "prepare", adopted_meanwhile)
    with closing(db.connect(world.cfg)) as conn:
        checked(conn)
        assert baseline.take(world.context(conn, "intake"), series_row(conn)) is None, "the series has its baseline"
    assert world.rows("SELECT status FROM series") == [("ok",)], "not held"


@budget(load="db", seconds=3)
def test_a_share_error_in_the_baseline_pass_ends_the_intake_run(world, monkeypatch):
    library_series(world, THREE)
    admitted = []

    def share_down(*_args, **_kwargs):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(adoption, "zipfile", SimpleNamespace(ZipFile=share_down))  # adoption's reads only
    monkeypatch.setattr(step.admit, "run", lambda *args: admitted.append(args))
    world.run(intake)
    assert admitted == [], "the run ended at the share error: nothing after the pass ran"
    assert world.rows("SELECT status, baseline_at FROM series") == [("ok", None)], "not held: the share is"


@budget(load="db", seconds=3)
def test_a_series_kavita_no_longer_lists_is_not_adopted_by_the_pass(world, monkeypatch):
    kavita_id = library_series(world, THREE)
    checks_down(world, monkeypatch)
    with closing(db.connect(world.cfg)) as conn:
        checked(conn)
    world.kavita.series_rows.pop(kavita_id)  # its files still answer, as a stale volumes reply might
    world.run(intake)
    assert world.rows("SELECT status, baseline_at FROM series") == [("ok", None)], "only series Kavita holds"
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)]


@budget(load="db", seconds=3)
def test_a_file_gone_before_its_layout_sample_holds_the_series(world, monkeypatch):
    library_series(world, THREE)
    folder = world.library / "Solo Leveling"
    plan = adoption.plan

    def then_gone(path, members, xml):  # the last file planned: Ch.1 goes before the layout sample
        found = plan(path, members, xml)
        if path.endswith("Ch.3.cbz"):
            (folder / "Ch.1.cbz").unlink()
        return found

    monkeypatch.setattr(adoption, "plan", then_gone)
    world.run(intake)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human", "the series' to fix, not a failed stage"
    assert "Ch.1.cbz: cannot be opened" in reason


@budget(load="db", seconds=3)
def test_a_share_unmounted_before_the_layout_sample_is_the_shares_problem(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)
    plan = adoption.plan

    def then_unmounted(path, members, xml):  # after the last file planned, before the layout sample
        found = plan(path, members, xml)
        if path.endswith("Ch.3.cbz"):
            shutil.rmtree(world.library / "Solo Leveling")
            preflight.owner_file(world.cfg).unlink()
        return found

    monkeypatch.setattr(adoption, "plan", then_unmounted)
    with closing(db.connect(world.cfg)) as conn:
        checked(conn)
        with pytest.raises(workflow.Unreachable):
            baseline.take(world.context(conn, "intake"), series_row(conn))
    assert world.rows("SELECT status, baseline_at FROM series") == [("ok", None)], "not held: the share is"
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)]


@budget(load="db", seconds=3)
def test_an_override_file_the_volume_basis_cannot_read_holds_the_series(world, monkeypatch):
    library_series(world, THREE)
    checks_down(world, monkeypatch)
    with closing(db.connect(world.cfg)) as conn:  # its layout known: only the basis reads the override file's volumes
        conn.execute(
            """INSERT INTO series_conversion (series_id, layout, origin)
            VALUES ((SELECT id FROM series), 'pages', 'computed')"""
        )
    write_override(world, "volumes: 7\n")
    world.run(intake)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human", "a person fixes the override file"
    assert "override file of Solo Leveling" in reason
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)]


@budget(load="pure")
def test_a_hold_names_at_most_10_problems_and_counts_the_rest():
    problems = [adoption.Problem("missing", f"Ch.{n}.cbz is gone") for n in range(1, 13)]
    reason = adoption.reason(problems)
    assert "Ch.10.cbz is gone" in reason, "the first 10 named"
    assert "Ch.11.cbz" not in reason, "the rest counted, not named"
    assert "and 2 more" in reason


# --- the series' ComicInfo name: the Series its files carry (SPEC "State")

KAVITA = db_row(name="Solo Leveling", localized_name=None, original_name="Na Honjaman Level Up")


@budget(load="pure")
@pytest.mark.parametrize(
    ("values", "want"),
    [
        (["Solo Leveling.", "Solo Leveling.", "solo leveling"], "Solo Leveling."),
        (["Na Honjaman Level-Up", "Na Honjaman Level Up"], "Na Honjaman Level Up"),
        (["Solo  Leveling", "Solo-Leveling"], "Solo  Leveling"),
        ([None, None], "Solo Leveling"),
    ],
    ids=["most-common", "tie-original-name", "tie-first-by-sort", "none-kavitas-name"],
)
def test_the_series_name_is_the_series_value_most_of_its_files_carry(values, want):
    assert adoption.series_name(values, KAVITA) == want, (
        "most common exact value; a tie: Kavita's original name, else the first by sort; none: Kavita's name"
    )


@budget(load="pure")
@pytest.mark.parametrize(
    ("values", "kind", "named"),
    [
        (["Solo Leveling", "Solo Leveling", "Other Thing"], "series names", "'Other Thing'"),
        (["Other Thing", None], "series name", "'Other Thing' is none of Kavita's names"),
    ],
    ids=["differing", "not-kavitas"],
)
def test_differing_series_values_or_a_name_none_of_kavitas_is_a_problem(values, kind, named):
    found = adoption.series_name(values, KAVITA)
    assert isinstance(found, adoption.Problem), "a file carrying it would leave the series"
    assert found.kind == kind
    assert named in found.text, "the problem names the values"


def named_files(*series):
    """Return 1 library file per value of series: Ch.<n>.cbz, its ComicInfo.xml carrying that Series (None: none)."""
    return {
        f"Ch.{n}.cbz": chapter_file(n * 10 + 1, info=comicinfo(Number=str(n), **({"Series": s} if s else {})))
        for n, s in enumerate(series, 1)
    }


@budget(load="db", seconds=3)
def test_an_adopted_series_takes_the_series_name_its_files_carry_and_each_row_its_files_own(world):
    library_series(world, named_files("Solo Leveling.", "Solo Leveling.", "solo leveling", None))
    world.run(intake)
    assert world.rows("SELECT comicinfo_series FROM series") == [("Solo Leveling.",)], "its files' name, not Kavita's"
    assert world.rows("SELECT source_name, comicinfo_series FROM published_file ORDER BY 1") == [
        ("Ch.1", "Solo Leveling."),
        ("Ch.2", "Solo Leveling."),
        ("Ch.3", "solo leveling"),
        ("Ch.4", "Solo Leveling."),
    ], "each row its file's own Series, else the series' name"


@budget(load="db", seconds=3)
@pytest.mark.parametrize(
    ("series", "named"),
    [(("Solo Leveling", "Other Thing"), "'Other Thing'"), (("Other Thing", "Other Thing"), "none of Kavita's names")],
    ids=["differing", "not-kavitas"],
)
def test_files_whose_series_would_leave_the_series_hold_it_for_a_human(world, series, named):
    library_series(world, named_files(*series))
    world.run(intake)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human", "a person fixes the files, rescans the library in Kavita, then retries"
    assert named in reason, "the reason names the values"
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)], "nothing is recorded"


@budget(load="db", seconds=10)
def test_a_pipeline_chapter_of_an_adopted_series_is_written_with_its_files_name(world):
    library_series(world, named_files("Na Honjaman Level Up", "Na Honjaman Level Up"), original="Na Honjaman Level Up")
    world.download(world.manga("Solo Leveling"), "Chapter 3", scanlator="Grp")
    world.run(intake)
    world.run(process)
    world.run(export)
    ((path,),) = world.rows("SELECT library_path FROM published_file WHERE origin = 'pipeline'")
    info = archive.read_comicinfo(config.to_local(world.cfg, path))
    assert info is not None
    assert comicinfo_xml.read_all(info)["Series"] == "Na Honjaman Level Up", "the files' name: Kavita keeps 1 series"
