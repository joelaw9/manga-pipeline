"""Better copies (SPEC "Library copies", "Retroactive updates"): a download that is a better copy of a published file
replaces it under its name, with its naming, cover and the reader's progress kept, the download put aside at publish."""

import argparse
import io
import zipfile
from contextlib import closing

import pytest
from PIL import Image

from manga_pipeline import cli, config, db, export, intake, process, retention, rewrite
from manga_pipeline import comicinfo as comicinfo_xml

from tests.test_cover_rewrites import bulk_file, recorded_covers, refreshes
from tests.test_rewrites import SERIES, crash_after, files, progress, read, scan
from tests.timing import budget
from tests.world import World, cbz


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def library(world, cover=()):
    """Bulk Vol.1 Ch.1 and Vol.1 Ch.2 (4 PNG pages of 16 px each; cover: outputs holding a 000_cover page), confirmed
    and their covers read. Returns the Kavita id."""
    bulk = (("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1", "1"), ("Vol.1 Ch.2.cbz", "Grp_Chapter 2", "2", "1"))
    kid = world.kavita_series(SERIES, bulk=bulk, basis="filename")
    for name, number in (("Vol.1 Ch.1.cbz", 1), ("Vol.1 Ch.2.cbz", 2)):
        (world.library / SERIES / name).write_bytes(bulk_file(name, number, name in cover))
    world.kavita.scan_library(4)
    world.kavita.run_scans()
    world.run(intake)  # its baseline pass records the bulk files
    world.run(export)  # nothing published yet: a run that only reads the covers
    assert world.rows("SELECT count(*) FROM published_file WHERE cover_state IS NOT NULL") == [(2,)], "set up"
    return kid


def pages(width, n=4, seed=""):
    """A CBZ of n PNG pages `width` px wide (1.5 times as tall), each its own colour."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for i in range(1, n + 1):
            out = io.BytesIO()
            Image.new("RGB", (width, width * 3 // 2), (40 + i * 30, 90, 200 - i * 20 + len(seed))).save(out, "PNG")
            z.writestr(f"{i:03}.png", out.getvalue())
    return data.getvalue()


def copy(world, name="Chapter 2", width=20, n=4):
    """A download of the series' manga; returns its path."""
    manga = next((i for i, m in world.suwayomi.manga_rows.items() if m["title"] == SERIES), None)
    manga = manga or world.manga(SERIES)
    return world.download(manga, name, data=pages(width, n, name))[1]


def through(world):
    for part in (intake, intake, process, export):
        world.run(part)


def row(world, name="Vol.1 Ch.2.cbz"):
    ((r,),) = world.rows("SELECT to_jsonb(p) FROM published_file p WHERE library_path = %s", f"library/{SERIES}/{name}")
    return r


@budget(load="db", seconds=10)
def test_a_wider_copy_replaces_its_bulk_file_under_its_name_keeping_its_naming_and_progress(world):
    kid = library(world)
    read(world, kid, "Vol.1 Ch.2.cbz", 2)
    download = copy(world, width=20)  # 20 >= 1.05 x 16
    before = progress(world, kid)
    through(world)
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"], "1 file, under its name"
    assert not download.exists(), "the download put aside at publish"
    kept = config.abs_path(world.cfg["paths"]["retention"])
    assert any(p.name == "Vol.1 Ch.2.cbz" for p in (kept / "library-files").rglob("*.cbz")), "the old file kept"
    r = row(world)
    assert (r["origin"], r["median_page_width"], r["comicinfo_volume"], r["comicinfo_number"]) == (
        "pipeline",
        20,
        "1",
        "2",
    ), "the row describes the copy; its naming is the file's"
    assert r["pending_copy"] is None
    assert r["source_name"] == "Chapter 2"
    with zipfile.ZipFile(world.library / SERIES / "Vol.1 Ch.2.cbz") as z:
        fields = comicinfo_xml.read_all(z.read("ComicInfo.xml"))
    assert (fields["Volume"], fields["Number"]) == ("1", "2")
    assert world.rows(
        "SELECT count(*) FROM handled_chapter WHERE identity LIKE '%%\"2\"%%' AND content_digest IS NOT NULL"
        " AND outcome = 'published' AND handled_at > '2001'"
    ) != [(0,)]
    scan(world)
    world.run(export)
    assert progress(world, kid)["Vol.1 Ch.2.cbz"] == (2, 4), f"same name and volume: Kavita kept it ({before})"
    assert refreshes(world) == [("refresh_covers", world.cfg["kavita"]["library_id"], kid)], "page 1 is new"
    world.run(intake)
    assert world.rows("SELECT count(*) FROM chapter WHERE kind = 'download' AND status <> 'done'") == [(0,)], (
        "the download is never taken again"
    )


@budget(load="pure", seconds=1)
def test_a_copys_progress_is_scaled_to_its_page_count():
    assert rewrite.copy_shift(2, 4, 8) == 2, "page 2 of 4 is page 4 of 8"
    assert rewrite.copy_shift(3, 6, 4) == -1, "page 3 of 6 is page 2 of 4"
    assert rewrite.copy_shift(2, 4, 4) == 0


@budget(load="db", seconds=10)
def test_an_uncensored_copy_replaces_an_untagged_file_of_the_same_width(world):
    library(world)
    copy(world, name="Chapter 2 [R18]", width=16)
    through(world)
    assert row(world)["source_name"] == "Chapter 2 [R18]", "a higher edition rating wins whatever its width"


@budget(load="db", seconds=10)
@pytest.mark.parametrize("name", ["Chapter 2", "Chapter 2 [R15]"], ids=["untagged", "censored"])
def test_a_lower_rated_copy_of_an_uncensored_file_is_a_duplicate(world, name):
    library(world)
    world.rows(
        "UPDATE published_file SET source_name = 'Grp_Chapter 2 [R19]' WHERE library_path LIKE '%%Ch.2.cbz'"
        " RETURNING id"
    )
    copy(world, name=name, width=40)
    through(world)
    ((stage, status, error),) = world.rows("SELECT stage, status, last_error FROM chapter WHERE kind = 'download'")
    assert (stage, status) == ("junk", "done"), "wider, but a lower rating"
    assert "a duplicate" in error, "wider, but a lower rating"
    assert row(world)["origin"] == "bulk"


@budget(load="db", seconds=10)
def test_a_copy_takes_its_files_own_cover_page_when_no_image_is_stored(world):
    kid = library(world, cover=("Vol.1 Ch.2.cbz",))
    recorded_covers(world, kid, {1: "Vol.1 Ch.2.cbz"})
    world.run(export)
    copy(world, width=20, n=5)  # 5 pages, as the file with its cover page
    through(world)
    assert row(world)["origin"] == "pipeline", "replaced by the copy"
    with zipfile.ZipFile(world.library / SERIES / "Vol.1 Ch.2.cbz") as z:
        names = z.namelist()
    assert names[0].startswith("000_cover."), "a chapter keeps its cover"
    assert row(world)["cover_state"] == "volume:1", "a chapter keeps its cover"


@budget(load="db", seconds=10)
def test_a_busy_file_holds_the_copy_and_chapter_drop_lets_it_go(world):
    library(world)
    world.rows("UPDATE published_file SET pending_error = 'stuck' WHERE library_path LIKE '%%Ch.2.cbz' RETURNING id")
    download = copy(world, width=20)
    through(world)
    ((chapter_id, status, error),) = world.rows("SELECT id, status, last_error FROM chapter WHERE kind = 'download'")
    assert status == "needs_human"
    assert "busy" in error

    cli.chapter_drop(world.cfg, argparse.Namespace(chapter=chapter_id))
    assert not download.exists()
    assert world.rows("SELECT count(*) FROM handled_chapter WHERE outcome = 'dropped'") == [(1,)]


@budget(load="db", seconds=10)
@pytest.mark.parametrize("step", [1, 2, 3, 4])
def test_a_crash_after_each_act_of_a_copys_replacement_recovers(world, monkeypatch, step):
    library(world)
    copy(world, width=20)
    for part in (intake, intake, process):
        world.run(part)
    crash_after(monkeypatch, step, "Vol.1 Ch.2.cbz")
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    world.run(export)
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"]
    r = row(world)
    assert (r["origin"], r["median_page_width"], r["pending_copy"]) == ("pipeline", 20, None)


@budget(load="db", seconds=10)
def test_a_copy_changes_no_other_chapters_name(world):
    library(world)
    copy(world, width=20)
    world.download(next(iter(world.suwayomi.manga_rows)), "Chapter 3", data=cbz(pages=4, seed="3"))
    through(world)
    assert files(world) == ["Ch.3.cbz", "Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"], "chapter 3 published beside the copy"


@budget(load="db", seconds=10)
def test_a_dropped_copy_replacement_leaves_the_file_and_records_the_copy_dropped(world, monkeypatch):
    library(world)
    copy(world, width=20)
    monkeypatch.setattr(retention, "put_library", lambda *a: (_ for _ in ()).throw(PermissionError("denied")))
    for part in (intake, intake, process, export, export, export):
        world.run(part)
    monkeypatch.undo()
    ((file_id,),) = world.rows("SELECT id FROM published_file WHERE pending_copy IS NOT NULL")
    with closing(db.connect(world.cfg)) as conn:
        rewrite.drop(conn, file_id, world.cfg["workflow"]["attempt_limit"])
    assert row(world)["origin"] == "bulk", "the file stays as it is"
    assert row(world)["pending_copy"] is None, "the file stays as it is"
    assert world.rows(
        "SELECT outcome FROM handled_chapter WHERE identity = (SELECT identity FROM chapter WHERE kind = 'download')"
    ) == [("dropped",)], "the copy is recorded dropped, not published"


@budget(load="db", seconds=10)
def test_a_copy_of_a_file_whose_cover_is_not_read_yet_keeps_the_files_cover(world):
    library(world, cover=("Vol.1 Ch.2.cbz",))
    world.rows("UPDATE published_file SET cover_state = NULL RETURNING id")  # baselined, not yet read by export
    copy(world, width=20, n=5)  # 5 pages, as the file with its cover page
    for part in (intake, intake, process):
        world.run(part)
    assert world.rows("SELECT v.volume FROM volume_cover v JOIN chapter c ON c.id = v.chapter_id") == [("1",)], (
        "the file's own cover page goes with the copy"
    )
