"""Numbered notice titles (SPEC "Chapter stages": discover and junk): a download whose title is a notice
(validate.non_chapter_names) but that has a chapter number is judged at junk by the bulk converter's rule: kept as a
chapter with a note when it has an end marker, at least the series' median page count, or no median is known."""

import json
from contextlib import closing

import pytest

from manga_pipeline import config, db, export, intake, notices, process, status

from tests.timing import budget
from tests.world import World, cbz, decide_junk
from tests.world import comicinfo as ci

NAMES = ["Notice", "Hiatus Notice", "Hiatus", "Author's Note"]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, validate={"non_chapter_names": NAMES})
    w.kavita_series("Solo Leveling")  # the bulk converter's Ch.1: 3 pages
    w.manga_id = w.manga("Solo Leveling")
    return w


def published(world, pages, numbers):
    """Pipeline-published files of the series (other chapter numbers), each of `pages` pages."""
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        for n in numbers:
            conn.execute(
                """INSERT INTO published_file (library_path, series_id, identity, state, pages_in, origin)
                VALUES (%s, 1, %s, 'confirmed', %s, 'pipeline')""",
                (f"/lib/Ch.{n}.cbz", json.dumps(["grp", None, "", str(n), ""]), pages),
            )


def chapter(world):
    ((stage, status_, error, notes),) = world.rows("SELECT stage, status, last_error, notes FROM chapter")
    return stage, status_, error or "", json.loads(notes or "[]")


def listed_notes(world):
    """The chapter notes `status` lists."""
    with closing(db.read_only(world.cfg)) as conn:
        lines = status.report(conn)
    start = lines.index("Chapter notes:") + 1
    return [line for line in lines[start:] if line.startswith("  chapter ")]


def kept(notes):
    return [n for n in notes if n.startswith(notices.KEPT)]


@budget(load="db", seconds=3)
def test_a_numbered_notice_title_with_an_end_marker_is_published_with_a_note(world):
    world.download(world.manga_id, "Chapter 131 - Author's Note [END]")
    world.run(intake)
    published(world, 14, (101, 102, 103))  # 3 pages against a median of 14: only the end marker keeps it
    for part in (process, export):
        world.run(part)
    assert world.rows("SELECT comicinfo_number FROM published_file WHERE pages_in = 3") == [("131",)]
    (note,) = kept(chapter(world)[3])
    assert note.endswith(": end marker") and len(listed_notes(world)) == 1, "published as 131, listed by status"


@budget(load="db", seconds=3)
def test_a_notice_title_numbered_only_by_its_comicinfo_is_numbered(world):
    world.download(world.manga_id, "Author's Note [END]", data=cbz(comicinfo=ci(Number="131")))
    for part in (intake, process, export):
        world.run(part)
    assert world.rows("SELECT comicinfo_number FROM published_file WHERE origin = 'pipeline'") == [("131",)]
    assert kept(chapter(world)[3]), "kept with its note"


@budget(load="db", seconds=3)
def test_a_short_numbered_notice_is_dropped_at_junk_once_and_not_listed(world):
    path = world.download(world.manga_id, "Chapter 12 - Hiatus", data=cbz(pages=1))[1]
    world.run(intake)
    published(world, 14, (101, 102, 103))
    world.run(process)
    stage, status_, error, _ = chapter(world)
    assert (stage, status_) == ("junk", "done") and error.startswith("dropped: "), "1 page against a median of 14"
    kept_copy = config.abs_path(world.cfg["paths"]["retention"]) / "Src (EN)" / "Solo Leveling" / path.name
    assert not path.exists() and kept_copy.exists(), "removed the retention way"
    assert world.rows("SELECT count(*) FROM handled_chapter WHERE outcome = 'dropped'") == [(1,)], "handled once"
    assert listed_notes(world) == [], "a dropped chapter is not listed"


@budget(load="db", seconds=3)
def test_a_numbered_notice_as_long_as_the_series_median_is_kept(world):
    world.download(world.manga_id, "Chapter 12 - Hiatus", data=cbz(pages=15))
    world.run(intake)
    published(world, 14, (101, 102, 103))
    world.run(process)
    stage, _, _, notes = chapter(world)
    assert stage != "junk" and "15 pages" in kept(notes)[0], "15 pages against a median of 14"


@budget(load="db", seconds=10)
def test_a_numbered_notice_in_a_strip_series_of_bulk_files_only_is_kept(tmp_path):
    world = World(tmp_path, validate={"non_chapter_names": NAMES})
    bulk = tuple((f"Ch.{n}.cbz", f"Grp_Chapter {n}", str(n)) for n in (1, 2, 3))
    world.kavita_series("Solo Leveling", bulk=bulk, layout="strip")
    world.download(world.manga("Solo Leveling"), "Chapter 12 - Hiatus", data=cbz(pages=1))
    world.run(intake)
    world.run(process)
    _stage, status_, error, notes = chapter(world)
    assert not error.startswith("dropped") and "no median" in kept(notes)[0], (
        "a bulk strip file's page count is of re-cut pages: no like-with-like median, so kept"
    )


@budget(load="db", seconds=5)
def test_a_kept_notice_waiting_on_a_junk_decision_stays_kept_when_the_median_rises(world):
    world.download(world.manga_id, "Chapter 12 - Hiatus", data=cbz(pages=15, credit=True))
    world.run(intake)
    published(world, 14, (101, 102, 103))
    world.run(process)
    stage, status_, _, notes = chapter(world)
    assert (stage, status_) == ("junk", "waiting") and len(kept(notes)) == 1, "kept, then waits on its credit page"
    published(world, 30, (104, 105, 106, 107, 108))
    assert decide_junk(world.cfg, "junk") == 1
    world.run(process)
    stage, _, error, notes = chapter(world)
    assert stage != "junk" and not error.startswith("dropped"), "once kept, never judged again"
    assert len(kept(notes)) == 1, "the note is written once"


@budget(load="db")
def test_an_unnumbered_notice_is_dropped_at_discover(world):
    world.download(world.manga_id, "Hiatus")
    world.run(intake)
    stage, status_, error, _ = chapter(world)
    assert (stage, status_) == ("discover", "done")
    assert "a notice" in error
