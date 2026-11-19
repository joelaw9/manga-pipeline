"""Source review's survey, measure, search, switch, replace and retire (SPEC "Source review")."""

import errno
import logging
from contextlib import closing

import pytest

from manga_pipeline import cli, config, db, export, intake, library_copy, lock, process

from tests.builders import add_chapter, add_series, running, settle
from tests.source_world import FILES, SERIES, candidate, library, link, main, make_world, pages, zipped
from tests.timing import budget


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    for handler in logging.getLogger("manga_pipeline").handlers[:]:
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def store_measures(w, series_id, width, bpp):
    """Give a series' files stored measures, as publishing stores them: 3 pages, this width and bits per pixel."""
    w.rows(
        "UPDATE published_file SET pages_in = 3, median_page_width = %s, strip_length = 4500, bpp = %s"
        " WHERE series_id = %s RETURNING id",
        width,
        bpp,
        series_id,
    )


# ---------------------------------------------------------------- survey, measure, search


@budget(load="db", seconds=4)
def test_survey_lists_a_series_crossing_a_trigger_first_and_leaves_a_series_in_flight_out(world, capsys):
    beta = library(world, "Beta")
    store_measures(world, 1, 1000, 1.5)
    store_measures(world, beta, 700, 1.5)
    with closing(db.connect(world.cfg)) as conn:
        add_series(conn, "Gamma")  # new, in flight: not in Kavita yet
    code, lines, _ = main(world, capsys, "source", "survey")
    assert code == 0, lines
    assert len(lines) == 2, f"only the series Kavita holds: {lines}"
    assert lines[0].startswith(f"series {beta} Beta"), "the series crossing a trigger is listed first"
    assert lines[0].endswith("triggers: width"), lines[0]
    assert lines[1].startswith("series 1 Alpha"), lines[1]
    assert lines[1].endswith("triggers: none"), lines[1]


@budget(load="db", seconds=4)
def test_survey_lists_a_series_with_an_unreadable_file_with_its_error_and_goes_on(world, capsys):
    beta = library(world, "Beta")
    ((file_id, stored),) = world.rows(
        "SELECT id, library_path FROM published_file WHERE series_id = %s ORDER BY id DESC LIMIT 1", beta
    )
    config.to_local(world.cfg, stored).write_bytes(b"not a zip")
    code, lines, _ = main(world, capsys, "source", "survey")
    assert code == 0, lines
    assert lines[0].startswith("series 1 Alpha"), "the next series is still surveyed"
    assert lines[1].startswith(f"series {beta} Beta (pages, 5 files): file {file_id} ({stored}) cannot be read:")


@budget(load="db", seconds=3)
def test_survey_ends_on_an_unreachable_share(world, capsys, monkeypatch):
    def dropped(*_args):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(library_copy, "original", dropped)
    code, lines, err = main(world, capsys, "source", "survey")
    assert code == 1
    assert lines == [], "a share that cannot be reached is no file's defect"
    assert "share" in err


@budget(load="db", seconds=3)
def test_measure_reads_a_bulk_series_from_its_files_with_no_bits_per_pixel(world, capsys):
    manga, _ = candidate(world, in_library=True)
    link(world, manga)
    code, lines, _ = main(world, capsys, "source", "measure", "1")
    assert code == 0, lines
    files = [line for line in lines if line.startswith("file ")]
    assert len(files) == FILES
    assert all("width 16, bits per pixel -," in line for line in files), "a bulk file's pages are re-encoded"
    assert "combined (latest 5): width 16, bits per pixel -, junk 0" in lines
    assert "triggers: width" in lines
    assert f"manga {manga} {SERIES}: Better (EN), in Suwayomi's library" in lines


@budget(load="db", seconds=3)
def test_a_series_in_flight_is_refused(world, capsys):
    with closing(db.connect(world.cfg)) as conn:
        new = add_series(conn, "Gamma")
    for argv in (["measure", str(new)], ["search", str(new)], ["sample", str(new), "1"]):
        code, _, err = main(world, capsys, "source", *argv)
        assert code == 2, f"{argv}: a series Kavita does not hold yet is refused"
    assert "not in Kavita yet: run once it is published" in err


@budget(load="db", seconds=3)
def test_search_of_a_series_with_no_linked_manga_reports_coverage_and_numbering(world, capsys):
    manga, _ = candidate(world, range(1, FILES))  # no chapter 5
    code, lines, _ = main(world, capsys, "source", "search", "1", "--source", "Better (EN)", "--source", "Gone (EN)")
    assert code == 0, lines
    found = (
        f"Better (EN): manga {manga} {SERIES}: 4 chapters, lists 4 of 5 published keys, numbering agrees on 0 of 3"
        " sample files, not in Suwayomi's library, linked to no series"
    )
    assert lines == [found, "Gone (EN): not installed"], (
        "the series is searched against its published files; with key 5 missing, no sample file's neighbours agree"
    )
    assert ("fetch_manga_and_chapters", manga) in world.suwayomi.writes, "a manga outside the library is fetched"


@budget(load="db", seconds=3)
def test_search_reads_a_library_manga_from_its_stored_chapters_and_never_fetches_it(world, capsys):
    manga, _ = candidate(world, in_library=True)
    world.suwayomi.add_chapter(manga, "Chapter 6", scanlator="Better", listed=False)  # new at the source
    code, lines, _ = main(world, capsys, "source", "search", "1", "--source", "Better (EN)")
    assert code == 0, lines
    assert "5 chapters, lists 5 of 5 published keys, numbering agrees on 3 of 3" in lines[0], lines
    assert "in Suwayomi's library" in lines[0]
    assert not [w for w in world.suwayomi.writes if w[0] == "fetch_manga_and_chapters"], "a library manga is read"


@budget(load="db", seconds=3)
def test_search_reads_a_linked_manga_outside_the_library_from_its_stored_chapters(world, capsys):
    manga, _ = candidate(world)  # not in Suwayomi's library
    link(world, manga)
    world.suwayomi.add_chapter(manga, "Chapter 6", scanlator="Better", listed=False)  # new at the source
    code, lines, _ = main(world, capsys, "source", "search", "1", "--source", "Better (EN)")
    assert code == 0, lines
    assert "5 chapters," in lines[0], lines
    assert "not in Suwayomi's library, linked to series 1" in lines[0], lines
    assert not [w for w in world.suwayomi.writes if w[0] == "fetch_manga_and_chapters"], "a linked manga is read"


# ---------------------------------------------------------------- switch


@budget(load="db", seconds=4)
def test_switch_links_and_adds_the_manga_queues_nothing_and_is_a_no_op_again(world, capsys):
    old = world.manga(SERIES)
    link(world, old)
    manga, _ = candidate(world)
    code, lines, _ = main(world, capsys, "source", "switch", "1", str(manga), "--yes")
    assert code == 0, lines
    assert world.rows("SELECT series_id FROM suwayomi_manga WHERE manga_id = %s", manga) == [(1,)]
    assert world.suwayomi.manga_rows[manga]["inLibrary"]
    code, lines, _ = main(world, capsys, "source", "switch", "1", str(manga), "--yes")
    assert code == 0
    assert lines == [f"manga {manga} is already linked to series 1 and in Suwayomi's library: nothing changed"]
    assert [w for w in world.suwayomi.writes if w[0] == "set_in_library"] == [("set_in_library", manga, True)]
    assert not world.suwayomi.queue, "nothing is queued"
    assert not [w for w in world.suwayomi.writes if w[0] in ("enqueue", "start_downloader")]
    assert world.suwayomi.manga_rows[old]["inLibrary"], "the old manga stays in the library"
    assert world.rows("SELECT manga_id FROM suwayomi_manga ORDER BY manga_id") == [(old,), (manga,)]


@budget(load="db", seconds=3)
def test_switch_is_refused_for_a_manga_linked_elsewhere_or_with_no_installed_source(world, capsys):
    beta = library(world, "Beta")
    manga, _ = candidate(world)
    link(world, manga, beta)
    code, _, err = main(world, capsys, "source", "switch", "1", str(manga), "--yes")
    assert code == 2
    assert f"manga {manga} is linked to series {beta}" in err
    orphan = world.suwayomi.add_manga(SERIES, "999")  # its extension is gone
    code, _, err = main(world, capsys, "source", "switch", "1", str(orphan), "--yes")
    assert code == 2
    assert "its source is not installed" in err
    assert world.rows("SELECT manga_id FROM suwayomi_manga") == [(manga,)], "a refused switch links nothing"
    assert not [w for w in world.suwayomi.writes if w[0] == "set_in_library"]


# ---------------------------------------------------------------- replace


def switched(w, capsys, numbers=range(1, FILES + 1)):
    manga, chapters = candidate(w, numbers)
    assert main(w, capsys, "source", "switch", "1", str(manga), "--yes")[0] == 0
    return manga, chapters


@budget(load="db", seconds=4)
def test_replace_queues_the_named_files_chapters_and_skips_a_busy_file_an_unfinished_key_and_no_match(world, capsys):
    manga, chapters = switched(world, capsys)
    world.suwayomi.add_chapter(manga, "Chapter 5", scanlator="Other")  # 2 groups list key 5, neither the file's
    world.rows("UPDATE published_file SET pending_error = 'rename failed' WHERE id = 1 RETURNING id")  # busy
    with closing(db.connect(world.cfg)) as conn:
        add_chapter(conn, 1, '["grp", null, "", "2", ""]', "junk")  # an unfinished chapter of key 2
    code, lines, _ = main(world, capsys, "source", "replace", "1", str(manga), "--all", "--yes")
    assert code == 0, lines
    assert lines == [
        "file 1 #1: skipped: busy with a change (status lists it)",
        "file 2 #2: skipped: an unfinished chapter of the series has its chapter key",
        f"file 3 #3: queued chapter {chapters[3]} Chapter 3",
        f"file 4 #4: queued chapter {chapters[4]} Chapter 4",
        "file 5 #5: skipped: no match on the manga",
        "queued 2 of 5",
    ]
    assert [e["chapter"] for e in world.suwayomi.queue] == [chapters[3], chapters[4]]
    assert world.suwayomi.downloader == "STARTED"


@budget(load="db", seconds=3)
def test_replace_skips_a_chapter_suwayomi_has_downloaded_with_its_file_in_place(world, capsys):
    manga, chapters = switched(world, capsys)
    downloaded = world.suwayomi.path(chapters[3])
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(zipped(pages()))
    world.suwayomi.chapter_rows[chapters[3]]["isDownloaded"] = True
    world.suwayomi.chapter_rows[chapters[4]]["isDownloaded"] = True  # flagged, its file gone: Suwayomi downloads it
    code, lines, _ = main(world, capsys, "source", "replace", "1", str(manga), "--file", "3", "--file", "4", "--yes")
    assert code == 0, lines
    assert lines == [
        f"file 3 #3: skipped: Suwayomi has chapter {chapters[3]} downloaded, its file in place: that file arrives",
        f"file 4 #4: queued chapter {chapters[4]} Chapter 4",
        "queued 1 of 2",
    ], "Suwayomi queues no second copy of a chapter whose file is in place"
    assert [e["chapter"] for e in world.suwayomi.queue] == [chapters[4]]


@budget(load="db", seconds=3)
def test_replace_is_refused_for_an_unlinked_manga_or_a_file_of_another_series(world, capsys):
    manga, _ = candidate(world, in_library=True)
    code, _, err = main(world, capsys, "source", "replace", "1", str(manga), "--file", "3", "--yes")
    assert code == 2
    assert f"manga {manga} is not linked to series 1" in err
    link(world, manga)
    code, _, err = main(world, capsys, "source", "replace", "1", str(manga), "--file", "99", "--yes")
    assert code == 2
    assert "no published file 99 of series 1" in err
    assert not world.suwayomi.queue


def through(w):
    for part in (intake, intake, process, export):
        w.run(part)


@budget(load="db", seconds=5)
def test_a_replaced_better_copy_takes_its_file_and_a_worse_one_is_dropped(world, capsys):
    manga, chapters = switched(world, capsys)
    for file_id, width in ((3, 24), (4, 8)):
        assert main(world, capsys, "source", "replace", "1", str(manga), "--file", str(file_id), "--yes")[0] == 0
        world.suwayomi.finish_downloads(zipped(pages(width, seed=str(width))))
        settle(world.suwayomi.path(chapters[file_id]))
        through(world)
    rows = world.rows("SELECT id, origin, source_name FROM published_file WHERE id IN (3, 4) ORDER BY id")
    assert rows == [(3, "pipeline", "Better_Chapter 3"), (4, "bulk", "Grp_Chapter 4")], "the wider copy replaced 3"
    ((status, error),) = world.rows(
        "SELECT status, last_error FROM chapter WHERE kind = 'download' AND identity LIKE '%%\"4\"%%'"
    )
    assert status == "done"
    assert "a duplicate" in error, "the narrower copy is dropped"


# ---------------------------------------------------------------- retire


def two_sources(w):
    """Link the old manga (chapters 1 to 3) and switch to the new one (chapters 1 to 4); return (old, new, chapters)."""
    old = w.manga(SERIES)
    for n in range(1, 4):
        w.suwayomi.add_chapter(old, f"Chapter {n}", scanlator="Grp")
    link(w, old)
    new, chapters = candidate(w, range(1, FILES), in_library=True)
    link(w, new)
    return old, new, chapters


@budget(load="db", seconds=3)
def test_retire_takes_the_manga_out_of_the_library_unlinks_it_and_prints_the_keys_left_uncovered(world, capsys):
    old, new, _ = two_sources(world)
    code, lines, _ = main(world, capsys, "source", "retire", "1", str(old), "--yes")
    assert code == 0, lines
    assert lines[0] == "published keys no other linked manga lists: #5"
    assert not world.suwayomi.manga_rows[old]["inLibrary"]
    assert world.rows("SELECT manga_id FROM suwayomi_manga") == [(new,)]


@budget(load="db", seconds=4)
def test_retire_is_refused_while_its_manga_has_a_queued_chapter_a_file_or_an_expected_chapter(world, capsys):
    _, new, chapters = two_sources(world)
    world.suwayomi.enqueue([chapters[1]])
    ran = main(world, capsys, "source", "retire", "1", str(new), "--yes")
    assert ran.code == 2
    assert "in Suwayomi's download queue" in ran.err
    world.suwayomi.queue.clear()
    downloaded = world.suwayomi.path(chapters[2])
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(b"PK")
    ran = main(world, capsys, "source", "retire", "1", str(new), "--yes")
    assert ran.code == 2
    assert "CBZ files in its download folder" in ran.err
    downloaded.unlink()
    with closing(db.connect(world.cfg)) as conn:
        add_chapter(conn, 1, '["better", null, "", "3", ""]', "validate", suwayomi_chapter_id=chapters[3])
    ran = main(world, capsys, "source", "retire", "1", str(new), "--yes")
    assert ran.code == 2
    assert "unfinished chapters hold or expect" in ran.err
    assert world.suwayomi.manga_rows[new]["inLibrary"], "a refused retire changes nothing"
    assert len(world.rows("SELECT 1 FROM suwayomi_manga")) == 2


@budget(load="db", seconds=3)
def test_retire_is_refused_while_a_re_download_expects_a_chapter_of_its_manga(world, capsys):
    _, new, chapters = two_sources(world)
    expects = f'{{"expected": {{"manga_id": {new}, "chapter_id": {chapters[4]}}}}}'
    with closing(db.connect(world.cfg)) as conn:
        add_chapter(conn, 1, '["grp", null, "", "4", ""]', "validate", redownload=expects)
    ran = main(world, capsys, "source", "retire", "1", str(new), "--yes")
    assert ran.code == 2
    assert "unfinished chapters hold or expect" in ran.err
    assert world.suwayomi.manga_rows[new]["inLibrary"]


@budget(load="db", seconds=4)
def test_retire_ends_a_manga_whose_source_is_uninstalled_once_no_source_folder_holds_its_title(world, capsys):
    gone = world.suwayomi.add_manga("Delta", "999")
    link(world, gone)
    del world.suwayomi.manga_rows[gone]
    ran = main(world, capsys, "source", "retire", "1", str(gone), "--yes")
    assert ran.code == 2
    assert f"Suwayomi has no manga {gone}" in ran.err, "a manga Suwayomi no longer has is refused, naming why"
    orphan = world.suwayomi.add_manga(SERIES, "999")  # its extension is gone: no source folder can be named
    world.suwayomi.add_chapter(orphan, "Chapter 1", scanlator="Gone")
    link(world, orphan)
    left = world.mangas / "Gone (EN)" / SERIES / "Gone_Chapter 1.cbz"
    left.parent.mkdir(parents=True)
    left.write_bytes(b"PK")
    ran = main(world, capsys, "source", "retire", "1", str(orphan), "--yes")
    assert ran.code == 2
    assert "CBZ files in its download folder" in ran.err, "the folder of its title under any source folder counts"
    left.unlink()
    ran = main(world, capsys, "source", "retire", "1", str(orphan), "--yes")
    assert ran.code == 0, ran
    assert not world.suwayomi.manga_rows[orphan]["inLibrary"], "the old source's manga leaves Suwayomi's library"
    assert world.rows("SELECT manga_id FROM suwayomi_manga WHERE manga_id = %s", orphan) == []


# ---------------------------------------------------------------- locks


@budget(load="db", seconds=3)
@pytest.mark.parametrize("command", ["switch", "replace", "retire"])
def test_a_changing_command_is_refused_while_intake_runs(world, capsys, command):
    manga, _ = candidate(world, in_library=True)
    link(world, manga)
    argv = ["source", command, "1", str(manga), *(["--all"] if command == "replace" else []), "--yes"]
    with running(world.cfg, "intake"):
        assert main(world, capsys, *argv)[0] == 3, "intake's lock is the database's: held on any machine"
    assert world.suwayomi.manga_rows[manga]["inLibrary"]
    assert not [w for w in world.suwayomi.writes if w[0] in ("set_in_library", "enqueue")]


@budget()
def test_every_locked_source_command_holds_the_idle_lock_shared_so_state_reset_waits_for_it():
    locked = {name: c for name, c in cli.COMMANDS.items() if name.startswith("source ") and c.locked}
    assert set(locked) == {"source sample", "source switch", "source replace", "source retire"}, locked
    assert all(c.idle == lock.SHARED for c in locked.values()), {n: c.idle for n, c in locked.items()}
    assert cli.COMMANDS["state reset"].idle == lock.EXCLUSIVE, "state reset waits for every holder of the idle lock"
