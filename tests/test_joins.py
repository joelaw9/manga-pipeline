"""Split chapters' joins (SPEC "Retroactive updates", Split chapters): a run of published parts becomes chapter N."""

import zipfile
from contextlib import closing

import pytest

from manga_pipeline import archive, comicinfo, config, db, export, intake, joins, process, rewrite, state
from manga_pipeline.db import sql

from tests.timing import budget
from tests.world import World, cbz

SERIES = "Solo Leveling"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def published(world, *chapters):
    """Add the bulk series and publish the chapters (Suwayomi names, e.g. "Chapter 3.1") through the pipeline.

    Each download holds 4 pages. Returns the Kavita id once every file is confirmed and no join runs yet: the cap is
    0 while they are published.
    """
    world.cfg["export"]["rewrites_open"] = 0
    kid = world.kavita_series(SERIES, basis="filename")
    manga = world.manga(SERIES)
    for name in chapters:
        world.download(manga, name, data=cbz(pages=4, seed=name))
    for part in (intake, intake, process, export):
        world.run(part)
    world.kavita.run_scans()
    world.run(export)
    world.cfg["export"]["rewrites_open"] = 50
    return kid


def files(world):
    return sorted(p.name for p in (world.library / SERIES).iterdir() if p.name.endswith(".cbz"))


def pages(path):
    """Return the page members' bytes of a CBZ, in reading order."""
    with zipfile.ZipFile(path) as z:
        return [z.read(n) for n in archive.archive_pages(z.namelist())]


def scan(world):
    world.rows("UPDATE meta SET value = '2000-01-01T00:00:00+00:00' WHERE key = %s RETURNING key", export.REQUESTED_AT)
    world.run(export)
    world.kavita.run_scans()


def read(world, kid, name, n):
    f = next(f for f in world.kavita.series_files(kid) if f["path"].name == name)
    world.kavita.save_progress(kid, f["volume_id"], f["chapter_id"], n)


def progress(world, kid):
    return {f["path"].name: (f["pages_read"], f["pages"]) for f in world.kavita.series_files(kid)}


@budget(load="db", seconds=60)
def test_a_published_run_ends_as_1_file_holding_every_page_in_order_with_progress_kept(world):
    kid = published(world, "Chapter 3.1", "Chapter 3.2")
    folder = world.library / SERIES
    assert {"Ch.3.1.cbz", "Ch.3.2.cbz"} <= set(files(world)), "set up: each part published alone first"
    parts = pages(folder / "Ch.3.1.cbz") + pages(folder / "Ch.3.2.cbz")
    read(world, kid, "Ch.3.1.cbz", 4)  # read in full
    read(world, kid, "Ch.3.2.cbz", 2)
    world.run(export)  # queues the join, builds it, queues and finishes the replacement
    assert "Ch.3.cbz" in files(world), "chapter N"
    assert not {"Ch.3.1.cbz", "Ch.3.2.cbz"} & set(files(world)), "the parts are gone from the library"
    assert pages(folder / "Ch.3.cbz") == parts, "every page of both parts, byte for byte, in order"
    with zipfile.ZipFile(folder / "Ch.3.cbz") as z:
        fields = comicinfo.read_all(z.read(archive.COMICINFO))
        entries = comicinfo.read_pages(z.read(archive.COMICINFO))
    assert (fields["Number"], fields["Title"], fields["PageCount"]) == ("3", "Chapter 3", "8")
    assert entries == [] or len(entries) == 8, "Page entries name every page, or there are none"
    rows = world.rows(
        """SELECT library_path, comicinfo_number, joined_parts, identity::jsonb ->> 3 FROM published_file
        WHERE library_path LIKE %s""",
        f"library/{SERIES}/Ch.3%",
    )
    assert rows == [(f"library/{SERIES}/Ch.3.cbz", "3", 2, "3")], "1 row: chapter N, holding 2 parts"
    kept = {p.name for p in config.abs_path(world.cfg["paths"]["retention"]).rglob("*.cbz")}
    assert {"Ch.3.1.cbz", "Ch.3.2.cbz"} <= kept, "both parts kept the retention way"
    scan(world)
    world.run(export)
    assert progress(world, kid)["Ch.3.cbz"][0] == 6, "part 1 read in full (4) plus 2 pages of part 2"


@budget(load="db", seconds=60)
def test_a_later_part_joins_the_joined_file(world):
    published(world, "Chapter 3.1", "Chapter 3.2")
    world.run(export)
    scan(world)
    world.run(export)
    before = pages(world.library / SERIES / "Ch.3.cbz")
    manga = world.suwayomi.manga_rows and next(iter(world.suwayomi.manga_rows))
    world.download(manga, "Chapter 3.3", data=cbz(pages=4, seed="3.3"))
    world.cfg["export"]["rewrites_open"] = 0
    for part in (intake, intake, process, export):
        world.run(part)
    scan(world)
    world.run(export)  # confirmed
    part3 = pages(world.library / SERIES / "Ch.3.3.cbz")
    world.cfg["export"]["rewrites_open"] = 50
    world.run(export)
    assert pages(world.library / SERIES / "Ch.3.cbz") == before + part3, "the joined file, then part 3"
    assert world.rows("SELECT joined_parts FROM published_file WHERE comicinfo_number = '3'") == [(3,)]
    assert "Ch.3.3.cbz" not in files(world)


@budget(load="db", seconds=60)
def test_join_parts_false_keeps_the_parts(world):
    published(world, "Chapter 3.1", "Chapter 3.2")
    folder = config.abs_path(world.cfg["paths"]["naming_overrides"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{SERIES}.yaml").write_bytes(b"join_parts: false\n")
    world.run(export)
    assert {"Ch.3.1.cbz", "Ch.3.2.cbz"} <= set(files(world)), "the series' switch: no join"


@budget(load="db", seconds=60)
def test_a_part_still_open_holds_the_join(world):
    published(world, "Chapter 3.1", "Chapter 3.2")
    manga = next(iter(world.suwayomi.manga_rows))
    world.download(manga, "Chapter 3.2", "Other", data=cbz(pages=4, seed="other 3.2"))
    world.run(intake)  # discovered: open, its key the part's
    world.run(export)
    assert {"Ch.3.1.cbz", "Ch.3.2.cbz"} <= set(files(world)), "no join while a download of a part is open"


@budget(load="db", seconds=60)
def test_the_parts_done_chapters_name_the_joined_file_and_their_rows_go(world):
    published(world, "Chapter 3.1", "Chapter 3.2")
    (part2,) = world.rows("SELECT id FROM published_file WHERE comicinfo_number = '3.2'")[0]
    named = world.rows("SELECT count(*) FROM chapter WHERE published_file_id = %s", part2)[0][0]
    world.rows(
        "UPDATE chapter SET published_file_id = %s WHERE library_path LIKE %s RETURNING id",
        part2,
        f"library/{SERIES}/Ch.3.2.cbz",
    )  # as a conversion's chapter names its adopted file
    world.run(export)
    (joined,) = world.rows("SELECT id FROM published_file WHERE comicinfo_number = '3'")[0]
    assert world.rows("SELECT count(*) FROM published_file WHERE id = %s", part2) == [(0,)], "the part's row is gone"
    assert world.rows("SELECT count(*) FROM chapter WHERE published_file_id = %s", joined)[0][0] >= named + 1, (
        "every chapter that named it names the joined file"
    )


@budget(load="db", seconds=60)
def test_a_reset_or_a_dropped_join_leaves_no_file_busy(world):
    published(world, "Chapter 3.1", "Chapter 3.2")
    with closing(db.connect(world.cfg)) as conn:
        joins.queue(world.context(conn, "export"))
    busy = sql.SQL("SELECT count(*) FROM published_file p WHERE {}").format(rewrite.BUSY.format(p=sql.Identifier("p")))
    assert world.rows(busy)[0][0] == 2, "an open join holds both files"
    with closing(db.connect(world.cfg)) as conn:
        state.reset(world.cfg, conn)
    assert world.rows(busy)[0][0] == 0, "a reset deletes the chapter: nothing holds them"


@budget(load="db", seconds=60)
def test_a_finish_stopped_while_removing_the_parts_is_finished_by_the_next_run(world, monkeypatch):
    published(world, "Chapter 3.1", "Chapter 3.2")
    real = rewrite.record_step

    def stops_after_the_files(conn, file_id, step):
        if step == rewrite.STEP_PARTS_REMOVED:
            msg = "stopped after the files went, before the step was recorded"
            raise RuntimeError(msg)
        return real(conn, file_id, step)

    with monkeypatch.context() as m:
        m.setattr(rewrite, "record_step", stops_after_the_files)
        world.run(export)
    assert world.rows("SELECT count(*) FROM published_file WHERE pending_error IS NOT NULL") == [(1,)], "recorded"
    world.rows("UPDATE published_file SET pending_error = NULL RETURNING id")
    world.run(export)  # the step again: the files already gone are skipped
    assert world.rows(
        "SELECT comicinfo_number, joined_parts FROM published_file WHERE comicinfo_number LIKE '3%%'"
    ) == [("3", 2)], "settled: 1 row, chapter N holding 2 parts"


@budget(load="db", seconds=60)
def test_an_unreadable_part_holds_the_join_for_a_person(world):
    published(world, "Chapter 3.1", "Chapter 3.2")
    (world.library / SERIES / "Ch.3.2.cbz").write_bytes(b"not a zip")
    world.run(export)
    assert world.rows("SELECT status FROM chapter WHERE join_parts IS NOT NULL") == [("needs_human",)]
    assert {"Ch.3.1.cbz", "Ch.3.2.cbz"} <= set(files(world)), "nothing changed in the library"
