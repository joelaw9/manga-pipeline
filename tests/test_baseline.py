"""The junk stage's bulk_conversion wait and a series' baseline from its record (SPEC "State", "Bad downloads",
"Publishing")."""

import json

import pytest

from manga_pipeline import intake, process

from tests.timing import budget
from tests.world import World, cbz


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def at_junk(world, record=True, name="Chapter 2", data=None, **series):
    """A Kavita series with 2 bulk chapters (record or not) and 1 download of it taken through intake."""
    bulk = (("Ch.1.cbz", "Grp_Chapter 1", "1"), ("Ch.1.5 - Extra.cbz", "Grp_Chapter 1.5 - Extra", "1.5"))
    kavita_id = world.kavita_series("Solo Leveling", record=record, bulk=bulk, **series)
    _, path = world.download(world.manga("Solo Leveling"), name, scanlator="Grp", data=data)
    world.run(intake)
    return kavita_id, path


@budget(load="db")
def test_a_series_without_a_record_holds_its_chapters_until_the_record_parses(world):
    kavita_id, _ = at_junk(world, record=False)
    world.run(process)
    ((status, waiting_on, data),) = world.rows("SELECT status, waiting_on, wait_data FROM chapter")
    assert (status, waiting_on) == ("waiting", "bulk_conversion"), "nothing is published into a series blind"
    assert ".optimizer.json" in json.loads(data)["why"]
    world.run(process)
    assert world.rows("SELECT status FROM chapter") == [("waiting",)], "the wait holds while there is no record"
    world.write_record("Solo Leveling", kavita_id, "Solo Leveling", [])
    world.run(process)
    assert world.rows("SELECT stage, status FROM chapter") == [("comicinfo", "ready")], "the record ends the wait"


@budget(load="db")
def test_a_partial_record_is_no_record(world):
    kavita_id, _ = at_junk(world, record=False)
    folder = world.library / "Solo Leveling"
    (folder / ".optimizer.json").write_text(json.dumps({"schema": 4, "stages": ["structure"]}), encoding="utf-8")
    world.run(process)
    assert world.rows("SELECT waiting_on FROM chapter") == [("bulk_conversion",)]


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
        (str(folder / "Ch.1.5 - Extra.cbz"), "confirmed", "bulk", "1.5", "Solo Leveling"),
        (str(folder / "Ch.1.cbz"), "confirmed", "bulk", "1", "Solo Leveling"),
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
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "done")], "dropped, never published twice"
    assert not path.exists(), "put aside the retention way"


@budget(load="db")
def test_a_record_naming_another_series_records_nothing_and_needs_a_human(world):
    kavita_id, _ = at_junk(world, record=False)
    world.write_record("Some Other Name", kavita_id, "Solo Leveling", [])
    world.run(process)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human" and "Some Other Name" in reason
    assert world.rows("SELECT count(*) FROM published_file") == [(0,)], "nothing of the baseline is written"
    assert world.rows("SELECT waiting_on FROM chapter") == [("bulk_conversion",)], "its chapter stays held"


@budget(load="db")
def test_the_record_name_may_be_a_localized_kavita_name(world):
    kavita_id, _ = at_junk(world, record=False, localized="Na Honjaman Level Up")
    world.write_record("na honjaman level-up", kavita_id, "Solo Leveling", [])
    world.run(process)
    assert world.rows("SELECT comicinfo_series FROM series") == [("na honjaman level-up",)], (
        "the record's name is written exactly, once it normalizes to a Kavita name"
    )


@budget(load="db")
def test_the_baseline_stores_the_records_probed_calibration_groups_and_a_reset_keeps_them(world):
    from manga_pipeline import state

    at_junk(world)
    world.run(process)
    want = [("colour", 75, 1, "record")]
    got = world.rows("SELECT grp, q, fixed, origin FROM series_calibration")
    assert got == want, "the record's probed colour group is fixed; its unprobed gray group stays open (no row)"
    state.reset(world.cfg)
    assert world.rows("SELECT grp, q, fixed, origin FROM series_calibration") == want, "calibration is knowledge"
