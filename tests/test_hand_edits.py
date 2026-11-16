"""Hand edits (SPEC "Retroactive updates", hand edits): `series edit` stores a person's chapter titles and volumes,
checked whole; they win over every source, are written into published files by 7e's rewrite (progress carried), are
never reverted by a later check, and their removal brings the sources' values back."""

import dataclasses
import zipfile
from contextlib import closing

import pytest

from manga_pipeline import comicinfo as comicinfo_xml
from manga_pipeline import db, export, hand_edits, intake, names, process, rewrite, workflow
from manga_pipeline.metadata import naming, rewrite_plan

from tests import test_better_copies as copies
from tests.test_cover_rewrites import members, png, store_image
from tests.test_naming_resolver import new, settled
from tests.test_rewrites import SERIES, crash_after, files, library, progress, read, scan
from tests.timing import budget
from tests.world import World, cbz, comicinfo


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def edit(world, chapters, series=SERIES):
    """`series edit` of an edit file holding these chapters; returns the printed lines."""
    with closing(db.connect(world.cfg)) as conn:
        return hand_edits.apply(world.context(conn, "export"), {"series": series, "chapters": chapters})


def title(world, name):
    with zipfile.ZipFile(world.library / SERIES / name) as z:
        return comicinfo_xml.read_all(z.read("ComicInfo.xml")).get("Title")


def through(world):
    """The rewrites a changed edit queues, finished, and Kavita's re-read restoring progress."""
    world.run(export)
    world.run(process)
    world.run(export)
    scan(world)
    world.run(export)


@budget(load="db", seconds=60)
def test_a_title_edit_renames_a_pipeline_file_keeps_its_progress_and_its_removal_restores_it(world):
    kid = library(world)
    read(world, kid, "Ch.3.cbz", 2)
    assert edit(world, {"3": {"title": "The Hunt"}}) == [f"series 1 ({SERIES}): 3: title 'The Hunt'"]
    through(world)
    assert files(world) == ["Ch.3 - The Hunt.cbz", "Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"], "renamed for its new title"
    assert title(world, "Ch.3 - The Hunt.cbz") == "The Hunt"
    assert progress(world, kid)["Ch.3 - The Hunt.cbz"][0] == 2, "the rename's lost progress carried"
    assert world.rows("SELECT unedited FROM published_file WHERE library_path LIKE '%%Ch.3%%'") == [
        ({"title": None},)
    ], "its sources gave it no title"
    world.run(export)
    assert world.rows("SELECT count(*) FROM chapter WHERE kind = 'rewrite'") == [(1,)], "a later check reverts nothing"
    assert edit(world, {"3": {"title": None}}) == [f"series 1 ({SERIES}): 3: edit removed"]
    through(world)
    assert files(world) == ["Ch.3.cbz", "Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"], "back to its sources' name"
    assert title(world, "Ch.3.cbz") == "Chapter 3"
    assert world.rows("SELECT comicinfo_title, unedited FROM published_file WHERE library_path LIKE '%%Ch.3%%'") == [
        (None, None)
    ]
    assert world.rows("SELECT kavita_series_id FROM series") == [(kid,)], "the series id is kept"


@budget(load="db", seconds=60)
def test_a_title_edit_of_a_bulk_file_keeps_its_name_and_a_volume_rewrite_keeps_its_title(world):
    library(world)
    edit(world, {"1": {"title": "Awakening"}, "2": {"volume": 2}})
    through(world)
    assert files(world) == ["Ch.3.cbz", "Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz"], "a bulk file keeps its name"
    _, first, _ = members(world, "Vol.1 Ch.1.cbz")
    _, second, _ = members(world, "Vol.1 Ch.2.cbz")
    assert (first["Title"], first["Volume"], first["Translator"]) == ("Awakening", "1", "Grp"), "own fields kept"
    assert (second.get("Title"), second["Volume"]) == (None, "2"), "a volume edit writes no title the file lacked"
    assert world.rows("SELECT volume_source, unedited FROM published_file WHERE library_path LIKE '%%Ch.2.cbz'") == [
        ("edit", {"volume": "1", "volume_source": "filename"})
    ]


@budget(load="db", seconds=60)
def test_a_volume_edit_opening_a_covered_volume_inserts_its_cover_and_its_removal_drops_it(world):
    library(world)
    store_image(world, 2, png(900))
    edit(world, {"3": {"volume": 2}})
    world.run(export)
    assert world.rows("SELECT stage FROM chapter WHERE kind = 'rewrite'") == [("convert",)], "volume and cover in 1"
    through(world)
    assert files(world) == ["Vol.1 Ch.1.cbz", "Vol.1 Ch.2.cbz", "Vol.2 Ch.3.cbz"]
    got, fields, _ = members(world, "Vol.2 Ch.3.cbz")
    assert got[0].startswith("000_cover"), "a hand-given volume is covered"
    assert fields["Volume"] == "2", "a hand-given volume is covered"
    edit(world, {"3": {"volume": None}})
    through(world)
    got, fields, _ = members(world, "Ch.3.cbz")
    assert not got[0].startswith("000_cover"), "back to no volume, its cover dropped"
    assert "Volume" not in fields, "back to no volume, its cover dropped"


@budget(load="db", seconds=60)
def test_an_open_chapter_is_published_edited_and_its_removal_restores_the_sources_name(world):
    library(world)
    manga = next(i for i, m in world.suwayomi.manga_rows.items() if m["title"] == SERIES)
    world.download(manga, "Chapter 4", data=cbz(pages=4, seed="4"))
    world.run(intake)
    world.run(intake)
    edit(world, {"4": {"title": "Rank Up", "volume": 2}})
    world.run(process)
    world.run(export)
    assert "Vol.2 Ch.4 - Rank Up.cbz" in files(world), "named with its edit"
    assert world.rows("SELECT unedited FROM published_file WHERE library_path LIKE '%%Ch.4%%'") == [
        ({"title": None, "volume": None, "volume_source": "none"},)
    ]
    scan(world)
    world.run(export)  # confirmed
    edit(world, {"4": {"title": None, "volume": None}})
    through(world)
    assert "Ch.4.cbz" in files(world), "its sources' name back"


@budget(load="db", seconds=30)
def test_a_malformed_edit_file_changes_nothing_and_names_every_problem(world):
    library(world)
    with pytest.raises(workflow.Refused) as refused:
        edit(world, {"9": {"title": "x"}, "S 3": {}, "2": {"volume": 1.5, "colour": "red"}, "1": {"title": " "}})
    problems = str(refused.value).splitlines()
    want = ("'S 3'", "colour", "1.5", "' '", "'9': no chapters")
    assert len(problems) == 5, problems
    assert all(any(w in p for p in problems) for w in want), problems
    with pytest.raises(workflow.Refused) as refused:
        edit(world, {"9": {"title": "x"}})
    assert "'9': no chapters of the series match" in str(refused.value)
    with pytest.raises(workflow.Refused) as refused:
        edit(world, {"1": {"title": "x"}}, series="Nobody")
    assert "no in-scope series" in str(refused.value)
    assert world.rows("SELECT count(*) FROM naming_edit") == [(0,)], "nothing stored"


@budget(load="db", seconds=30)
def test_an_edit_out_of_volume_order_is_refused(world):
    library(world)
    with pytest.raises(workflow.Refused) as refused:
        edit(world, {"1": {"volume": 2}})  # chapter 2 stays in volume 1
    assert str(refused.value) == "chapter '2': volume 1 is out of order with chapter 1 in volume 2", "1 line per clash"
    edit(world, {"1": {"volume": 2}, "2": {"volume": 2}})  # together they keep the order


@budget(load="db", seconds=30)
def test_an_edit_stored_again_prints_unchanged(world):
    library(world)
    edit(world, {"3": {"title": "The Hunt"}})
    assert edit(world, {"3": {"title": "The Hunt"}}) == [f"series 1 ({SERIES}): 3: unchanged"]


# --- the pure parts


@budget(load="pure", seconds=1)
@pytest.mark.parametrize(
    ("token", "key"),
    [("10", (names.MAIN, None, 10.0)), ("S2 4.5", (names.MAIN, 2, 4.5)), ("side 3", (names.SIDE, None, 3.0))],
)
def test_a_chapter_token_reads_back_as_written(token, key):
    assert naming.parse_token(token) == key
    assert naming.edit_token(*key) == token


@budget(load="pure", seconds=1)
@pytest.mark.parametrize("token", ["", "ten", "S 3", "side", "S2 x", "1 2 3"])
def test_a_token_that_is_no_chapter_is_refused(token):
    with pytest.raises(ValueError):
        naming.parse_token(token)


@budget(load="pure", seconds=1)
def test_an_edited_volume_names_the_chapters_around_it_and_a_side_storys_title_keeps_its_label():
    rows = [settled(1, 1, "mangadex"), new(2), new(3), new(1, kind=names.SIDE)]
    edits = {naming.edit_key(names.MAIN, None, 2): naming.Edit(None, 3)}
    edits[naming.edit_key(names.SIDE, None, 1)] = naming.Edit("Beach Day", None)
    got = {(k[2], k[1]): v for k, v in naming.resolve("mangadex", rows, None, {"3": "3"}, edits=edits).items()}
    assert (got[(names.MAIN, 2)].volume, got[(names.MAIN, 2)].volume_source) == (3, "edit")
    assert got[(names.MAIN, 2)].opens_covered_volume, "a hand-given volume is covered"
    assert got[(names.SIDE, 1)].volume == 4, "side stories go after the highest main volume, the edited one included"
    assert got[(names.SIDE, 1)].title == "Side Story 1: Beach Day"


@budget(load="pure", seconds=1)
def test_an_edit_back_to_the_sources_volume_restores_their_source():
    rows = [dataclasses.replace(settled(1, 3, "edit"), key=("published", 1))]
    edits = {naming.edit_key(names.MAIN, None, 1): naming.Edit(None, 2)}
    got = rewrite_plan.edit_changes(rows, edits, {1: {"volume": "2", "volume_source": "mangadex"}})
    assert got == {1: (2, "mangadex", "", None)}


@budget(load="pure", seconds=1)
def test_an_open_chapter_edited_to_its_sources_volume_keeps_their_source():
    rows = [settled(1, 1, "filename"), new(2, src_volume="2")]
    edits = {naming.edit_key(names.MAIN, None, 2): naming.Edit(None, 2)}
    (got,) = naming.resolve("filename", rows, None, {}, edits=edits).values()
    assert (got.volume, got.volume_source, got.opens_covered_volume) == (2, "filename", False)


@budget(load="fs", seconds=2)
@pytest.mark.parametrize(("held", "want"), [("Old", False), ("New", True)])
def test_a_same_name_file_is_the_rewritten_one_only_with_the_new_title(tmp_path, held, want):
    path = tmp_path / "Vol.1 Ch.1.cbz"
    path.write_bytes(cbz(pages=2, comicinfo=comicinfo(Number=1, Volume=1, Title=held)))
    row = {"pending_size": path.stat().st_size, "pending_volume": "1", "pending_title": "New",
           "comicinfo_title": "Old", "comicinfo_number": "1"}  # fmt: skip
    assert rewrite._is_new(path, row) is want, "a crash left the old file: same size and volume, its old title"


@budget(load="pure", seconds=1)
def test_edit_changes_apply_an_edit_and_restore_a_removed_one():
    rows = [settled(1, 1, "filename"), settled(1, 2, "side", kind=names.SIDE)]
    rows = [dataclasses.replace(r, key=("published", i)) for i, r in enumerate(rows, 1)]
    edits = {naming.edit_key(names.SIDE, None, 1): naming.Edit("Beach Day", 3)}
    got = rewrite_plan.edit_changes(rows, edits, {1: {"volume": "2", "volume_source": "mangadex"}})
    assert got == {
        1: (2, "mangadex", "", None),
        2: (3, "edit", "Side Story 1: Beach Day", {"volume": "2", "volume_source": "side", "title": None}),
    }


@budget(load="db", seconds=60)
def test_a_better_copy_of_an_edited_file_keeps_the_edit(world):
    copies.library(world)
    edit(world, {"2": {"title": "Hello"}})
    world.run(export)
    world.run(process)
    world.run(export)
    assert copies.row(world)["comicinfo_title"] == "Hello", "set up: the edit written"
    scan(world)
    world.run(export)  # Kavita re-read it: the file is free again
    copies.copy(world, width=20)
    copies.through(world)
    r = copies.row(world)
    assert (r["origin"], r["comicinfo_title"], "title" in (r["unedited"] or {})) == ("pipeline", "Hello", True), (
        "the copy takes the file's edited name and keeps its unedited values for a removal"
    )
    assert title(world, "Vol.1 Ch.2.cbz") == "Hello"


@budget(load="db", seconds=60)
@pytest.mark.parametrize("step", [1, 2, 3, 4])
def test_a_crash_in_a_same_name_title_rewrite_recovers(world, monkeypatch, step):
    library(world)
    edit(world, {"1": {"title": "Awakening"}})
    crash_after(monkeypatch, step, "Vol.1 Ch.1.cbz")  # a ComicInfo-only rewrite: 1 export compares and finishes it
    with pytest.raises(KeyboardInterrupt):
        world.run(export)
    monkeypatch.undo()
    world.run(export)
    assert title(world, "Vol.1 Ch.1.cbz") == "Awakening"
    assert world.rows(
        "SELECT comicinfo_title, pending_path FROM published_file WHERE library_path LIKE '%%Ch.1.cbz'"
    ) == [("Awakening", None)], "finished once, the title recorded"


@budget(load="db", seconds=30)
def test_a_disorder_the_series_already_has_does_not_block_an_edit_and_naming_diff_lists_edits(world):
    library(world)
    world.rows("UPDATE published_file SET comicinfo_volume = '2' WHERE library_path LIKE '%%Ch.1.cbz' RETURNING id")
    edit(world, {"1": {"title": "Awakening"}})  # chapter 1 in volume 2 before chapter 2 in volume 1: not this edit's
    with closing(db.connect(world.cfg)) as conn:
        series = conn.execute("SELECT * FROM series WHERE origin = 'kavita'").fetchone()
        queued, _, _, _, edits = rewrite.diff(world.context(conn, "export"), series)
    assert edits == [("1", "Awakening", None)]
    assert [(q[0], q[5]) for q in queued] == [(f"library/{SERIES}/Vol.1 Ch.1.cbz", "Awakening")]


@budget(load="pure", seconds=1)
def test_an_open_chapters_edited_volume_names_the_open_chapters_after_it():
    rows = [settled(1, 1, "season markers"), settled(2, 1, "season markers"), new(3), new(4), new(5)]
    edits = {naming.edit_key(names.MAIN, None, 3): naming.Edit(None, 2)}
    got = {k[1]: v.volume for k, v in naming.resolve("season markers", rows, None, {}, edits=edits).items()}
    assert got == {3: 2, 4: 2, 5: 2}, (
        "the edited chapter 3 ends volume 1 at chapter 2 as a recorded season volume: the chapters after it follow"
    )
