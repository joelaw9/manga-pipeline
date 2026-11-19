"""Discovery, series identification and the discover stage.

SPEC "Suwayomi", "Series identification", "Bad downloads".
"""

import errno
from contextlib import closing

import pytest

from manga_pipeline import archive, config, db, discovery, intake, workflow
from manga_pipeline.stages import discover

from tests.builders import present, settle
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
        row = present(workflow.claim(ctx, ("discover",)))
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
        (
            "Solo Leveling",
            kavita_id,
            4,
            config.to_stored(world.cfg, world.library / "Solo Leveling"),
            "kavita",
            "ok",
            "Na Honjaman Level Up",
        )
    ], "a strict name hit stores the Kavita series with its folder (the folder of its files)"
    assert world.rows("SELECT download_path, suwayomi_chapter_id, stage, status FROM chapter") == [
        (config.to_stored(world.cfg, path), chapter_id, "discover", "ready")
    ]
    assert world.rows("SELECT manga_id FROM suwayomi_manga") == [(manga,)], "the match is stored"


@budget(load="db")
def test_a_download_is_taken_once_settled_whatever_suwayomi_flags(world):
    world.kavita_series("Solo Leveling")
    _, path = world.download(world.manga("Solo Leveling"), "Chapter 2", settled=False)
    scan(world)
    assert world.rows("SELECT count(*) FROM chapter") == [(0,)], "modified just now: it may still be written"
    assert world.rows("SELECT count(*) FROM unmatched_download") == [(0,)], "a download in progress is not listed"
    settle(path)
    scan(world)
    assert world.rows("SELECT count(*) FROM chapter") == [(1,)], "settled: taken though Suwayomi never flagged it"


@budget(load="db")
def test_a_title_differing_by_a_kept_mark_is_a_near_hit_needing_a_human_not_a_new_series(world):
    world.kavita_series("Kaiju No. 8!")
    manga = world.manga("Kaiju No. 8")
    world.download(manga, "Chapter 1")
    scan(world)
    rows = world.rows("SELECT name, status, status_reason, kavita_series_id FROM series")
    assert len(rows) == 1, "a near hit is never matched"
    assert rows[0][1] == "needs_human", "a near hit is never matched"
    assert rows[0][3] is None, "a near hit is never matched"
    assert "nearly matches: Kaiju No. 8!" in rows[0][2]
    assert f"series link {manga}" in rows[0][2]
    assert world.rows("SELECT status FROM chapter") == [("ready",)], "its chapters are recorded and held"


@budget(load="db")
def test_2_strict_hits_need_a_human(world):
    world.kavita_series("Tower", folder="A/Tower")
    world.kavita_series("TOWER!", folder="B/Tower", original="Tower")
    world.download(world.manga("Tower"), "Chapter 1")
    scan(world)
    ((status, reason),) = world.rows("SELECT status, status_reason FROM series")
    assert status == "needs_human"
    assert "matches 2 or more Kavita series" in reason


@budget(load="db")
def test_a_title_no_kavita_series_matches_is_a_new_series_listed_and_not_taken(world):
    world.kavita_series("Solo Leveling")
    _, path = world.download(world.manga("Brand New Thing"), "Chapter 1")
    scan(world)
    assert world.rows("SELECT name, origin, status, admitted_at FROM series WHERE origin = 'new'") == [
        ("Brand New Thing", "new", "ok", None)
    ], "a pending new series: its row and link, no chapter until it is admitted"
    ((listed, reason),) = world.rows("SELECT path, reason FROM unmatched_download")
    assert listed == config.to_stored(world.cfg, path)
    assert reason == "new series, metadata not fetched yet"


def _unlistable(monkeypatch, *folders, error=(22, "An unexpected network error occurred"), root=None):
    """Make os.scandir fail on these folders as Windows fails on a Samba name it cannot show (WinError 59, errno 22).

    root, if given, lists once and then fails (the share gone while discovery lists it).
    """
    real = discovery.os.scandir
    bad = {str(f) for f in folders}
    root_calls = []

    def scandir(path):
        if str(path) in bad:
            raise OSError(*error, str(path))
        if root is not None and str(path) == str(root):
            root_calls.append(path)
            if len(root_calls) > 1:
                raise OSError(22, "The network name cannot be found", str(path))
        return real(path)

    monkeypatch.setattr(discovery.os, "scandir", scandir)


@budget(load="db")
def test_a_manga_folder_that_cannot_be_listed_is_listed_and_the_rest_is_taken(world, monkeypatch):
    world.kavita_series("Solo Leveling")
    world.kavita_series("Omniscient Reader")
    _, good = world.download(world.manga("Solo Leveling"), "Chapter 2")
    _, bad = world.download(world.manga("Omniscient Reader"), "Chapter 5")
    _unlistable(monkeypatch, bad.parent)
    scan(world)
    assert world.rows("SELECT download_path FROM chapter") == [(config.to_stored(world.cfg, good),)], (
        "the other downloads are still taken"
    )
    ((listed, reason),) = world.rows("SELECT path, reason FROM unmatched_download")
    assert listed == config.to_stored(world.cfg, bad.parent), "the folder is listed"
    assert reason.startswith("its folder cannot be listed"), "the folder is listed"


@budget(load="db")
@pytest.mark.parametrize(
    ("error", "root_fails", "raised"),
    [
        ((22, "An unexpected network error occurred"), True, "The network name cannot be found"),
        ((errno.EIO, "Input/output error"), False, "Input/output error"),
    ],
    ids=["root-gone-after-listing", "share-errno"],
)
def test_a_manga_folder_error_ends_the_scan_when_the_share_is_failing(world, monkeypatch, error, root_fails, raised):
    world.kavita_series("Solo Leveling")
    _, path = world.download(world.manga("Solo Leveling"), "Chapter 2")
    root = path.parent.parent.parent
    _unlistable(monkeypatch, path.parent, error=error, root=root if root_fails else None)
    with pytest.raises(OSError, match=raised):
        scan(world)
    assert world.rows("SELECT count(*) FROM unmatched_download") == [(0,)], "a failing share lists nothing"


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
    world.download(manga, "Chapter 2")
    twin = world.suwayomi.add_chapter(manga, "Chapter 2")  # a second chapter that rebuilds to the same file name
    stray = world.suwayomi.path(twin)
    orphan = world.mangas / "Nowhere (EN)" / "Ghost" / "Chapter 1.cbz"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(cbz())
    settle(orphan)
    scan(world)
    first = dict(world.rows("SELECT path, first_seen FROM unmatched_download"))
    reasons = dict(world.rows("SELECT path, reason FROM unmatched_download"))
    assert "2 chapters of Suwayomi manga" in reasons[config.to_stored(world.cfg, stray)]
    assert reasons[config.to_stored(world.cfg, orphan)].startswith("new series"), "no Kavita series is Ghost"
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE unmatched_download SET first_seen = '2000-01-01'")
    scan(world)
    assert set(dict(world.rows("SELECT path, first_seen FROM unmatched_download")).values()) == {"2000-01-01"}, (
        "a download still listed keeps when it was first seen"
    )
    assert set(first) == set(reasons)
    orphan.unlink()
    scan(world)
    assert config.to_stored(world.cfg, orphan) not in dict(world.rows("SELECT path, reason FROM unmatched_download")), (
        "a gone file leaves"
    )


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
def test_discovery_checks_files_outside_the_write_transaction(world, monkeypatch):
    """A stalled share must not hold the database-wide write lock (SPEC "State": a write transaction is short)."""
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    real_exists = discovery.errors.exists
    in_transaction: list[bool] = []
    with closing(db.connect(world.cfg)) as conn:

        def exists(local):
            in_transaction.append(db.in_transaction(conn))
            return real_exists(local)

        monkeypatch.setattr(discovery.errors, "exists", exists)
        discovery.scan(world.context(conn, "intake"))
    assert in_transaction, "the download's file was checked"
    assert not any(in_transaction), "no file is checked inside a transaction"
    assert world.rows("SELECT count(*) FROM chapter") == [(1,)], "the download is still a new chapter"


@budget(load="db")
def test_a_chapter_records_its_source_by_display_name(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    scan(world)
    assert world.rows("SELECT source FROM chapter") == [("Src (EN)",)], "sources.ranking names sources this way"


@budget(load="db")
def test_paths_open_chapters_hold_or_expect_are_neither_new_chapters_nor_not_taken(world):
    world.kavita_series("Solo Leveling")
    _, held = world.download(world.manga("Solo Leveling"), "Chapter 2")
    scan(world)
    other = world.suwayomi.add_manga("Solo Leveling", world.suwayomi.add_source("Elsewhere"), in_library=False)
    _, expected = world.download(other, "Chapter 2")  # a re-download into a manga outside the library
    _, placed = world.download(other, "Chapter 3")
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute(
            "UPDATE chapter SET redownload = jsonb_build_object('expected',"
            " jsonb_build_object('path', %s::text))::text",
            (config.to_stored(world.cfg, expected),),
        )
    scan(world)
    assert world.rows("SELECT download_path FROM chapter ORDER BY id") == [
        (config.to_stored(world.cfg, held),),
        (config.to_stored(world.cfg, placed),),
    ], "no new chapter for the held or the expected path; another file of that folder is a hand-placed chapter"
    assert world.rows("SELECT count(*) FROM unmatched_download") == [(0,)], "neither is listed as not taken"


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
def test_discover_writes_the_number_its_comicinfo_gives_beside_the_identity(world):
    chapter_id, _ = discovered(world, "Oneshot", data=cbz(comicinfo=comicinfo(Number="12.5")))
    assert world.rows("SELECT number FROM chapter") == [(None,)], "no number before discover settles it"
    assert isinstance(run_stage(world, chapter_id), workflow.Done)
    assert world.rows("SELECT stage, number FROM chapter") == [("validate", 12.5)], "the ComicInfo Number, as a float"


@budget(load="db")
def test_a_chapter_with_no_number_needs_a_human_at_once(world):
    chapter_id, _ = discovered(world, "Oneshot")
    run_stage(world, chapter_id)
    assert world.rows("SELECT status, attempts FROM chapter") == [("needs_human", 0)]


@budget(load="db")
@pytest.mark.parametrize("name", ["Chapter 26.10", "Chapter 26-10", "Chapter 17.50"])
def test_a_number_whose_decimal_part_ends_in_0_needs_a_human_at_once(world, name):
    chapter_id, _ = discovered(world, name)
    run_stage(world, chapter_id)
    ((status, stage, attempts, error),) = world.rows("SELECT status, stage, attempts, last_error FROM chapter")
    assert (status, stage, attempts) == ("needs_human", "discover", 0), "held at discover, at once"
    number = name.split()[1].replace("-", ".")
    whole, part = number.split(".")
    assert error.endswith(
        f"number {number} has no single reading: {number.rstrip('0')}, or part {part} of chapter {whole}"
    )
    ((identity,),) = world.rows("SELECT identity FROM chapter")
    assert f'"{number}"' in identity, "the identity discovery stored is not chapter 26.1's either"


@budget(load="db")
def test_a_number_with_2_readings_from_comicinfo_alone_needs_a_human(world):
    chapter_id, _ = discovered(world, "Oneshot", data=cbz(comicinfo=comicinfo(Number="26.10")))
    run_stage(world, chapter_id)
    ((status, error),) = world.rows("SELECT status, last_error FROM chapter")
    assert status == "needs_human"
    assert "number 26.10 has no single reading" in error


@budget(load="db")
def test_a_number_with_2_readings_whose_pages_were_handled_is_put_aside_not_held(world):
    chapter_id, path = discovered(world, "Chapter 26.10", data=cbz(seed="x"))
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute(
            """INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)
            VALUES (1, 'other', 'dropped', %s, 'now')""",
            (archive.pages_digest(path),),
        )
    assert isinstance(run_stage(world, chapter_id), workflow.Dropped), "the same pages again are never a person's work"
    assert world.rows("SELECT count(*) FROM handled_chapter") == [(1,)], "and no second handled chapter is recorded"


@budget(load="db")
@pytest.mark.parametrize(("name", "number"), [("Chapter 26.1", "26.1"), ("Chapter 26.5", "26.5"), ("Chapter 30", "30")])
def test_other_decimal_and_whole_numbers_pass_discover(world, name, number):
    chapter_id, _ = discovered(world, name)
    assert isinstance(run_stage(world, chapter_id), workflow.Done)
    ((identity,),) = world.rows("SELECT identity FROM chapter")
    assert f'"{number}"' in identity, "a decimal part that does not end in 0 has 1 reading"


@budget(load="db")
def test_the_same_pages_as_a_handled_chapter_are_put_aside(world):
    chapter_id, path = discovered(world, "Chapter 2", data=cbz(seed="x"))
    with closing(db.connect(world.cfg)) as conn:
        digest = archive.pages_digest(path)
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO handled_chapter (series_id, identity, outcome, content_digest, handled_at)
                VALUES (1, 'other', 'duplicate', %s, 'now')""",
                (digest,),
            )
    outcome = run_stage(world, chapter_id)
    assert isinstance(outcome, workflow.Dropped), "whatever its outcome or identity, the same pages are not redone"
    assert world.rows("SELECT stage, status FROM chapter") == [("discover", "done")]
    kept = config.abs_path(world.cfg["paths"]["retention"]) / "Src (EN)" / "Solo Leveling" / path.name
    assert not path.exists(), "put aside the retention way"
    assert kept.exists(), "put aside the retention way"


@budget(load="db")
@pytest.mark.parametrize("data", [b"PK\x03\x04 truncated", b"not a zip at all"])
def test_an_archive_that_cannot_be_read_goes_on_to_validate_without_a_digest(world, data):
    chapter_id, _ = discovered(world, "Chapter 2", data=data)
    assert isinstance(run_stage(world, chapter_id), workflow.Done), "validate judges a broken archive, not discover"
    assert world.rows("SELECT stage, status, identity, content_digest FROM chapter") == [
        ("validate", "ready", '["", null, "", "2", ""]', None)
    ], "its number comes from its name alone"


@budget(load="db")
def test_an_archive_that_cannot_be_read_and_has_no_number_in_its_name_needs_a_human(world):
    chapter_id, _ = discovered(world, "Oneshot", data=b"broken")
    run_stage(world, chapter_id)
    assert world.rows("SELECT status, attempts FROM chapter") == [("needs_human", 0)]


@budget(load="db")
def test_an_unnumbered_notice_is_dropped_put_aside_and_recorded(tmp_path):
    world = World(tmp_path, validate={"non_chapter_names": ["Hiatus Notice"]})
    chapter_id, path = discovered(world, "Hiatus Notice", scanlator="Grp")
    outcome = run_stage(world, chapter_id)
    assert isinstance(outcome, workflow.Dropped), "a notice is not a chapter"
    kept = config.abs_path(world.cfg["paths"]["retention"]) / "Src (EN)" / "Solo Leveling" / path.name
    assert not path.exists(), "removed the retention way"
    assert kept.exists(), "removed the retention way"
    assert world.rows("SELECT outcome, content_digest FROM handled_chapter") == [
        ("dropped", archive.pages_digest(kept))
    ], "recorded as handled, so the same pages arriving again are put aside"


@budget(load="db")
def test_a_numbered_notice_goes_on_to_be_judged_at_junk(tmp_path):
    world = World(tmp_path, validate={"non_chapter_names": ["Hiatus Notice"]})
    chapter_id, _ = discovered(world, "Chapter 12 - Hiatus Notice", scanlator="Grp")
    assert isinstance(run_stage(world, chapter_id), workflow.Done), "numbered: it may be a chapter"


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
