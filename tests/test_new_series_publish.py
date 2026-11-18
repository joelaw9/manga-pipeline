"""New-series publishing (SPEC "Publishing", "Metadata checks").

An admitted new series lands whole in its own folder under the run's 1 library scan, never while a scan is awaited;
once Kavita lists it, its id is found by its name and folder, once per run, and it is an ordinary series.
"""

from types import SimpleNamespace

import pytest

from manga_pipeline import config, export, intake, process, workflow
from manga_pipeline.stages import comicinfo

from tests.timing import budget
from tests.world import LIBRARY_ID, World, cbz


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def ready(world, title="Brand New Thing", chapters=(1, 2, 3), al_id=7, english=None):
    """Add a new series whose chapters all wait for export at comicinfo.

    Admitted (under english, its AniList English title, when given), then taken, validated and processed.
    """
    media = {"id": al_id, "format": "MANGA", "title": {"romaji": title, "english": english, "native": None}}
    world.sources.anilist.results[title] = [media]
    world.sources.anilist.records[("media", str(al_id))] = media
    manga = world.manga(title)
    for n in chapters:
        world.download(manga, f"Chapter {n}", data=cbz(seed=f"{title}{n}", pages=4))
    world.run(intake)
    world.run(intake)
    world.run(process)
    ((series_id,),) = world.rows("SELECT id FROM series WHERE name = %s", title)
    assert set(world.rows("SELECT stage FROM chapter WHERE series_id = %s", series_id)) == {("comicinfo",)}, "set up"
    return series_id, manga


def stages(world, series_id):
    return sorted(world.rows("SELECT stage, status FROM chapter WHERE series_id = %s", series_id))


def scans(world):
    return [w for w in world.kavita.writes if w[0] == "scan_library"]


def files(world, folder):
    return sorted(p.name for p in (world.library / folder).iterdir())


def kavita_id(world, series_id):
    return world.rows("SELECT kavita_series_id FROM series WHERE id = %s", series_id)[0][0]


@budget(load="db", seconds=20)
def test_a_ready_new_series_lands_whole_under_1_scan_and_is_attached_by_its_folder(world):
    sid, _ = ready(world)
    world.run(export)
    assert files(world, "Brand New Thing") == ["Ch.1.cbz", "Ch.2.cbz", "Ch.3.cbz"], "all its chapters together"
    assert scans(world) == [("scan_library", LIBRARY_ID)], "the run's 1 library scan"
    assert kavita_id(world, sid) is None, "Kavita has not scanned yet"
    world.kavita.run_scans()
    world.run(export)
    assert kavita_id(world, sid) is not None, "found by its name and folder"
    assert stages(world, sid) == [("confirm", "done")] * 3, "confirmed as any series"
    assert world.rows("SELECT count(*) FROM series_written WHERE series_id = %s", sid) == [(1,)], "fields written"


@budget(load="db", seconds=30)
def test_2_new_series_ready_together_land_under_1_scan(world):
    _a, _ = ready(world, "Brand New Thing", al_id=7)
    _b, _ = ready(world, "Other New Thing", al_id=8)
    world.run(export)
    assert scans(world) == [("scan_library", LIBRARY_ID)], "1 scan for both"
    assert len(files(world, "Brand New Thing")) == len(files(world, "Other New Thing")) == 3


@budget(load="db", seconds=30)
def test_a_new_series_waits_while_a_scan_is_awaited_and_publishes_in_the_run_after(world):
    ready(world, "Brand New Thing", al_id=7)
    world.run(export)  # published; its scan requested, not run yet
    b, _ = ready(world, "Other New Thing", al_id=8)
    world.run(export)
    assert stages(world, b) == [("publish", "ready")] * 3, "a scan is awaited: not published"
    world.kavita.run_scans()
    world.run(export)
    assert len(files(world, "Other New Thing")) == 3, "published once the wait ended"


@budget(load="db", seconds=20)
def test_a_chapter_on_its_way_holds_the_series_and_a_waiting_one_does_not(world):
    _sid, _ = ready(world)
    world.rows("UPDATE chapter SET stage = 'convert' WHERE id = (SELECT min(id) FROM chapter) RETURNING id")
    world.run(export)
    assert not (world.library / "Brand New Thing").exists(), "a chapter still at convert holds the whole series"
    world.rows(
        "UPDATE chapter SET status = 'waiting', waiting_on = 'junk_decision' WHERE stage = 'convert' RETURNING id"
    )
    world.run(export)
    assert len(files(world, "Brand New Thing")) == 2, "a waiting chapter does not hold it"


@budget(load="db", seconds=30)
def test_a_chapter_finished_after_the_scan_request_publishes_once_the_id_is_found(world):
    _sid, manga = ready(world)
    world.run(export)
    world.download(manga, "Chapter 4", data=cbz(seed="4", pages=4))
    world.run(intake)
    world.run(process)
    world.run(export)
    assert "Ch.4.cbz" not in files(world, "Brand New Thing"), "the batch's scan is requested: it waits for the id"
    world.kavita.run_scans()
    world.run(export)  # confirm finds the id after this run's publish loop
    world.run(export)
    assert "Ch.4.cbz" in files(world, "Brand New Thing"), "published as for any series"


@budget(load="db", seconds=20)
def test_a_crash_leftover_folder_is_used_and_a_foreign_file_holds_the_series(world, tmp_path):
    sid, _ = ready(world)
    folder = world.library / "Brand New Thing"
    folder.mkdir()
    (folder / "Thumbs.db").write_bytes(b"x")
    # export's comicinfo only (the files named), then a crash after 1 rename
    only_comicinfo = SimpleNamespace(
        __name__="manga_pipeline.export", work=lambda ctx: workflow.drain(ctx, {"comicinfo": comicinfo.run})
    )
    world.run(only_comicinfo)
    (staged, target), *_ = world.rows("SELECT staged_path, library_path FROM chapter ORDER BY id")
    config.to_local(world.cfg, staged).replace(folder / target.rpartition("/")[2])  # renamed, then a crash
    world.run(export)
    assert files(world, "Brand New Thing") == ["Ch.1.cbz", "Ch.2.cbz", "Ch.3.cbz", "Thumbs.db"], "used; finished"
    assert world.rows("SELECT status FROM series WHERE id = %s", sid) == [("ok",)]

    other = World(tmp_path / "other")
    osid, _ = ready(other)
    (other.library / "Brand New Thing").mkdir()
    (other.library / "Brand New Thing" / "Stray.cbz").write_bytes(cbz(seed="stray"))
    other.run(export)
    ((status, reason),) = other.rows("SELECT status, status_reason FROM series WHERE id = %s", osid)
    assert status == "needs_human", "a file it did not publish"
    assert "Stray.cbz" in reason, "a file it did not publish"


@budget(load="db", seconds=30)
def test_intake_between_publish_and_attach_makes_no_second_row(world):
    sid, _ = ready(world)
    world.run(export)
    world.kavita.run_scans()
    second = world.suwayomi.add_manga("Brand New Thing", world.suwayomi.add_source("Other"))
    world.download(second, "Chapter 9", data=cbz(seed="9", pages=4))
    world.run(intake)
    assert world.rows("SELECT count(*) FROM series") == [(1,)], "discovery and sync find the published series"
    assert kavita_id(world, sid) is not None
    assert world.rows("SELECT series_id FROM suwayomi_manga WHERE manga_id = %s", second) == [(sid,)]


@budget(load="db", seconds=20)
def test_a_publish_killed_after_its_rename_attaches_at_the_next_intake_once_kavita_lists_it(world):
    sid, _ = ready(world)
    only_comicinfo = SimpleNamespace(
        __name__="manga_pipeline.export", work=lambda ctx: workflow.drain(ctx, {"comicinfo": comicinfo.run})
    )
    world.run(only_comicinfo)
    (staged, target), *_ = world.rows("SELECT staged_path, library_path FROM chapter ORDER BY id")
    folder = world.library / "Brand New Thing"
    folder.mkdir()
    config.to_local(world.cfg, staged).replace(folder / target.rpartition("/")[2])  # renamed in, then killed
    world.kavita.add_series("Brand New Thing", "Brand New Thing", LIBRARY_ID)  # Kavita's own scan found the file
    world.run(intake)
    assert world.rows("SELECT id FROM series") == [(sid,)], "the listed series is the admitted one: no second row"
    assert kavita_id(world, sid) is not None, "attached with no published_file row yet"


@budget(load="db", seconds=30)
def test_an_attached_new_series_kavita_recreates_keeps_its_row_through_its_comicinfo_name(world):
    sid, _ = ready(world, english="Shiny Title")
    world.run(export)
    world.kavita.run_scans()
    world.run(export)
    old = kavita_id(world, sid)
    assert old is not None, "set up: attached"
    del world.kavita.series_rows[old]
    new = world.kavita.add_series("Shiny Title", "Shiny Title", LIBRARY_ID)  # Kavita recreated it under a new id
    world.run(intake)
    assert world.rows("SELECT id, name, kavita_series_id FROM series") == [(sid, "Brand New Thing", new)], (
        "matched by its ComicInfo name (not its row name) and its folder: the row takes the new id"
    )


@budget(load="db", seconds=30)
def test_after_the_rescan_window_a_published_series_still_waits_for_its_id(world):
    sid, manga = ready(world)
    world.run(export)  # published; its scan requested and never run
    world.download(manga, "Chapter 4", data=cbz(seed="4", pages=4))
    world.run(intake)
    world.run(process)
    world.rows("UPDATE meta SET value = '2000-01-01T00:00:00+00:00' WHERE key = %s RETURNING key", export.REQUESTED_AT)
    world.run(export)  # the scan is no longer awaited: only the closed batch keeps Ch.4 out
    assert "Ch.4.cbz" not in files(world, "Brand New Thing"), "nothing more until the id is found"
    assert ("publish", "ready") in stages(world, sid)


@budget(load="db", seconds=30)
def test_a_listed_series_of_its_name_in_another_folder_is_neither_attached_nor_a_row(world):
    sid, _ = ready(world)
    world.run(export)
    world.series_without_record("Brand New Thing", folder="Elsewhere")
    world.run(intake)
    assert world.rows("SELECT id, kavita_series_id FROM series") == [(sid, None)], "folder decides; no second row"
    second = world.suwayomi.add_manga("Brand New Thing", world.suwayomi.add_source("Other"))
    world.download(second, "Chapter 9", data=cbz(seed="9", pages=4))
    world.run(intake)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series WHERE id <> %s", sid)
    assert status == "needs_human", (
        "a manga whose strict hit a published new series is named as is held, not a second Kavita series"
    )
    assert "admitted new series not found in Kavita yet" in reason, (
        "a manga whose strict hit a published new series is named as is held, not a second Kavita series"
    )


@budget(load="db", seconds=30)
def test_a_held_published_series_keeps_its_name_and_is_attached(world):
    sid, _ = ready(world)
    world.run(export)
    world.kavita.run_scans()
    world.rows("UPDATE series SET status = 'needs_human', status_reason = 'ids disagree' RETURNING id")
    world.run(intake)
    assert world.rows("SELECT count(*) FROM series") == [(1,)], "a needs_human series still owns its Kavita series"
    assert kavita_id(world, sid) is not None, "and is attached"


@budget(load="db", seconds=20)
def test_confirm_of_a_series_kavita_does_not_list_yet_waits_and_lists_the_library_once(world, monkeypatch):
    sid, _ = ready(world)
    world.run(export)
    calls = []
    listing = world.kavita.series_in_library
    monkeypatch.setattr(world.kavita, "series_in_library", lambda lib: calls.append(lib) or listing(lib))
    world.run(export)
    world.run(export)  # end_waits' wait_over, then confirm: still not listed
    assert stages(world, sid) == [("confirm", "waiting")] * 3, "waits on kavita_scan"
    assert calls == [LIBRARY_ID, LIBRARY_ID], "1 listing per run, not 1 per chapter"
