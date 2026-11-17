"""Downloads Suwayomi did not make, placed by hand (SPEC "Suwayomi", "Series identification", "Bad downloads"):
taken once settled, matched by their folder, and re-downloaded from ranked sources when bad.
"""

import errno
import json
from contextlib import closing

import pytest

from manga_pipeline import archive, cli, config, db, discovery, identify, intake, redownload, workflow

from tests.builders import settle
from tests.timing import budget
from tests.world import World, cbz, comicinfo

BAD = b"PK\x03\x04 truncated download"
OTHER = "Other (EN)"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, sources={"ranking": [OTHER], "download_timeout_hours": 24})


def place(world, folder, name, data=None):
    """A CBZ put into <downloads>/mangas/<folder>/ by hand, settled."""
    path = world.mangas / folder / f"{name}.cbz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cbz(seed=name) if data is None else data)
    return settle(path)


def scan(world):
    with closing(db.connect(world.cfg)) as conn:
        discovery.scan(world.context(conn, "intake"))


def link(world, kavita_id, folder):
    with closing(db.connect(world.cfg)) as conn:
        return identify.link(
            world.cfg, conn, world.kavita, kavita_id, identify.link_subject(world.cfg, world.suwayomi, folder=folder)
        )


def chapters(world):
    return world.rows("SELECT series_id, download_path, suwayomi_chapter_id, source FROM chapter ORDER BY id")


@budget(load="db")
def test_a_file_no_chapter_rebuilds_to_is_a_hand_placed_chapter_of_the_mangas_series(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 1")
    path = place(world, "Src (EN)/Solo Leveling", "Chapter 2 [extra]")
    scan(world)
    assert chapters(world)[1] == (1, config.to_stored(world.cfg, path), None, None), (
        "taken into the manga's series with no Suwayomi chapter and no source"
    )
    assert world.rows("SELECT count(*) FROM unmatched_download") == [(0,)]


@budget(load="db")
def test_a_hand_placed_folder_is_matched_by_its_title_folder_and_the_match_stored(world):
    world.kavita_series("Solo Leveling")
    path = place(world, "Local/Solo Leveling", "Chapter 1")
    scan(world)
    assert chapters(world) == [(1, config.to_stored(world.cfg, path), None, None)]
    assert world.rows("SELECT folder, series_id FROM download_folder") == [("Local/Solo Leveling", 1)]
    place(world, "Local/Solo Leveling", "Chapter 2")
    world.kavita.series_rows = {}  # the stored link decides now, not the name
    scan(world)
    assert len(chapters(world)) == 2, "the stored link takes the folder's next file"


@budget(load="db")
def test_a_folder_name_with_no_hit_is_matched_by_the_comicinfo_series_its_files_agree_on(world):
    world.kavita_series("Solo Leveling")
    for n in (1, 2):
        place(world, "Local/SL export", f"Chapter {n}", cbz(seed=str(n), comicinfo=comicinfo(Series="Solo Leveling")))
    place(world, "Local/SL export", "Chapter 3", BAD)  # unreadable: says nothing
    scan(world)
    assert world.rows("SELECT name FROM series") == [("Solo Leveling",)]
    assert len(chapters(world)) == 3, "every file of the folder is taken"


@budget(load="db")
def test_files_that_disagree_on_their_comicinfo_series_leave_the_folder_a_new_series(world):
    world.kavita_series("Solo Leveling")
    place(world, "Local/SL export", "Chapter 1", cbz(seed="1", comicinfo=comicinfo(Series="Solo Leveling")))
    place(world, "Local/SL export", "Chapter 2", cbz(seed="2", comicinfo=comicinfo(Series="Other")))
    scan(world)
    assert world.rows("SELECT count(*) FROM chapter") == [(0,)]
    reasons = {why for (why,) in world.rows("SELECT reason FROM unmatched_download")}
    assert reasons == {"new series, metadata not fetched yet"}
    assert world.rows("SELECT name, alt_name FROM series WHERE origin = 'new'") == [("SL export", None)], (
        "files that disagree give no ComicInfo Series name"
    )


@budget(load="db")
def test_a_near_hit_folder_is_held_and_series_link_folder_settles_it(world):
    kavita_id = world.kavita_series("Kaiju No. 8!")
    place(world, "Local/Kaiju No. 8", "Chapter 1")
    scan(world)
    ((series_id, status_, reason),) = world.rows("SELECT id, status, status_reason FROM series")
    assert status_ == "needs_human"
    assert 'series link --folder "Local/Kaiju No. 8" KAVITA_SERIES_ID' in reason
    assert link(world, kavita_id, "Local/Kaiju No. 8") == series_id, "the folder's held row is the one linked"
    with closing(db.connect(world.cfg)) as conn:
        workflow.retry_series(conn, series_id)
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")]


@pytest.mark.parametrize("folder", ["Local", "Local/Nothing here", "Local/../x", "Local\\Solo Leveling", "/Local/a"])
@budget(load="db")
def test_series_link_folder_refuses_what_is_not_a_download_folder(world, folder):
    kavita_id = world.kavita_series("Solo Leveling")
    place(world, "Local/Solo Leveling", "Chapter 1")
    with pytest.raises(workflow.Refused):
        link(world, kavita_id, folder)


@budget(load="db")
def test_a_bad_hand_placed_chapter_is_redownloaded_from_a_ranked_source_checked_against_the_series_manga(world):
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    for n in [*range(1, 12), *range(13, 21)]:  # Suwayomi lists every chapter but 12
        world.suwayomi.add_chapter(manga, f"Chapter {n}")
    world.download(manga, "Chapter 1")  # links the manga to the series
    other = world.suwayomi.add_manga("Solo Leveling", world.suwayomi.add_source("Other"), in_library=False)
    other_chapters = {n: world.suwayomi.add_chapter(other, f"Ch. {n}") for n in range(1, 21)}
    path = place(world, "Src (EN)/Solo Leveling", "Chapter 12", BAD)
    world.run(intake)
    (text,) = [t for (t,) in world.rows("SELECT redownload FROM chapter WHERE redownload IS NOT NULL")]
    state = json.loads(text)
    assert state["expected"]["chapter_id"] == other_chapters[12], "Ch. 12 of the ranked source"
    assert not path.exists(), "the rejected copy went the retention way"
    assert [w for w in world.suwayomi.writes if w[0] == "delete_downloaded"] == [], "Suwayomi knows nothing of it"


@budget(load="db")
def test_a_bad_hand_placed_chapter_of_a_series_with_no_suwayomi_manga_needs_a_human_at_once(world):
    world.kavita_series("Solo Leveling")
    place(world, "Local/Solo Leveling", "Chapter 12", BAD)
    world.run(intake)
    ((stage, status_, error),) = world.rows("SELECT stage, status, last_error FROM chapter")
    assert (stage, status_) == ("validate", "needs_human")
    assert "no Suwayomi manga to check numbering against" in error
    assert [w for w in world.suwayomi.writes if w[0] in ("search_source", "delete_downloaded")] == [], (
        "no source searched, nothing deleted through Suwayomi"
    )
    with closing(db.connect(world.cfg)) as conn:
        workflow.retry_chapter(conn, 1)
    world.run(intake)
    assert world.rows("SELECT status FROM chapter") == [("needs_human",)], "retry never asks Suwayomi for chapter None"


@budget(load="db")
def test_series_link_folder_refuses_another_spelling_and_a_folder_a_library_manga_maps_to(world):
    kavita_id = world.kavita_series("Solo Leveling")
    place(world, "Local/Solo Leveling", "Chapter 1")
    with pytest.raises(workflow.Refused, match="names are exact"):
        link(world, kavita_id, "local/solo leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 1")
    with pytest.raises(workflow.Refused, match="a library manga maps to"):
        link(world, kavita_id, "Src (EN)/Solo Leveling")


@budget(load="pure")
def test_series_link_parses_a_manga_or_a_folder_but_not_both():
    args = cli.parser().parse_args(["series", "link", "--folder", "Local/Solo Leveling", "7"])
    assert (args.manga_id, args.folder, args.kavita_series_id) == (None, "Local/Solo Leveling", 7)
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["series", "link", "3", "--folder", "Local/Solo Leveling", "7"])


@budget(load="db")
def test_a_held_folder_linked_to_a_series_a_manga_already_has_is_merged_into_its_row(world):
    kavita_id = world.kavita_series("Kaiju No. 8!")
    world.download(world.manga("Kaiju No. 8!"), "Chapter 1")  # strict hit
    place(world, "Local/Kaiju No. 8", "Chapter 2")  # near hit: held
    scan(world)
    ((kept,),) = world.rows("SELECT id FROM series WHERE kavita_series_id IS NOT NULL")
    assert link(world, kavita_id, "Local/Kaiju No. 8") == kept
    assert world.rows("SELECT id FROM series") == [(kept,)], "the held row is merged away"
    assert world.rows("SELECT folder, series_id FROM download_folder") == [("Local/Kaiju No. 8", kept)]
    assert world.rows("SELECT DISTINCT series_id FROM chapter") == [(kept,)]


@budget(load="db")
def test_a_file_the_comicinfo_read_cannot_open_says_nothing_but_a_failing_share_ends_the_scan(world, monkeypatch):
    world.kavita_series("Solo Leveling")
    place(world, "Local/SL export", "Chapter 1", cbz(seed="1", comicinfo=comicinfo(Series="Solo Leveling")))
    locked = place(world, "Local/SL export", "Chapter 2")
    read = archive.read_comicinfo

    def held_open(path):
        if path == locked:
            raise PermissionError(errno.EACCES, "in use", str(path))
        return read(path)

    monkeypatch.setattr(archive, "read_comicinfo", held_open)
    scan(world)
    assert world.rows("SELECT name FROM series") == [("Solo Leveling",)], "the readable file names the series"

    def share_gone(path):
        raise OSError(errno.EIO, "I/O error", str(path))

    monkeypatch.setattr(archive, "read_comicinfo", share_gone)
    place(world, "Local/Other export", "Chapter 1")
    with pytest.raises(OSError, match="I/O error"):
        scan(world)


@budget(load="db")
def test_a_hand_placed_chapter_checks_numbering_against_the_best_ranked_linked_manga(world):
    world.kavita_series("Solo Leveling")
    unranked = world.manga("Solo Leveling")  # lowest id, unranked source
    world.download(unranked, "Chapter 1")
    world.source = world.suwayomi.add_source("Ranked")
    ranked = world.manga("Solo Leveling")
    world.download(ranked, "Chapter 2")
    scan(world)
    world.cfg["sources"]["ranking"] = ["Ranked (EN)"]
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "intake")
        assert redownload.reference_manga(ctx, 1) == ranked, "the ranked source's manga, not the lowest id"
        del world.suwayomi.manga_rows[ranked]
        assert redownload.reference_manga(ctx, 1) == unranked, "a manga Suwayomi no longer holds is passed over"
