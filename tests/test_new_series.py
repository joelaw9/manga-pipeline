"""New series (SPEC "Series identification", "Metadata checks", "Publishing", "State").

A download no Kavita series matches becomes a pending new series, is admitted under its name and folder after its first
metadata check, and its chapters go through intake and process with a chosen volume basis and a measured layout
(publishing: test_new_series_publish.py).
"""

from contextlib import closing

import pytest

from manga_pipeline import config, db, discovery, identify, intake, process, workflow
from manga_pipeline.db import sql
from manga_pipeline.metadata import human, naming

from tests.builders import one, present, scope_cfg, settle
from tests.timing import budget
from tests.world import World, cbz, comicinfo


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def series(world):
    return world.rows(
        "SELECT name, status, comicinfo_series, folder, admitted_at IS NOT NULL FROM series WHERE origin = 'new'"
        " ORDER BY id"
    )


def anilist(world, title, english, al_id=7):
    media = {"id": al_id, "format": "MANGA", "title": {"romaji": title, "english": english, "native": None}}
    world.sources.anilist.results[title] = [media]
    world.sources.anilist.records[("media", str(al_id))] = media


def place(world, folder, name, data):
    path = world.mangas / folder / f"{name}.cbz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return settle(path)


@budget(load="db", seconds=2)
def test_a_new_series_is_admitted_under_its_anilist_english_title_and_taken_by_the_next_run(world):
    world.kavita_series("Solo Leveling")
    anilist(world, "Brand New Thing", "The Brand New Thing @COMIC")
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.run(intake)
    assert series(world)[0] == (
        "Brand New Thing",
        "ok",
        "The Brand New Thing",
        "library/The Brand New Thing",
        True,
    ), "admitted after its first check: AniList English title (its @COMIC mark dropped), folder under the library"
    assert world.rows("SELECT count(*) FROM chapter") == [(0,)], "discovery ran before the admission"
    world.run(intake)
    assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")], "taken and validated next run"
    assert world.rows("SELECT naming_basis FROM series WHERE origin = 'new'") == [("none",)], "nothing gives a volume"


@budget(load="db", seconds=2)
def test_a_pending_series_lists_its_downloads_until_admitted(world):
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    with closing(db.connect(world.cfg)) as conn:
        discovery.scan(world.context(conn, "intake"))
    assert series(world) == [("Brand New Thing", "ok", None, None, False)], "pending: a row, no name or folder yet"
    assert world.rows("SELECT reason FROM unmatched_download") == [("new series, metadata not fetched yet",)]


@budget(load="db", seconds=2)
def test_a_hand_placed_new_folder_is_named_by_its_files_comicinfo_series(world):
    for n in (1, 2):
        place(world, "Local/raw dump", f"Chapter {n}", cbz(seed=str(n), comicinfo=comicinfo(Series="Clean Name")))
    world.run(intake)
    assert world.rows("SELECT name, alt_name, comicinfo_series, folder FROM series") == [
        ("raw dump", "Clean Name", "Clean Name", "library/Clean Name")
    ], "no AniList title: the files' ComicInfo Series before the title folder"


@budget(load="db", seconds=2)
def test_sources_down_at_the_first_check_leave_the_series_pending_until_a_run_reaches_them(world, monkeypatch):
    world.download(world.manga("Brand New Thing"), "Chapter 1")

    def down(_name):
        msg = "AniList"
        raise workflow.Unreachable(msg, "down")

    with monkeypatch.context() as m:
        m.setattr(world.sources.anilist, "search", down)
        world.run(intake)
        assert series(world)[0][4] is False, "no completed check: not admitted"
    world.run(intake)
    assert series(world)[0][2:] == ("Brand New Thing", "library/Brand New Thing", True), "admitted once reached"


@budget(load="db", seconds=2)
def test_a_fuzzy_near_hit_is_held_and_series_new_admits_it(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Levelling"), "Chapter 1")
    world.run(intake)
    ((sid, status, reason),) = world.rows("SELECT id, status, status_reason FROM series WHERE origin = 'new'")
    assert status == "needs_human", "ratio >= 90 is a near hit (D7.18)"
    assert "nearly matches: Solo Leveling" in reason, "ratio >= 90 is a near hit (D7.18)"
    assert world.rows("SELECT status FROM chapter") == [("ready",)], "its chapter is recorded and held"
    with closing(db.connect(world.cfg)) as conn, pytest.raises(workflow.Refused, match="series new"):
        workflow.retry_series(conn, sid)
    with closing(db.connect(world.cfg)) as conn:
        human.new_series(conn, sid)
    world.run(intake)
    assert world.rows("SELECT comicinfo_series, status FROM series WHERE id = %s", sid) == [("Solo Levelling", "ok")]


@budget(load="db", seconds=2)
def test_a_name_clash_holds_the_series_and_series_new_name_admits_it(world):
    (world.library / "Brand New Thing").mkdir(parents=True)  # a stray folder Kavita does not list
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.run(intake)
    ((sid, status, reason),) = world.rows("SELECT id, status, status_reason FROM series")
    assert status == "needs_human"
    assert "already has a folder" in reason
    assert f"series new {sid} --name" in reason
    with closing(db.connect(world.cfg)) as conn, pytest.raises(workflow.Refused, match="series new"):
        workflow.retry_series(conn, sid)
    with closing(db.connect(world.cfg)) as conn:
        human.new_series(conn, sid, "Brand New Thing (2024)")
    world.run(intake)
    assert series(world)[0][1:] == ("ok", "Brand New Thing (2024)", "library/Brand New Thing (2024)", True)


@budget(load="db")
def test_series_new_refuses_a_series_that_is_not_a_held_new_one(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 1")
    world.run(intake)
    ((sid,),) = world.rows("SELECT id FROM series")
    with closing(db.connect(world.cfg)) as conn, pytest.raises(workflow.Refused, match="not a new series"):
        human.new_series(conn, sid)


@budget(load="db", seconds=10)
def test_a_new_series_goes_through_process_to_publish(world):
    anilist(world, "Brand New Thing", None)
    manga = world.manga("Brand New Thing")
    for n in (1, 2, 3, 4):  # only the last carries a volume: a choice over chapter 1 alone would be none
        world.download(manga, f"{'Vol.1 ' if n == 4 else ''}Chapter {n}", data=cbz(seed=str(n), pages=4))
    world.run(intake)
    world.run(intake)
    assert world.rows("SELECT naming_basis FROM series") == [("filename",)], "chosen over the whole batch"
    world.run(process)
    assert [o for _, o in world.rows("SELECT layout, origin FROM series_conversion")] == ["computed"], (
        "measured over a sample of its chapters"
    )
    assert set(world.rows("SELECT stage, status FROM chapter")) == {("comicinfo", "ready")}, "ready for export"


@budget(load="pure")
@pytest.mark.parametrize(
    ("chapters", "mangadex", "override", "want"),
    [
        ([("main", 1, None, "Vol.1 Ch.1"), ("main", 2, None, "")], {}, None, naming.FILENAME),
        ([("main", 1, 1, ""), ("main", 2, 2, "")], {}, None, naming.SEASON_TAGS),
        ([("main", 1, None, "Episode 1 (Season 1 Finale)"), ("main", 2, None, "")], {}, None, naming.SEASON_MARKERS),
        ([("main", 1, None, ""), ("main", 2, None, "")], {"1": "1"}, None, naming.MANGADEX),
        ([("main", 1, None, ""), ("main", 2, None, ""), ("main", 3, None, "")], {"1": "1"}, None, naming.NONE),
        ([("main", 1, None, ""), ("main", 2, None, "")], {}, {"volumes": {1: "1-"}}, naming.OVERRIDE),
        ([("main", 1, None, ""), ("side", 1, None, "")], {}, None, naming.SIDE_DEFAULT),
        ([("main", 1, None, "")], {}, None, naming.NONE),
    ],
    ids=["filename", "season-tags", "season-markers", "mangadex-half", "mangadex-under-half", "override", "side",
         "none"],
)  # fmt: skip
def test_a_new_series_basis_is_the_first_source_that_volumes_it(chapters, mangadex, override, want):
    rows = [
        naming.Row(
            key=i, settled=False, kind=kind, number=float(number), season=season,
            src_volume="1" if marker.startswith("Vol.") else None, volume=None, volume_source=None,
            marker_title=marker, title="", date=None, order=i,
        )
        for i, (kind, number, season, marker) in enumerate(chapters)
    ]  # fmt: skip
    assert naming.choose(rows, override, mangadex) == want, "the bulk converter's order for a series it meets first"


@budget(load="db", seconds=2)
def test_a_held_new_series_chapters_wait_until_it_is_admitted(world, monkeypatch):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Levelling"), "Chapter 1")
    world.run(intake)
    ((sid,),) = world.rows("SELECT id FROM series WHERE origin = 'new'")
    with closing(db.connect(world.cfg)) as conn:
        human.new_series(conn, sid)

    def down(_library_id):
        msg = "Kavita"
        raise workflow.Unreachable(msg, "down")

    monkeypatch.setattr(world.kavita, "series_in_library", down)
    world.run(intake)  # the metadata step is skipped: no admission this run
    assert world.rows("SELECT stage, status FROM chapter") == [("discover", "ready")], (
        "a series is admitted before any of its chapters is worked"
    )


@budget(load="db", seconds=10)
def test_an_override_layout_is_the_series_layout_whatever_its_case(world):
    manga = world.manga("Brand New Thing")
    world.download(manga, "Chapter 1", data=cbz(seed="1", pages=4))
    world.run(intake)
    overrides = config.abs_path(world.cfg["paths"]["naming_overrides"])
    overrides.mkdir(parents=True, exist_ok=True)
    (overrides / "Brand New Thing.yaml").write_bytes(b"layout: Pages\n")
    world.run(intake)
    world.run(process)
    assert world.rows("SELECT layout, origin FROM series_conversion") == [("pages", "override")], (
        "the bulk converter reads the key case-folded"
    )


@budget(load="db", seconds=2)
def test_series_link_refuses_the_manga_of_an_admitted_new_series(world):
    kavita_id = world.kavita_series("Solo Leveling")
    manga = world.manga("Brand New Thing")
    world.download(manga, "Chapter 1")
    world.run(intake)
    with closing(db.connect(world.cfg)) as conn, pytest.raises(workflow.Refused, match="admitted"):
        identify.link(
            world.cfg, conn, world.kavita, kavita_id, identify.manga_link_subject(world.suwayomi, manga)
        )


@budget(load="db", seconds=2)
def test_linking_a_held_new_series_moves_its_human_ids(world):
    kavita_id = world.kavita_series("Solo Leveling")
    manga = world.manga("Solo Levelling")
    world.download(manga, "Chapter 1")
    world.run(intake)
    ((held,),) = world.rows("SELECT id FROM series WHERE origin = 'new'")
    with closing(db.connect(world.cfg)) as conn:
        conn.execute("INSERT INTO series_source_id VALUES (%s, 'anilist', 'human', '7')", (held,))
        subject = identify.manga_link_subject(world.suwayomi, manga)
        target = identify.link(world.cfg, conn, world.kavita, kavita_id, subject)
    assert world.rows("SELECT series_id, source_id FROM series_source_id WHERE origin = 'human'") == [(target, "7")]
    assert world.rows("SELECT count(*) FROM series WHERE id = %s", held) == [(0,)], "the held row is merged away"


@budget(load="db")
@pytest.mark.parametrize("column", ["name", "alt_name", "comicinfo_series", "localized_name", "original_name"])
def test_series_only_names_a_series_by_any_of_its_names_in_sql_and_python(world, column):
    with closing(db.connect(world.cfg)) as conn:
        sid = one(
            conn,
            sql.SQL("INSERT INTO series (name, origin, {}) VALUES ('Base', 'new', 'Wanted') RETURNING id").format(
                sql.Identifier(column)
            )
            if column != "name"
            else "INSERT INTO series (name, origin) VALUES ('Wanted', 'new') RETURNING id",
        )
        cfg = scope_cfg(4, ["Wanted"])
        row = present(conn.execute("SELECT * FROM series WHERE id = %s", (sid,)).fetchone())
        found = conn.execute(
            sql.SQL("SELECT 1 FROM series s WHERE s.id = %(id)s AND {}").format(workflow.SCOPE),
            {**workflow.scope_params(cfg), "id": sid},
        ).fetchone()
        assert (found is not None) is workflow.row_in_scope(cfg, row) is True, f"{column}: SQL and Python agree"
