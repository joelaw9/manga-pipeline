"""Discovery, series identification and the discover stage (SPEC "Suwayomi", "Series identification", "Bad
downloads")."""

from contextlib import closing

import pytest

from manga_pipeline import archive, config, db, discovery, intake, workflow
from manga_pipeline.stages import discover

from tests.timing import budget
from tests.world import World, cbz, comicinfo


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def scan(world):
    with closing(db.connect(world.cfg)) as conn:
        discovery.scan(world.context(conn, "intake"))


def run_stage(world, chapter_id):
    """Claim the chapter at discover and run the discover stage on it; returns the outcome."""
    with closing(db.connect(world.cfg)) as conn:
        ctx = world.context(conn, "intake")
        row = workflow.claim(ctx, ("discover",))
        assert row["id"] == chapter_id
        return workflow.work(ctx, row, discover.run)


@budget(load="db")
def test_a_complete_download_of_a_kavita_series_becomes_a_chapter_of_it(world):
    kavita_id = world.kavita_series("Solo Leveling", localized="Na Honjaman Level Up")
    manga = world.manga("Solo Leveling")
    chapter_id, path = world.download(manga, "Chapter 2", scanlator="Grp")
    scan(world)
    series = world.rows(
        "SELECT name, kavita_series_id, kavita_library_id, folder, origin, status, localized_name FROM series"
    )
    assert series == [
        ("Solo Leveling", kavita_id, 4, str(world.library / "Solo Leveling"), "kavita", "ok", "Na Honjaman Level Up")
    ], "a strict name hit stores the Kavita series with its folder (the folder of its files)"
    assert world.rows("SELECT download_path, suwayomi_chapter_id, stage, status FROM chapter") == [
        (str(path), chapter_id, "discover", "ready")
    ]
    assert world.rows("SELECT manga_id FROM suwayomi_manga") == [(manga,)], "the match is stored"


@budget(load="db")
def test_an_unflagged_download_is_skipped_without_a_word(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2", flagged=False)
    scan(world)
    assert world.rows("SELECT count(*) FROM chapter") == [(0,)], "Suwayomi is still writing it"
    assert world.rows("SELECT count(*) FROM unmatched_download") == [(0,)], "a download in progress is not listed"


@budget(load="db")
def test_a_title_differing_by_a_kept_mark_is_a_near_hit_needing_a_human_not_a_new_series(world):
    world.kavita_series("Kaiju No. 8!")
    manga = world.manga("Kaiju No. 8")
    world.download(manga, "Chapter 1")
    scan(world)
    rows = world.rows("SELECT name, status, status_reason, kavita_series_id FROM series")
    assert len(rows) == 1 and rows[0][1] == "needs_human" and rows[0][3] is None, "a near hit is never matched"
    assert "nearly matches: Kaiju No. 8!" in rows[0][2] and f"series link {manga}" in rows[0][2]
    assert world.rows("SELECT status FROM chapter") == [("ready",)], "its chapters are recorded and held"


@budget(load="db")
def test_2_strict_hits_need_a_human(world):
    world.kavita_series("Tower", folder="A/Tower")
    world.kavita_series("TOWER!", folder="B/Tower", original="Tower")
    world.download(world.manga("Tower"), "Chapter 1")
    scan(world)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human" and "matches 2 or more Kavita series" in reason


@budget(load="db")
def test_a_title_no_kavita_series_matches_is_a_new_series_listed_and_not_taken(world):
    world.kavita_series("Solo Leveling")
    _, path = world.download(world.manga("Brand New Thing"), "Chapter 1")
    scan(world)
    assert world.rows("SELECT count(*) FROM series") == [(0,)], "a new series is not processed yet"
    ((listed, reason),) = world.rows("SELECT path, reason FROM unmatched_download")
    assert listed == str(path) and reason.startswith("new series")


@budget(load="db")
def test_2_sources_of_1_kavita_series_share_its_series_row(world):
    world.kavita_series("Solo Leveling")
    first = world.manga("Solo Leveling")
    world.source = world.suwayomi.add_source("Other")
    second = world.manga("Solo Leveling")
    world.download(first, "Chapter 2")
    world.download(second, "Chapter 3")
    scan(world)
    assert world.rows("SELECT count(*) FROM series") == [(1,)]
    assert world.rows("SELECT count(DISTINCT series_id), count(*) FROM suwayomi_manga") == [(1, 2)]


@budget(load="db")
def test_files_that_cannot_be_matched_are_listed_with_their_first_sighting_kept(world):
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    stray = world.mangas / "Src (EN)" / "Solo Leveling" / "Not a chapter.cbz"
    orphan = world.mangas / "Nowhere (EN)" / "Ghost" / "Chapter 1.cbz"
    for path in (stray, orphan):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cbz())
    world.download(manga, "Chapter 2")
    scan(world)
    first = dict(world.rows("SELECT path, first_seen FROM unmatched_download"))
    reasons = dict(world.rows("SELECT path, reason FROM unmatched_download"))
    assert "rebuilds to this file name" in reasons[str(stray)]
    assert reasons[str(orphan)] == "no library manga maps to its folder"
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE unmatched_download SET first_seen = '2000-01-01'")
    scan(world)
    assert set(dict(world.rows("SELECT path, first_seen FROM unmatched_download")).values()) == {"2000-01-01"}, (
        "a download still listed keeps when it was first seen"
    )
    assert set(first) == set(reasons)
    orphan.unlink()
    scan(world)
    assert str(orphan) not in dict(world.rows("SELECT path, reason FROM unmatched_download")), "a gone file leaves"


@budget(load="db")
def test_the_stored_library_of_a_known_series_decides_its_scope(world):
    world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Leveling")
    world.download(manga, "Chapter 2")
    scan(world)
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE series SET kavita_library_id = 9")
        conn.execute("DELETE FROM chapter")
    scan(world)
    assert world.rows("SELECT count(*) FROM chapter") == [(0,)], "a series of another library is out of scope"


@budget(load="db")
def test_series_only_excludes_a_match_without_writing_or_listing_it(tmp_path):
    world = World(tmp_path, series={"only": ["Something Else"]})
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    scan(world)
    assert world.rows("SELECT count(*) FROM series") == [(0,)]
    assert world.rows("SELECT count(*) FROM unmatched_download") == [(0,)], "out of scope is not unmatched"


@budget(load="db")
def test_a_download_at_a_done_chapters_path_is_a_new_chapter(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    scan(world)
    scan(world)
    assert world.rows("SELECT count(*) FROM chapter") == [(1,)], "an open chapter holds its path"
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE chapter SET status = 'done'")
    scan(world)
    assert world.rows("SELECT count(*) FROM chapter") == [(2,)], "the same file downloaded again is looked at again"


@budget(load="db")
def test_a_download_an_export_confirms_during_the_listing_is_not_taken_again(world, monkeypatch):
    world.kavita_series("Solo Leveling")
    _, path = world.download(world.manga("Solo Leveling"), "Chapter 2")
    scan(world)
    record = discovery._record

    def confirmed_meanwhile(ctx, found, unmatched):  # after the listing: confirm moves the file, then records done
        path.unlink()
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE chapter SET status = 'done'")
        record(ctx, found, unmatched)

    monkeypatch.setattr(discovery, "_record", confirmed_meanwhile)
    scan(world)
    assert world.rows("SELECT count(*) FROM chapter") == [(1,)], (
        "a listed file gone once its chapter is done is no new download"
    )


# --- the discover stage


def discovered(world, name, data=None, scanlator=None):
    world.kavita_series("Solo Leveling")
    _, path = world.download(world.manga("Solo Leveling"), name, scanlator=scanlator, data=data)
    scan(world)
    ((chapter_id,),) = world.rows("SELECT id FROM chapter")
    return chapter_id, path


@budget(load="db")
@pytest.mark.parametrize("season", ["1", "2"])
def test_a_season_is_part_of_the_identity(world, season):
    chapter_id, _ = discovered(world, f"S{season} Episode 5")
    assert isinstance(run_stage(world, chapter_id), workflow.Done)
    ((identity, digest),) = world.rows("SELECT identity, content_digest FROM chapter")
    assert identity == f'["", "{season}", "", "5", ""]', "S2 Episode 5 is not S1 Episode 5"
    assert len(digest) == 64


@budget(load="db")
def test_comicinfo_number_numbers_a_chapter_whose_name_has_none(world):
    chapter_id, _ = discovered(world, "Oneshot", data=cbz(comicinfo=comicinfo(Number="7")))
    assert isinstance(run_stage(world, chapter_id), workflow.Done)
    assert world.rows("SELECT identity FROM chapter") == [('["", null, "", "7", "oneshot"]',)]


@budget(load="db")
def test_a_chapter_with_no_number_needs_a_human_at_once(world):
    chapter_id, _ = discovered(world, "Oneshot")
    run_stage(world, chapter_id)
    assert world.rows("SELECT status, attempts FROM chapter") == [("needs_human", 0)]


@budget(load="db")
def test_the_same_pages_as_a_handled_chapter_are_put_aside(world):
    chapter_id, path = discovered(world, "Chapter 2", data=cbz(seed="x"))
    with closing(db.connect(world.cfg)) as conn:
        digest = archive.pages_digest(path)
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)
                VALUES (1, 'other', 'duplicate', ?, 'now')""",
                (digest,),
            )
    outcome = run_stage(world, chapter_id)
    assert isinstance(outcome, workflow.Dropped), "whatever its outcome or identity, the same pages are not redone"
    assert world.rows("SELECT stage, status FROM chapter") == [("discover", "done")]
    kept = config.abs_path(world.cfg["paths"]["retention"]) / "Src (EN)" / "Solo Leveling" / path.name
    assert not path.exists() and kept.exists(), "put aside the retention way"


@budget(load="db")
def test_a_download_gone_before_its_stage_is_dropped(world):
    chapter_id, path = discovered(world, "Chapter 2")
    path.unlink()
    assert isinstance(run_stage(world, chapter_id), workflow.Dropped)


@budget(load="db")
def test_intake_runs_discovery_and_the_stage(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")], "discover, then validate (stub)"
