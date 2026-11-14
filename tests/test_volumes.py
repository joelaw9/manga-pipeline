"""A new chapter's volume and the published name it gives (SPEC "Publishing"): the bulk converter's rule."""

from pathlib import Path

import pytest

from manga_pipeline import config, export, intake, names, process, volumes

from tests.timing import budget
from tests.world import World, cbz
from tests.world import comicinfo as ci


def series(basis, name="Solo Leveling"):
    return {"naming_basis": basis, "rules_key": name}


def write_override(folder, name, text):
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / f"{name}.yaml").write_text(text, encoding="utf-8")


def world_overrides(world):
    return config.abs_path(world.cfg["paths"]["naming_overrides"])


@budget(load="pure")
@pytest.mark.parametrize(
    "basis, stem, ci_volume, want",
    [
        ("filename", "Vol.3 Ch.20", None, 3),
        ("filename", "Chapter 20", "02", 2),
        ("filename", "Chapter 20", None, None),
        ("season tags", "S2 - Chapter 5", None, 2),
        ("season tags", "Chapter 5", None, None),
        ("side stories need volumes: main story = Vol 1", "Chapter 5", None, 1),
        ("mangadex", "Vol.3 Ch.20", "3", None),
        ("none", "Vol.3 Ch.20", None, None),
        (None, "Vol.3 Ch.20", None, None),
        ("filename", "Side Story 1", "4", None),
    ],
    ids=[
        "filename-name",
        "filename-comicinfo",
        "filename-none",
        "season",
        "season-none",
        "side-default",
        "network-basis",
        "basis-none",
        "no-basis",
        "side-story",
    ],
)
def test_volume_by_basis(basis, stem, ci_volume, want, tmp_path):
    got = volumes.chapter_volume(series(basis), names.parse(stem), ci_volume, tmp_path)
    assert got == want, "only the bases Phase 3 can know give a volume, and only to a main chapter"


@budget(load="fs")
@pytest.mark.parametrize(
    "number, want",
    [("3", 0), ("4", 3), ("5", None), ("65", 1), ("66", 2), ("141", 2), ("65.5", None)],
    ids=["open-start", "single", "in-no-range", "closed-end", "range-start", "open-end", "between-ranges"],
)
def test_override_ranges_as_the_bulk_converter_reads_them(tmp_path, number, want):
    write_override(tmp_path, "Solo Leveling", 'layout: strip\nvolumes: {0: "-3", 3: "4", 1: "6-65", 2: "66-"}\n')
    got = volumes.chapter_volume(series("override"), names.parse(f"Chapter {number}"), None, tmp_path)
    assert got == want


@budget(load="fs")
@pytest.mark.parametrize(
    "text",
    [
        None,
        "volumes: [1, 2]\n",
        'volumes: {x: "1-5"}\n',
        'volumes: {1: "9-2"}\n',
        'volumes: {1: "a-b"}\n',
        "- a\n",
        "volumes: {1: [\n",
    ],
    ids=["missing", "not-a-mapping", "bad-volume", "backwards", "bad-range", "file-not-a-mapping", "bad-yaml"],
)
def test_a_missing_or_malformed_override_is_an_override_error(tmp_path, text):
    if text is not None:
        write_override(tmp_path, "Solo Leveling", text)
    with pytest.raises(volumes.OverrideError, match="Solo Leveling"):
        volumes.chapter_volume(series("override"), names.parse("Chapter 3"), None, tmp_path)


@budget(load="db", seconds=2)
def test_a_chapter_of_an_override_series_is_published_with_the_bulk_converters_name(tmp_path):
    world = World(tmp_path / "world")
    write_override(world_overrides(world), "Solo Leveling", 'volumes: {1: "1-65", 2: "66-"}\n')
    world.kavita_series("Solo Leveling", bulk=(("Vol.1 Ch.1.cbz", "Grp_Chapter 1", "1"),), basis="override")
    world.download(
        world.manga("Solo Leveling"),
        "Chapter 141: Arise (END)",
        data=cbz(comicinfo=ci(Number="141", Title="Chapter 141 : Arise (END)", Volume="9")),
    )
    for part in (intake, process, export):
        world.run(part)
    target = world.library / "Solo Leveling" / "Vol.2 Ch.141 - Arise.cbz"
    assert target.is_file(), "Vol.<override volume> Ch.<n> - <cleaned title>.cbz"
    assert world.rows("SELECT comicinfo_volume, comicinfo_title FROM published_file WHERE origin = 'pipeline'") == [
        ("2", "Arise")
    ], "ComicInfo Volume is the computed one (the download's 9 is dropped), Title the cleaned one"


@budget(load="db", seconds=2)
def test_an_override_series_without_its_file_needs_a_human_at_comicinfo(tmp_path):
    world = World(tmp_path / "world")
    world.kavita_series("Solo Leveling", basis="override")
    world.download(world.manga("Solo Leveling"), "Chapter 2")
    for part in (intake, process, export):
        world.run(part)
    assert world.rows("SELECT stage, status, attempts FROM chapter") == [("comicinfo", "needs_human", 0)]
    assert not list(Path(world.library / "Solo Leveling").glob("Ch.2*")), "nothing published"


@budget(load="pure")
def test_a_file_name_volume_that_is_not_whole_is_a_volume_error():
    with pytest.raises(volumes.VolumeError, match="volume 2.5 is not a whole number"):
        volumes.chapter_volume(series("filename"), names.parse("Vol.2.5 Ch.10"), None, "unused")


@budget(load="db", seconds=2)
def test_without_a_volume_the_downloads_volume_is_dropped_and_the_title_falls_back(tmp_path):
    world = World(tmp_path / "world")
    world.kavita_series("Solo Leveling")  # basis none
    world.download(world.manga("Solo Leveling"), "Chapter 2", data=cbz(comicinfo=ci(Number="2", Volume="9")))
    for part in (intake, process, export):
        world.run(part)
    assert (world.library / "Solo Leveling" / "Ch.2.cbz").is_file()
    assert world.rows("SELECT comicinfo_volume, comicinfo_title FROM published_file WHERE origin = 'pipeline'") == [
        (None, "Chapter 2")
    ], "name and ComicInfo agree: no volume; an untitled chapter is titled Chapter <n>"


@budget(load="fs")
def test_the_override_file_is_named_by_the_rules_key_not_the_folder(tmp_path):
    """A renamed folder keeps its override file: the file is named by the series' first folder (its rules key)."""
    write_override(tmp_path, "Solo Leveling", 'volumes: {2: "66-"}\n')
    renamed = {"naming_basis": "override", "rules_key": "Solo Leveling", "folder": "/lib/I Level Up Alone"}
    assert volumes.chapter_volume(renamed, names.parse("Chapter 70"), None, tmp_path) == 2
