"""New series (SPEC "Series identification", "Metadata checks", "Publishing", "State").

A download no Kavita series matches becomes a pending new series, is admitted under its name and folder after its first
metadata check, and its chapters go through intake and process with a chosen volume basis and a measured layout
(publishing: test_new_series_publish.py).
"""

import io
import zipfile
from contextlib import closing

import numpy as np
import pytest
from PIL import Image

from manga_pipeline import config, db, discovery, identify, intake, process, workflow
from manga_pipeline.db import sql
from manga_pipeline.metadata import human, naming

from tests.builders import one, present, scope_cfg, settle
from tests.timing import budget
from tests.world import SOURCE_FOLDER, World, cbz, comicinfo


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
        f"library/{SOURCE_FOLDER}/The Brand New Thing",
        True,
    ), "admitted after its first check: AniList English title (its @COMIC mark dropped), folder in its source folder"
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
        ("raw dump", "Clean Name", "Clean Name", "library/Local/Clean Name")
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
    assert series(world)[0][2:] == ("Brand New Thing", f"library/{SOURCE_FOLDER}/Brand New Thing", True), (
        "admitted once reached"
    )


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
    (world.library / SOURCE_FOLDER / "Brand New Thing").mkdir(parents=True)  # a stray folder Kavita does not list
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
    assert series(world)[0][1:] == (
        "ok",
        "Brand New Thing (2024)",
        f"library/{SOURCE_FOLDER}/Brand New Thing (2024)",
        True,
    )


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
    assert set(world.rows("SELECT stage, status FROM chapter")) == {("comicinfo", "ready")}, "ready for export"


def page_manga_cbz(pages=4):
    """Return a CBZ of page-manga pages: each a left-to-right ramp shifted per page, so no 2 join as slices."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n in range(1, pages + 1):
            ramp = ((np.arange(32) * 6 + 60 * n) % 200 + 20).astype(np.uint8)
            out = io.BytesIO()
            Image.fromarray(np.tile(ramp, (48, 1)), "L").convert("RGB").save(out, "PNG")
            z.writestr(f"{n:03}.png", out.getvalue())
    return data.getvalue()


@budget(load="db", seconds=10)
def test_a_new_series_layout_is_measured_over_a_sample_of_its_chapters(world):
    manga = world.manga("Brand New Thing")
    world.download(manga, "Chapter 1", data=page_manga_cbz())  # alone, it measures as pages
    for n in (2, 3):  # 1-colour pages: every join continuous, as a strip's slices
        world.download(manga, f"Chapter {n}", data=cbz(seed=str(n), pages=4))
    world.run(intake)
    world.run(intake)
    world.run(process)
    assert world.rows("SELECT layout, origin FROM series_conversion") == [("strip", "computed")], (
        "a new series' layout is the median over up to 3 of its chapters, not the first chapter's alone"
    )


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
    assert naming.choose(rows, override, naming.MangaDex(mangadex)) == want, (
        "the bulk converter's order for a series it meets first"
    )


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
        identify.link(world.cfg, conn, world.kavita, kavita_id, identify.manga_link_subject(world.suwayomi, manga))


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


def give_id(world, series_id, value, origin="search"):
    """Store an AniList id of the series, as its check or a person would have."""
    world.rows(
        "INSERT INTO series_source_id VALUES (%s, 'anilist', %s, %s) RETURNING series_id", series_id, origin, value
    )


def library_series_with_id(world, value="7"):
    """Add a Kavita series, let intake make its row, and give that row the AniList id; return the row's id."""
    world.kavita_series("Solo Leveling")
    world.run(intake)
    ((sid,),) = world.rows("SELECT id FROM series WHERE origin = 'kavita'")
    give_id(world, sid, value)
    return sid


def new_one(world):
    return world.rows("SELECT id, status, status_reason, admitted_at IS NOT NULL FROM series WHERE origin = 'new'")


@budget(load="db", seconds=5)
@pytest.mark.parametrize(("human_id", "held"), [(None, True), ("9", False)], ids=["shared", "library-human-differs"])
def test_a_pending_series_whose_id_a_library_series_holds_is_a_double_hit(world, human_id, held):
    library = library_series_with_id(world)
    if human_id is not None:
        give_id(world, library, human_id, "human")
    anilist(world, "Brand New Thing", None, al_id=7)
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.run(intake)
    ((sid, status, reason, admitted),) = new_one(world)
    if not held:
        assert (status, admitted) == ("ok", True), "a human id wins: the library series' AniList id is not 7"
        return
    assert (status, admitted) == ("needs_human", False), "an id a library series holds is a double hit"
    assert f"series {library} (Solo Leveling) holds its anilist id 7" in reason, "the hold names the series and the id"
    assert "series link" in reason, "the hold names series link"
    assert f"series new {sid}" in reason, "the hold names series new"
    assert "set-id" not in reason, "set-id does not lift a hold"


@budget(load="db", seconds=10)
def test_series_new_admits_a_series_held_for_a_shared_id_and_its_name_is_still_checked(world):
    library_series_with_id(world)
    anilist(world, "Brand New Thing", None, al_id=7)
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.run(intake)
    ((sid, status, _, _),) = new_one(world)
    assert status == "needs_human", "set up: held for the shared id"
    (world.library / SOURCE_FOLDER / "Brand New Thing").mkdir(parents=True)  # a stray folder Kavita does not list
    with closing(db.connect(world.cfg)) as conn:
        human.new_series(conn, sid)
    world.run(intake)
    ((_, status, reason, _),) = new_one(world)
    assert status == "needs_human", "after series new the name is still compared"
    assert "already has a folder" in reason, "after series new the ids are not compared again, the name still is"
    with closing(db.connect(world.cfg)) as conn:
        human.new_series(conn, sid, "Brand New Thing (2024)")
    world.run(intake)
    assert new_one(world)[0][1:] == ("ok", None, True), "the person's word stands: admitted despite the shared id"


@budget(load="db", seconds=10)
def test_a_series_released_by_series_new_before_its_first_check_is_held_for_an_id_it_shares(world):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Levelling"), "Chapter 1")
    world.run(intake)
    ((sid, status, _, _),) = new_one(world)
    assert status == "needs_human", "set up: a near hit, held before any check"
    ((library,),) = world.rows("SELECT id FROM series WHERE origin = 'kavita'")
    give_id(world, library, "7")
    anilist(world, "Solo Levelling", None, al_id=7)
    with closing(db.connect(world.cfg)) as conn:
        human.new_series(conn, sid)
    world.run(intake)
    ((_, status, reason, _),) = new_one(world)
    assert status == "needs_human", "no person saw its ids: they are compared at its admission"
    assert "holds its anilist id 7" in reason, "no person saw its ids: they are compared at its admission"


@budget(load="db", seconds=5)
def test_of_2_pending_series_sharing_an_id_the_second_is_held_once_the_first_is_admitted(world):
    anilist(world, "Brand New Thing", None, al_id=7)
    anilist(world, "Other New Thing", None, al_id=7)
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.download(world.manga("Other New Thing"), "Chapter 1")
    world.run(intake)
    (first, second) = world.rows(
        "SELECT id, status, status_reason, admitted_at IS NOT NULL FROM series WHERE origin = 'new' ORDER BY id"
    )
    assert first[1:] == ("ok", None, True), "the first is admitted"
    assert second[1] == "needs_human", "an admitted new series' id is a double hit for the second, in the same run"
    assert f"series {first[0]} (Brand New Thing) holds its anilist id 7" in second[2], "the hold names the first"


@budget(load="db", seconds=2)
def test_a_new_series_is_admitted_into_the_library_folder_of_its_manga_source(world):
    manga = world.suwayomi.add_manga("Brand New Thing", world.suwayomi.add_source("MangaK"))
    world.download(manga, "Chapter 1")
    world.run(intake)
    assert series(world)[0][3:] == ("library/MangaK (EN)/Brand New Thing", True), (
        "its source's download folder name is its library source folder"
    )


@budget(load="db", seconds=2)
def test_a_same_named_folder_in_another_source_folder_is_no_clash(world):
    (world.library / "Other (EN)" / "Brand New Thing").mkdir(parents=True)
    (world.library / "Brand New Thing").mkdir()
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.run(intake)
    assert series(world)[0][1:] == ("ok", "Brand New Thing", f"library/{SOURCE_FOLDER}/Brand New Thing", True), (
        "only the folders in its own source folder clash"
    )


def held_reason(world):
    ((status, reason, admitted),) = world.rows(
        "SELECT status, status_reason, admitted_at IS NOT NULL FROM series WHERE origin = 'new'"
    )
    assert (status, admitted) == ("needs_human", False), "held at admission"
    return reason


@budget(load="db", seconds=2)
def test_a_series_with_links_to_several_source_folders_takes_its_lowest_id_manga_s(world):
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    with closing(db.connect(world.cfg)) as conn:
        discovery.scan(world.context(conn, "intake"))  # pending: its row and first link, no check yet
    ((sid,),) = world.rows("SELECT id FROM series WHERE origin = 'new'")
    world.rows("INSERT INTO download_folder (folder, series_id) VALUES ('Local/Brand New Thing', %s) RETURNING 1", sid)
    other = world.suwayomi.add_source("MangaK")
    world.rows(  # linked after the first, with a lower manga id than any the fake gives
        "INSERT INTO suwayomi_manga (manga_id, source_id, title, series_id) VALUES (1, %s, 'x', %s) RETURNING 1",
        other,
        sid,
    )
    world.run(intake)
    assert series(world)[0][3:] == ("library/MangaK (EN)/Brand New Thing", True), (
        "its manga with the lowest id decides, before a later manga and a hand-placed folder"
    )


@budget(load="db", seconds=2)
def test_a_series_whose_manga_source_is_not_installed_is_held_with_the_fix(world, monkeypatch):
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    monkeypatch.setattr(world.suwayomi, "sources", list)  # Suwayomi lists no source: the manga's is uninstalled
    world.run(intake)
    reason = held_reason(world)
    ((sid,),) = world.rows("SELECT id FROM series WHERE origin = 'new'")
    assert f"the source {world.source} of its manga" in reason, "the hold names the source"
    assert "is not installed" in reason, "the hold names why"
    assert f"install the source; then series new {sid}" in reason, "the hold names the fix"


@budget(load="db", seconds=2)
def test_a_missing_library_root_at_admission_ends_the_run_and_holds_no_series(world, caplog):
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.library.rmdir()  # the share's library folder is not there
    world.run(intake)
    assert series(world)[0][1:] == ("ok", None, None, False), "pending, not held: the share's problem"
    assert f"library folder {world.library} missing" in caplog.text, "admission ends the run: Unreachable(share)"


@budget(load="db", seconds=3)
def test_suwayomi_down_at_admission_leaves_the_series_pending_and_intake_works_its_chapters(world, monkeypatch):
    world.kavita_series("Solo Leveling")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    world.download(world.manga("Brand New Thing"), "Chapter 1")

    def down():
        msg = "suwayomi"
        raise workflow.Unreachable(msg, "down")

    with monkeypatch.context() as m:
        m.setattr(world.suwayomi, "sources", down)
        world.run(intake)
        assert series(world)[0][1:] == ("ok", None, None, False), "not admitted: pending until a run lists the sources"
        assert world.rows("SELECT stage, status FROM chapter") == [("junk", "ready")], (
            "the library series' chapter is taken and validated all the same"
        )
    world.run(intake)
    assert series(world)[0][4] is True, "admitted once Suwayomi answers"


@budget(load="db", seconds=2)
def test_a_new_series_gets_its_romaji_as_localized_name_when_free(world):
    anilist(world, "Brand New Thing", "The Brand New Thing")
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.run(intake)
    assert world.rows("SELECT comicinfo_series, localized_name FROM series WHERE origin = 'new'") == [
        ("The Brand New Thing", "Brand New Thing")
    ], "SPEC 'Series names': the romanized name is the localized name, so its files carry it as LocalizedSeries"


@budget(load="db", seconds=2)
def test_a_new_series_romaji_another_series_holds_is_left_out(world):
    world.kavita_series("Shinpin-Mono")
    media = {
        "id": 7,
        "format": "MANGA",
        "title": {"romaji": "Shinpin Mono", "english": "Brand New Thing", "native": None},
    }
    world.sources.anilist.results["Brand New Thing"] = [media]
    world.sources.anilist.records[("media", "7")] = media
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.run(intake)
    assert world.rows("SELECT status, comicinfo_series, localized_name FROM series WHERE origin = 'new'") == [
        ("ok", "Brand New Thing", None)
    ], "a romanized name that is not free is left out; the series is still admitted"


@budget(load="db", seconds=2)
def test_a_new_series_named_by_its_title_has_no_romaji_equal_to_it(world):
    media = {"id": 7, "format": "MANGA", "title": {"romaji": "Brand New Thing", "english": None, "native": None}}
    world.sources.anilist.results["Brand New Thing"] = [media]
    world.sources.anilist.records[("media", "7")] = media
    world.download(world.manga("Brand New Thing"), "Chapter 1")
    world.run(intake)
    assert world.rows("SELECT comicinfo_series, localized_name FROM series WHERE origin = 'new'") == [
        ("Brand New Thing", None)
    ], "SPEC 'Series names': no romanized name when it equals the name"
