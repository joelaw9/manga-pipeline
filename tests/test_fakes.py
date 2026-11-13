"""The fakes later phases test against behave like the services where it matters (SPEC "Tests", "Services"): the
download flag and file names of Suwayomi, and Kavita's scans (name matching, deleted and recreated series, stale
sizes, folderPath)."""

import pytest

from manga_pipeline.clients import kavita, suwayomi

from tests.fakes.kavita import FakeKavita
from tests.fakes.suwayomi import FakeSuwayomi, cbz
from tests.timing import budget

ROOT = "/kavita/library"


def comicinfo(series, localized=None):
    extra = f"<LocalizedSeries>{localized}</LocalizedSeries>" if localized else ""
    return f"<?xml version='1.0'?><ComicInfo><Series>{series}</Series>{extra}</ComicInfo>"


# --- FakeSuwayomi


@pytest.fixture
def suw(tmp_path):
    fake = FakeSuwayomi(tmp_path / "mangas")
    fake.source = fake.add_source("MangaDex")
    fake.manga_id = fake.add_manga("Re:Zero", fake.source)
    fake.c1 = fake.add_chapter(fake.manga_id, "Ch.1: Start", "Group")
    fake.c2 = fake.add_chapter(fake.manga_id, "Ch.2")
    return fake


@budget(load="fs")
def test_a_finished_download_lies_where_the_name_helpers_say_and_is_flagged(suw, tmp_path):
    suw.enqueue([suw.c1, suw.c2])
    assert [e["state"] for e in suw.download_queue()["queue"]] == ["QUEUED", "QUEUED"]
    suw.finish_downloads()
    folder = tmp_path / "mangas" / "MangaDex (EN)" / "Re_Zero"
    assert sorted(p.name for p in folder.iterdir()) == ["Ch.2.cbz", "Group_Ch.1_ Start.cbz"]
    assert [c["isDownloaded"] for c in suw.chapters(suw.manga_id)] == [True, True]
    assert suw.download_queue()["queue"] == [], "a finished download leaves the queue"


@budget(load="fs")
def test_a_download_being_written_is_visible_but_not_flagged(suw):
    suw.enqueue([suw.c1])
    suw.begin_download(suw.c1)
    assert suw.path(suw.c1).exists() and not suw.chapters(suw.manga_id)[0]["isDownloaded"]
    assert suw.download_queue()["queue"][0]["state"] == "DOWNLOADING"


@budget(load="fs")
def test_the_flag_stays_when_the_file_is_removed_directly_and_enqueue_downloads_again(suw):
    suw.enqueue([suw.c1])
    suw.finish_downloads()
    suw.enqueue([suw.c1])
    assert suw.download_queue()["queue"] == [], "flagged with its file in place: enqueue does nothing"
    suw.path(suw.c1).unlink()
    assert suw.chapters(suw.manga_id)[0]["isDownloaded"], "only the API changes the flag"
    suw.enqueue([suw.c1])
    suw.enqueue([suw.c1])
    assert len(suw.download_queue()["queue"]) == 1, "flagged with its file gone: queued again, once"


@budget(load="fs")
def test_delete_downloaded_removes_the_file_its_emptied_folders_and_the_flag(suw, tmp_path):
    suw.enqueue([suw.c1])
    suw.finish_downloads()
    suw.delete_downloaded([suw.c1, suw.c2])
    assert not (tmp_path / "mangas" / "MangaDex (EN)").exists()
    assert [c["isDownloaded"] for c in suw.chapters(suw.manga_id)] == [False, False]


@budget(load="fs")
def test_a_failed_download_stays_queued_as_error(suw):
    suw.enqueue([suw.c1])
    suw.fail_download(suw.c1)
    suw.finish_downloads()
    entry = suw.download_queue()["queue"][0]
    assert (entry["state"], entry["tries"], entry["chapter"]) == ("ERROR", 3, {"id": suw.c1, "mangaId": suw.manga_id})
    assert not suw.path(suw.c1).exists()


@budget(load="fs")
def test_reads_have_the_clients_shapes(suw):
    other = suw.add_manga("Elsewhere", suw.source, in_library=False)
    assert suw.library_size() == 1 and [m["id"] for m in suw.library_manga()] == [suw.manga_id]
    manga = suw.manga(suw.manga_id)
    assert set(manga) == {"id", "title", "status", "url", "inLibrary", "sourceId", "source"}
    assert manga["source"] == {"id": suw.source, "name": "MangaDex", "lang": "en", "displayName": "MangaDex (EN)"}
    assert set(suw.chapters(suw.manga_id)[0]) == set(suwayomi.CHAPTER.split()), "the fields the real query asks for"
    assert suw.manga(other)["inLibrary"] is False and suw.manga(12345) is None
    assert suw.sources() == [manga["source"]]


@budget(load="fs")
def test_source_calls_find_manga_outside_the_library_and_fail_when_the_source_is_down(suw):
    other = suw.add_manga("Re:Zero (Official)", suw.add_source("Weeb Central"), in_library=False)
    chapter = suw.add_chapter(other, "Chapter 1")
    source = suw.manga(other)["sourceId"]
    assert [m["id"] for m in suw.search_source(source, "re:zero")] == [other]
    assert [c["id"] for c in suw.fetch_manga_and_chapters(other)["chapters"]] == [chapter]
    suw.unavailable.add(source)
    with pytest.raises(suwayomi.SourceUnavailable):
        suw.search_source(source, "re:zero")
    with pytest.raises(suwayomi.SourceUnavailable):
        suw.fetch_manga_and_chapters(other)


@budget(load="fs")
def test_a_chapter_new_on_the_source_is_listed_only_after_a_fetch(suw):
    new = suw.add_chapter(suw.manga_id, "Ch.3", listed=False)
    assert [c["id"] for c in suw.chapters(suw.manga_id)] == [suw.c1, suw.c2], "Suwayomi has not fetched the list"
    fetched = suw.fetch_manga_and_chapters(suw.manga_id)["chapters"]
    assert [c["id"] for c in fetched] == [suw.c1, suw.c2, new] == [c["id"] for c in suw.chapters(suw.manga_id)]
    assert fetched[-1]["sourceOrder"] == 3


@budget(load="fs")
def test_a_call_the_fake_does_not_answer_fails_loudly(suw):
    with pytest.raises(AssertionError, match="unexpected request"):
        suw.graphql("{ __typename }")


# --- FakeKavita


@pytest.fixture
def kav(tmp_path):
    return FakeKavita({4: ROOT, 5: "/kavita/books"}, {"/kavita": str(tmp_path)})


def put(tmp_path, relative, series=None, localized=None, pages=3):
    """A CBZ at <tmp>/library/<relative>, with a ComicInfo Series when given."""
    path = tmp_path / "library" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cbz(pages, comicinfo(series, localized) if series else None))
    return path


def names(kav, series_id):
    return sorted(f["path"].name for f in kav.series_files(series_id))


@budget(load="fs")
def test_a_held_series_reports_its_files_and_the_top_folder_as_folder_path(kav, tmp_path):
    put(tmp_path, "Local/Alpha/Ch.1.cbz", "Alpha", pages=5)
    sid = kav.add_series("Alpha", "Local/Alpha")
    row = kav.series(sid)
    assert (row["folderPath"], row["lowestFolderPath"]) == (f"{ROOT}/Local", f"{ROOT}/Local/Alpha")
    assert kav.series_folder(sid) == tmp_path / "library" / "Local" / "Alpha", "the client's own folder rule runs"
    file = kav.series_files(sid)[0]
    assert (file["path"], file["pages"]) == (tmp_path / "library" / "Local" / "Alpha" / "Ch.1.cbz", 5)
    assert file["bytes"] == file["path"].stat().st_size
    assert [s["id"] for s in kav.series_in_library(4)] == [sid] and kav.series_in_library(5) == []
    assert kav.libraries()[0] == {"id": 4, "name": "Library 4", "folders": [ROOT]}


@budget(load="fs")
def test_series_scan_indexes_a_new_file_that_carries_the_series_name(kav, tmp_path):
    put(tmp_path, "Alpha/Ch.1.cbz", "Alpha")
    sid = kav.add_series("Alpha", "Alpha", localized="Arufa")
    first = kav.series_files(sid)[0]
    put(tmp_path, "Alpha/Ch.2.cbz", "ALPHA?")  # normalises to the series' name
    put(tmp_path, "Alpha/Ch.3.cbz", "Something", localized="Arufa")  # matches by LocalizedSeries
    kav.scan_series(4, sid)
    kav.run_scans()
    assert names(kav, sid) == ["Ch.1.cbz", "Ch.2.cbz", "Ch.3.cbz"]
    assert kav.series_files(sid)[0] == first, "a file already indexed keeps its ids"
    assert kav.writes == [("scan_series", 4, sid)]


@budget(load="fs")
def test_a_requested_scan_changes_nothing_until_kavita_runs_it(kav, tmp_path):
    put(tmp_path, "Alpha/Ch.1.cbz", "Alpha")
    sid = kav.add_series("Alpha", "Alpha")
    put(tmp_path, "Alpha/Ch.2.cbz", "Alpha")
    put(tmp_path, "Beta/Ch.1.cbz", "Beta")
    kav.scan_series(4, sid)
    kav.scan_library(4)
    assert names(kav, sid) == ["Ch.1.cbz"] and len(kav.series_in_library(4)) == 1, "SPEC 'Services': never awaited"
    assert kav.writes == [("scan_series", 4, sid), ("scan_library", 4)]
    kav.run_scans()
    assert names(kav, sid) == ["Ch.1.cbz", "Ch.2.cbz"] and len(kav.series_in_library(4)) == 2
    assert kav.pending == []


@budget(load="fs")
def test_a_file_carrying_another_name_is_not_indexed_into_the_series(kav, tmp_path):
    put(tmp_path, "Alpha/Ch.1.cbz", "Alpha")
    sid = kav.add_series("Alpha", "Alpha")
    put(tmp_path, "Alpha/Ch.2.cbz", "Alpha (Source Title)")
    kav.scan_series(4, sid)
    kav.run_scans()
    assert names(kav, sid) == ["Ch.1.cbz"], "Kavita files it under another series"
    other = [s for s in kav.series_in_library(4) if s["id"] != sid]
    assert [s["name"] for s in other] == ["Alpha (Source Title)"] and names(kav, other[0]["id"]) == ["Ch.2.cbz"]


@budget(load="fs")
def test_a_series_whose_files_all_carry_another_name_is_deleted_and_recreated(kav, tmp_path):
    put(tmp_path, "Alpha/Ch.1.cbz", "Source Title")
    sid = kav.add_series("Alpha", "Alpha")
    kav.scan_series(4, sid)
    kav.run_scans()
    assert kav.series(sid) is None and kav.volumes(sid) == [], "the series id is gone"
    [new] = kav.series_in_library(4)
    assert new["id"] != sid and new["name"] == "Source Title"


@budget(load="fs")
def test_a_file_without_comicinfo_takes_its_folders_name(kav, tmp_path):
    put(tmp_path, "Alpha/Ch.1.cbz")
    sid = kav.add_series("Alpha", "Alpha")
    kav.scan_series(4, sid)
    kav.run_scans()
    assert names(kav, sid) == ["Ch.1.cbz"]


@budget(load="fs")
def test_series_scan_of_a_missing_folder_deletes_the_series(kav, tmp_path):
    path = put(tmp_path, "Alpha/Ch.1.cbz", "Alpha")
    sid = kav.add_series("Alpha", "Alpha")
    path.unlink()
    path.parent.rmdir()
    kav.scan_series(4, sid)
    kav.run_scans()
    assert kav.series(sid) is None


@budget(load="fs")
def test_a_scan_sees_a_replaced_file_unless_sizes_are_stale(kav, tmp_path):
    path = put(tmp_path, "Alpha/Ch.1.cbz", "Alpha", pages=2)
    sid = kav.add_series("Alpha", "Alpha")
    old = kav.series_files(sid)[0]["bytes"]
    put(tmp_path, "Alpha/Ch.1.cbz", "Alpha", pages=9)
    kav.stale_sizes = True
    kav.scan_series(4, sid)
    kav.run_scans()
    assert kav.series_files(sid)[0]["bytes"] == old != path.stat().st_size, "the stale-size hazard"
    kav.stale_sizes = False
    kav.scan_series(4, sid)
    kav.run_scans()
    assert kav.series_files(sid)[0]["bytes"] == path.stat().st_size


@budget(load="fs")
def test_a_broken_archive_is_not_indexed(kav, tmp_path):
    put(tmp_path, "Alpha/Ch.1.cbz", "Alpha")
    sid = kav.add_series("Alpha", "Alpha")
    (tmp_path / "library" / "Alpha" / "Ch.2.cbz").write_bytes(b"not a zip")
    kav.scan_series(4, sid)
    kav.run_scans()
    assert names(kav, sid) == ["Ch.1.cbz"]


@budget(load="fs")
def test_library_scan_adds_new_series_and_keeps_the_ids_of_held_ones(kav, tmp_path):
    put(tmp_path, "Local/Alpha/Ch.1.cbz", "Alpha")
    sid = kav.add_series("Alpha", "Local/Alpha")
    put(tmp_path, "Local/Alpha/Ch.2.cbz", "Alpha")
    put(tmp_path, "Beta/Ch.1.cbz", "Beta")
    put(tmp_path, "Beta/Ch.2.cbz", "Beta")
    kav.scan_library(4)
    kav.run_scans()
    held = {s["name"]: s for s in kav.series_in_library(4)}
    assert sorted(held) == ["Alpha", "Beta"] and held["Alpha"]["id"] == sid
    assert names(kav, sid) == ["Ch.1.cbz", "Ch.2.cbz"] and names(kav, held["Beta"]["id"]) == ["Ch.1.cbz", "Ch.2.cbz"]
    assert held["Beta"]["folderPath"] == f"{ROOT}/Beta" and kav.writes == [("scan_library", 4)]


@budget(load="fs")
def test_library_scan_follows_a_renamed_folder_by_name_and_drops_a_series_without_files(kav, tmp_path):
    path = put(tmp_path, "Old Name/Ch.1.cbz", "Alpha")
    gone = put(tmp_path, "Gone/Ch.1.cbz", "Gone")
    sid, gone_id = kav.add_series("Alpha", "Old Name"), kav.add_series("Gone", "Gone")
    path.parent.rename(tmp_path / "library" / "New Name")
    gone.unlink()
    kav.scan_library(4)
    kav.run_scans()
    assert [s["id"] for s in kav.series_in_library(4)] == [sid], "matched by ComicInfo Series: same id"
    assert kav.series_folder(sid) == tmp_path / "library" / "New Name" and kav.series(gone_id) is None


@budget(load="fs")
def test_series_of_two_libraries_are_kept_apart(kav, tmp_path):
    put(tmp_path, "Alpha/Ch.1.cbz", "Alpha")
    (tmp_path / "books" / "Alpha").mkdir(parents=True)
    (tmp_path / "books" / "Alpha" / "Vol.1.cbz").write_bytes(cbz(2, comicinfo("Alpha")))
    manga, book = kav.add_series("Alpha", "Alpha"), kav.add_series("Alpha", "Alpha", library_id=5)
    assert [s["id"] for s in kav.series_in_library(4)] == [manga] and [s["id"] for s in kav.series_in_library(5)] == [
        book
    ]
    kav.scan_library(5)
    kav.run_scans()
    assert names(kav, manga) == ["Ch.1.cbz"] and names(kav, book) == ["Vol.1.cbz"]


@budget(load="fs")
def test_series_with_files_in_2_folders_has_no_usable_folder(kav, tmp_path):
    put(tmp_path, "Local/Alpha/Ch.1.cbz", "Alpha")
    put(tmp_path, "Weeb/Alpha/Ch.2.cbz", "Alpha")
    sid = kav.add_series("Alpha", "Local/Alpha")
    kav.scan_library(4)
    kav.run_scans()
    assert names(kav, sid) == ["Ch.1.cbz", "Ch.2.cbz"]
    with pytest.raises(kavita.FolderError, match="2 folders"):
        kav.series_folder(sid)
    row = kav.series(sid)
    assert (row["lowestFolderPath"], row["folderPath"]) == (ROOT, ROOT), "the common parent of its files"
    put(tmp_path, "Weeb/Alpha/Ch.3.cbz", "Alpha")
    kav.scan_series(4, sid)
    kav.run_scans()
    assert names(kav, sid) == ["Ch.1.cbz", "Ch.2.cbz", "Ch.3.cbz"], "a series scan covers its lowest folder and below"


@budget(load="fs")
def test_a_kavita_call_the_fake_does_not_answer_fails_loudly(kav):
    with pytest.raises(AssertionError, match="unexpected request"):
        kav._call("GET", "/api/Series/metadata?seriesId=1")
