"""Volume covers following MangaDex (SPEC "Retroactive updates"): each check of a series fetches again a stored volume
image whose volume MangaDex now chooses another file for; a new image replaces the stored one, and the published files
holding the old one take the new one (or drop it, outside its volume) through the cover rewrite."""

from contextlib import closing

import pytest

from manga_pipeline import db, export, intake, process, rewrite_covers, workflow
from manga_pipeline.clients import http
from manga_pipeline.metadata import covers, human, naming, rewrite_plan

from tests.test_cover_rewrites import library, members, png, row, states, store_image
from tests.test_rewrites import override, scan
from tests.timing import budget
from tests.world import World

MD = "7a3c0b8e-1f2d-4c5e-9a6b-0d1e2f3a4b5c"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def mangadex(world, file_name, data):
    """MangaDex's record of the series, its cover list choosing file_name for volume 2, and that file's image."""
    md = world.sources.mangadex
    md.images[(MD, file_name)] = data
    md.records[("manga", MD)] = {"id": MD, "attributes": {"originalLanguage": "ko"}}
    md.records[("covers", MD)] = [{"volume": "2", "locale": "en", "fileName": file_name}]


def series_id(world):
    ((sid,),) = world.rows("SELECT id FROM series WHERE origin = 'kavita'")
    return sid


def check_now(world):
    """`series refetch-covers`, then the intake whose check of the series refreshes its covers."""
    with closing(db.connect(world.cfg)) as conn:
        human.refetch_covers(conn, series_id(world))
    world.run(intake)


def stored(world):
    return world.rows("SELECT volume, source_url, sha256 FROM series_volume_cover")


@budget(load="db", seconds=60)
def test_a_new_image_at_mangadex_replaces_the_stored_one_and_the_files_cover(world):
    library(world)
    with closing(db.connect(world.cfg)) as conn:
        human.set_id(conn, series_id(world), "mangadex", MD)
    mangadex(world, "a.png", png(900))
    check_now(world)  # the cover list stored; no image stored yet: nothing to refresh
    assert stored(world) == []
    override(world, {1: "1-2", 2: "3-"})
    world.run(export)
    world.run(intake)  # fetches the wanted volume 2 image
    ((_, url, first),) = stored(world)
    assert url.endswith(f"/{MD}/a.png")
    scan(world)
    world.run(export)
    world.run(export)
    world.run(process)
    world.run(export)
    assert states(world)["library/Solo Leveling/Vol.2 Ch.3.cbz"] == "volume:2", "set up: the file holds image a"
    scan(world)
    world.run(export)
    check_now(world)
    assert stored(world)[0][2] == first, "MangaDex still chooses a.png: nothing fetched again"
    mangadex(world, "b.png", png(900, colour=(20, 160, 60)))
    check_now(world)
    ((_, url, second),) = stored(world)
    assert url.endswith("/b.png"), "the check fetched MangaDex's new choice"
    assert second != first, "the check fetched MangaDex's new choice"
    assert states(world)["library/Solo Leveling/Vol.2 Ch.3.cbz"] == "stale:2", "the file holds the old image"
    world.run(export)
    world.run(process)
    world.run(export)
    names_, _, pages = members(world, "Vol.2 Ch.3.cbz")
    assert names_[0].startswith("000_cover.")
    assert sum(n.startswith("000_cover.") for n in names_) == 1
    assert pages[0]["Type"] == "FrontCover"
    assert states(world)["library/Solo Leveling/Vol.2 Ch.3.cbz"] == "volume:2", "the new image in place of the old"
    ((sha,),) = world.rows(
        "SELECT v.sha256 FROM volume_cover v JOIN chapter c ON c.id = v.chapter_id ORDER BY c.id DESC LIMIT 1"
    )
    assert sha == second, "the rewrite took the new image"


def stored_a(world, locale="en"):
    """The series' volume 2 image stored from MangaDex's a.png (listed under locale)."""
    library(world)
    with closing(db.connect(world.cfg)) as conn:
        human.set_id(conn, series_id(world), "mangadex", MD)
    mangadex(world, "a.png", png(900))
    world.sources.mangadex.records[("covers", MD)][0]["locale"] = locale
    check_now(world)
    with closing(db.connect(world.cfg)) as conn:
        covers.volume_cover(world.context(conn, "intake"), series_id(world), 2)


def lists_read(world):
    return sum(1 for c in world.sources.calls if c[1] == "covers")


def answer(world, status):
    """MangaDex answers status for b.png, after the check's own series-cover fetch of it (covers.pick)."""
    md, asked = world.sources.mangadex, []
    real = md.cover_image

    def cover_image(md_id, name):
        asked.append(name)
        if name == "b.png" and asked.count(name) > 1:
            raise http.HttpError(status, f"https://uploads.mangadex.org/covers/{md_id}/{name}", b"")
        return real(md_id, name)

    md.cover_image = cover_image


def due_now(world):
    sql = "SELECT next_check_at <= checked_at FROM series_check WHERE series_id = %s"
    ((due,),) = world.rows(sql, series_id(world))
    return due


B = [{"volume": "2", "locale": "en", "fileName": "b.png"}]


def check_keeps_a(world, monkeypatch, case):
    """With image a stored, MangaDex answers as case says and the check runs: it reads the list and refreshes, and
    the stored image stays."""
    stored_a(world)
    before, lists, refreshed = stored(world), lists_read(world), []
    real = covers.refresh_volumes
    monkeypatch.setattr(covers, "refresh_volumes", lambda ctx, sid: (refreshed.append(sid), real(ctx, sid)))
    md = world.sources.mangadex
    md.images[(MD, "b.png")] = png(900, colour=(20, 160, 60))
    if case == "no-choice":
        md.records[("covers", MD)] = []
    elif case == "tie":  # a.png still 1 of the best, listed second
        md.records[("covers", MD)] = [*B, {"volume": "2", "locale": "en", "fileName": "a.png"}]
    else:
        md.records[("covers", MD)] = B
        if case == "not-an-image":
            md.images[(MD, "b.png")] = b"an error page"
        else:
            answer(world, 404 if case == "not-served" else 503)
    check_now(world)
    assert lists_read(world) > lists, "the check read the list and refreshed"
    assert refreshed == [series_id(world)], "the check read the list and refreshed"
    assert stored(world) == before, "the stored image stays"


@budget(load="db", seconds=30)
@pytest.mark.parametrize("case", ["no-choice", "tie"])
def test_the_stored_image_stays_while_mangadex_chooses_no_new_file(world, monkeypatch, caplog, case):
    check_keeps_a(world, monkeypatch, case)
    assert "volume 2 cover not refreshed" not in caplog.text, "b.png tried only when it is the new choice"
    assert due_now(world) is False, "a series whose cover choice stands is not due again"


@budget(load="db", seconds=30)
@pytest.mark.parametrize(
    ("case", "tried"),
    [
        ("not-served", False),  # a 404: no cover file, the stored image kept quietly
        ("not-an-image", True),
    ],
)
def test_the_stored_image_stays_when_mangadex_refuses_its_new_choice(world, monkeypatch, caplog, case, *, tried):
    check_keeps_a(world, monkeypatch, case)
    assert ("volume 2 cover not refreshed" in caplog.text) is tried, "b.png tried only when it is the new choice"
    assert due_now(world) is False, "a refused file waits for the next check"


@budget(load="db", seconds=30)
def test_the_stored_image_stays_and_the_check_is_due_again_while_mangadex_is_down(world, monkeypatch, caplog):
    check_keeps_a(world, monkeypatch, "mangadex-down")
    assert "volume 2 cover not refreshed" in caplog.text, "b.png tried only when it is the new choice"
    assert due_now(world) is True, "MangaDex down: the series' check is due again"


@budget(load="db", seconds=30)
def test_an_english_cover_replaces_a_stored_one_of_another_language(world):
    stored_a(world, locale="ja")
    before = stored(world)
    world.sources.mangadex.images[(MD, "b.png")] = png(900, colour=(20, 160, 60))
    world.sources.mangadex.records[("covers", MD)] = [{"volume": "2", "locale": "ja", "fileName": "a.png"}, *B]
    check_now(world)
    assert stored(world)[0][1].endswith("/b.png")
    assert stored(world) != before


@budget(load="db", seconds=30)
def test_refetch_covers_makes_the_check_due_and_refuses_an_unknown_series(world):
    library(world)
    world.rows("UPDATE series_check SET urgent = false RETURNING series_id")
    with closing(db.connect(world.cfg)) as conn:
        human.refetch_covers(conn, series_id(world))
        with pytest.raises(workflow.Refused):
            human.refetch_covers(conn, 999)
    assert world.rows("SELECT urgent FROM series_check WHERE series_id = %s", series_id(world)) == [(True,)]


# --- the pure part


@budget(load="pure", seconds=1)
def test_a_stale_file_of_its_volume_takes_the_new_image_and_one_of_another_volume_drops_it():
    rows = [row(1, 1, 1), row(2, 2, 1), row(3, 3, 2), row(4, 4, 2)]
    states_ = {1: "none", 2: "stale:2", 3: "stale:2", 4: "none"}
    covered = naming.covered_volumes("override", rows, {1: "1-2", 2: "3-"}, {})
    got = rewrite_plan.cover_changes(rows, covered, states=states_, taken=set(), images={"1": "x", "2": "y"})
    assert got == {1: ("insert", "1"), 2: ("drop", None), 3: ("insert", "2")}, (
        "the stale holder in volume 2 takes the new image (not file 4); the stale file in volume 1 drops it"
    )


@budget(load="db", seconds=30)
def test_a_chapter_published_with_a_replaced_image_holds_a_stale_cover(world):
    library(world)
    store_image(world, 2, png(900))
    ((chapter,),) = world.rows("SELECT id FROM chapter WHERE kind = 'download'")
    world.rows("INSERT INTO volume_cover (chapter_id, path, sha256, volume) VALUES (%s, 'volume/old.png', 'old', '2')"
               " RETURNING chapter_id", chapter)  # fmt: skip
    with closing(db.connect(world.cfg)) as conn:
        state = rewrite_covers.current_state
        assert state(conn, chapter, series_id(world), "volume:2") == "stale:2", "it took the image replaced since"
        assert state(conn, chapter, series_id(world), "none") == "none"
        conn.execute("UPDATE volume_cover SET sha256 = (SELECT sha256 FROM series_volume_cover) WHERE chapter_id = %s",
                     (chapter,))  # fmt: skip
        assert state(conn, chapter, series_id(world), "volume:2") == "volume:2", "the image stored now"


@budget(load="pure", seconds=1)
@pytest.mark.parametrize(
    ("state", "volume"), [("volume:2", "2"), ("stale:2", "2"), ("volume:", ""), ("none", None), ("unfit:ab", None)]
)
def test_a_cover_page_is_held_for_a_current_or_an_old_image(state, volume):
    assert rewrite_covers.cover_volume(state) == volume


@budget(load="db", seconds=30)
@pytest.mark.parametrize("state", ["volume:", "volume:2.5"])
def test_a_cover_of_no_whole_volume_follows_no_stored_image(world, state):
    library(world)
    store_image(world, 2, png(900))
    ((chapter,),) = world.rows("SELECT id FROM chapter WHERE kind = 'download'")
    world.rows("INSERT INTO volume_cover (chapter_id, path, sha256, volume) VALUES (%s, 'volume/o.png', 'o', %s)"
               " RETURNING chapter_id", chapter, state.removeprefix("volume:"))  # fmt: skip
    with closing(db.connect(world.cfg)) as conn:
        assert rewrite_covers.current_state(conn, chapter, series_id(world), state) == state
