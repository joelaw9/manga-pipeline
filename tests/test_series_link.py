"""`series link` and the status lines Phase 3 adds (SPEC "Series identification", "CLI")."""

from contextlib import closing

import pytest

from manga_pipeline import db, discovery, identify, intake, status, workflow

from tests.builders import add_series
from tests.timing import budget
from tests.world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def link(world, manga_id, kavita_id):
    with closing(db.connect(world.cfg)) as conn:
        return identify.link(world.cfg, conn, world.suwayomi, world.kavita, manga_id, kavita_id)


def scan(world):
    with closing(db.connect(world.cfg)) as conn:
        discovery.scan(world.context(conn, "intake"))


@budget(load="db")
def test_link_fixes_a_near_hit_and_retry_releases_its_chapters(world):
    kavita_id = world.kavita_series("Kaiju No. 8!")
    manga = world.manga("Kaiju No. 8")
    world.download(manga, "Chapter 2")
    scan(world)
    ((series_id, status_),) = world.rows("SELECT id, status FROM series")
    assert status_ == "needs_human"
    assert link(world, manga, kavita_id) == series_id, "the manga's held row is the one linked"
    got = world.rows("SELECT name, kavita_series_id, kavita_library_id, folder, origin, status FROM series")
    assert got == [("Kaiju No. 8!", kavita_id, 4, str(world.library / "Kaiju No. 8!"), "kavita", "needs_human")], (
        "link stores the fix; retry --series clears needs_human"
    )
    with closing(db.connect(world.cfg)) as conn:
        workflow.retry_series(conn, series_id)
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")]


@budget(load="db")
def test_link_onto_a_series_another_source_already_has_reuses_its_row(world):
    kavita_id = world.kavita_series("Kaiju No. 8!", original="Kaiju 8")
    first = world.manga("Kaiju No. 8!")
    world.download(first, "Chapter 2")
    scan(world)
    second = world.manga("Kaiju No. 8")
    assert link(world, second, kavita_id) == 1
    assert world.rows("SELECT count(*) FROM series") == [(1,)]
    assert world.rows("SELECT manga_id, series_id FROM suwayomi_manga ORDER BY 1") == [(first, 1), (second, 1)]


@budget(load="db")
def test_link_merges_a_held_near_hit_into_the_series_another_source_has(world):
    kavita_id = world.kavita_series("Kaiju No. 8!", original="Kaiju 8")
    first = world.manga("Kaiju No. 8!")
    world.download(first, "Chapter 2")
    scan(world)  # strict hit -> series 1
    second = world.manga("Kaiju No. 8")
    world.download(second, "Chapter 3")
    scan(world)  # near hit -> held series 2
    assert link(world, second, kavita_id) == 1
    assert world.rows("SELECT id, status FROM series") == [(1, "ok")], "the held row is merged away"
    assert world.rows("SELECT series_id FROM chapter ORDER BY id") == [(1,), (1,)]
    assert world.rows("SELECT manga_id, series_id FROM suwayomi_manga ORDER BY 1") == [(first, 1), (second, 1)]


@budget(load="db")
def test_link_run_again_refreshes_the_names_and_folder(world):
    kavita_id = world.kavita_series("Old Name")
    manga = world.manga("Old Name")
    world.download(manga, "Chapter 2")
    scan(world)
    world.kavita.series_rows[kavita_id]["name"] = "New Name"
    link(world, manga, kavita_id)
    assert world.rows("SELECT name FROM series") == [("New Name",)]


@budget(load="db")
@pytest.mark.parametrize(
    "case", ["no manga", "no series", "other library", "no folder", "2 rows with chapters", "unknown series"]
)
def test_link_refuses_what_it_cannot_link(world, case):
    kavita_id = world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    if case == "no manga":
        args = (9999, kavita_id)
    elif case == "no series":
        args = (manga, 9999)
    elif case == "other library":
        world.kavita.series_rows[kavita_id]["libraryId"] = 9
        args = (manga, kavita_id)
    elif case == "no folder":
        empty = world.kavita.add_series("Empty", "Empty", 4)
        args = (manga, empty)
    elif case == "unknown series":  # the manga's row has chapters; the Kavita series has no row yet
        world.download(manga, "Chapter 2")
        scan(world)
        args = (manga, world.kavita_series("Other"))
    else:
        world.download(manga, "Chapter 2")
        scan(world)  # manga -> series 1 (Solo Leveling)
        other = world.kavita_series("Other")
        other_manga = world.manga("Other")
        world.download(other_manga, "Chapter 2")
        scan(world)  # Kavita "Other" -> series 2, with a chapter
        args = (manga, other)
    with pytest.raises(workflow.Refused):
        link(world, *args)


@budget(load="db")
def test_status_lists_downloads_not_taken_and_published_files_never_confirmed(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Brand New"), "Chapter 1")
    scan(world)
    with closing(db.connect(world.cfg)) as conn:
        series_id = add_series(conn, "Solo Leveling", "kavita")
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO published_file (library_path, series_id, identity, state, origin)
                VALUES ('/lib/Ch.9.cbz', ?, 'x', 'unconfirmed', 'pipeline')""",
                (series_id,),
            )
    with closing(db.read_only(world.cfg)) as conn:
        text = "\n".join(status.report(conn))
    assert "Downloads not taken:\n" in text and "new series 'Brand New'" in text
    assert "Published files never confirmed:\n  /lib/Ch.9.cbz" in text


@budget(load="db")
def test_link_of_a_manga_whose_series_has_no_chapters_leaves_that_series_alone(world):
    solo = world.kavita_series("Solo Leveling")
    other = world.kavita_series("Other")
    manga = world.manga("Solo Leveling")
    link(world, manga, solo)  # series 1, no chapters
    assert link(world, manga, other) == 2, "a new row for the other series"
    assert world.rows("SELECT id, kavita_series_id FROM series ORDER BY id") == [(1, solo), (2, other)]


@budget(load="db")
def test_merged_chapters_past_junk_go_back_to_junk(world):
    kavita_id = world.kavita_series("Kaiju No. 8!", original="Kaiju 8")
    first = world.manga("Kaiju No. 8!")
    world.download(first, "Chapter 2")
    scan(world)
    second = world.manga("Kaiju No. 8")
    world.download(second, "Chapter 3")
    scan(world)
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET stage = 'comicinfo', attempts = 2 WHERE series_id = 2")
    link(world, second, kavita_id)
    assert world.rows("SELECT stage, status, attempts FROM chapter WHERE id = 2") == [("junk", "ready", 0)], (
        "the series' baseline and handled digests apply to a merged chapter"
    )
